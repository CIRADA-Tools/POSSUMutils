"""Detect and resolve stuck 3d_pipeline_ingest='IngestRunning' tiles.

When a CANFAR ingest headless session dies before writing its DB status back
(CANFAR outage, OOM, crash), 3d_pipeline_ingest stays at 'IngestRunning'
indefinitely. The pipeline then neither retries nor completes the tile.

This module walks the stuck list each pipeline cycle and resolves each tile
based on real evidence (CADC + skaha), not a timeout. Verification combines a
CAOM2 TAP plane check with a minoc HEAD on every science artifact, so the
"orphan-partial" pattern (TAP registered 14/14 planes but minoc has 404s on
one or more files) is caught and treated as incomplete rather than flipped:

  TAP 14/14 planes AND minoc HEAD 200 on every science URI -> PATCH 'Ingested'
  CADC incomplete (TAP < 14/14, OR any minoc 404) AND no live `ingest<N>`
                                                        -> PATCH NULL (retry)
  CADC incomplete AND session alive                     -> no-op (still in flight)

Call reconcile_stuck_ingest_running(band_number, conn) at the start of
check_ingest_3Dpipeline.ingest_3Dpipeline() BEFORE get_tiles_for_ingest.
"""
from __future__ import annotations

import os

import astroquery.cadc as cadc
import requests

from print_all_open_sessions import get_open_sessions

# Planes a complete 3D ingest publishes to CADC (14 of them).
# Kept in sync with ingest3Dpipeline.all_3dproducts.
EXPECTED_3D_PLANES = {
    "FDF_real_dirty_p3d_v1",
    "FDF_im_dirty_p3d_v1",
    "FDF_tot_dirty_p3d_v1",
    "RMSF_FWHM_p3d_v1",
    "RMSF_tot_p3d_v1",
    "amp_peak_pi_p3d_v1",
    "frac_pol_p3d_v1",
    "i_model_p3d_v1",
    "misc_p3d_v1",
    "phi_peak_pi_fit_p3d_v1",
    "pol_angle_0_fit_p3d_v1",
    "snr_pi_fit_p3d_v1",
    "RMSF_im_p3d_v1",
    "RMSF_real_p3d_v1",
}

# Skaha session states that mean a session is still active (reconcile skips these).
_ALIVE_SKAHA_STATES = {"pending", "running"}

# CADC storage endpoint for direct HEAD on each science artifact URI.
_MINOC_PREFIX = "https://ws-cadc.canfar.net/minoc/files/"

# Timeout for a single minoc HEAD. Each tile makes up to 14 of these.
_MINOC_HEAD_TIMEOUT_S = 15

# Default CADC cert for TAP. Prefect's "Stage CADC certificate" task
# (util.stage_cadc_certificate) deposits the cert at ~/.ssl/cadcproxy.pem of
# the Prefect worker user before the flow runs. The absolute path hardcoded
# in ingest3Dpipeline.check_CADC ("/arc/home/ErikOsinga/.ssl/cadcproxy.pem")
# only works when running headless on CANFAR as that user, which the
# reconciler does NOT.
_DEFAULT_CERT = os.path.expanduser("~/.ssl/cadcproxy.pem")


def _get_stuck_tiles(conn, band_number):
    """Return a list of tile ints whose 3d_pipeline_ingest == 'IngestRunning'."""
    rows = conn.get_json(
        f"/3d-pipeline/tiles/order_by_3d_pipeline_val/band{band_number}/"
    )
    stuck = []
    for r in rows:
        if str(r.get("3d_pipeline_ingest", "")).strip() == "IngestRunning":
            try:
                stuck.append(int(r["tile"]))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(set(stuck))


def _cadc_p3d_planes_by_tile(tiles, band, cert_file):
    """One TAP query returning {tile: {productID: [science_uri, ...]}}.

    Only *_p3d_v1 planes are counted and only artifacts with
    productType='science' are kept (previews are skipped — not needed for
    minoc verification). Tiles with no artifacts at CADC map to {}.
    """
    os.environ["CADCTAP_SERVICE_URI"] = "ivo://cadc.nrc.ca/ams/cirada"
    sess = cadc.Cadc()
    sess.login(certificate_file=cert_file)

    if not tiles:
        return {}
    like_clauses = " OR ".join(
        f"o.observationID LIKE '{band}_%_{t}_v1'" for t in tiles
    )
    query = f"""
    SELECT o.observationID, p.productID, a.uri, a.productType
    FROM caom2.Observation o
    JOIN caom2.Plane p ON o.obsID = p.obsID
    JOIN caom2.Artifact a ON p.planeID = a.planeID
    WHERE o.collection = 'POSSUM'
      AND p.productID LIKE '%_p3d_v1'
      AND ({like_clauses})
    """
    async_q = sess.create_async(query)
    async_q.run().wait()
    async_q.raise_if_error()
    result = async_q.fetch_result().to_table()

    out = {t: {} for t in tiles}
    for row in result:
        if str(row["productType"]).lower() != "science":
            continue
        obs = str(row["observationID"])
        pid = str(row["productID"])
        uri = str(row["uri"])
        try:
            tile = int(obs.rsplit("_v1", 1)[0].rsplit("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if tile in out:
            out[tile].setdefault(pid, []).append(uri)
    return out


def _head_minoc(uri, cert_file):
    """HEAD a minoc URI and return its HTTP status (0 on request failure).

    Used to catch the "orphan-partial" pattern: TAP registered the plane but
    the actual science FITS file is a 404 at minoc. A tile cannot be flipped
    to Ingested unless every science URI returns 200.
    """
    try:
        r = requests.head(
            _MINOC_PREFIX + uri,
            cert=cert_file,
            timeout=_MINOC_HEAD_TIMEOUT_S,
            allow_redirects=True,
        )
        return r.status_code
    except Exception:  # noqa: BLE001 - defensive: any failure => "unknown, not 200"
        return 0


def _minoc_fully_present(planes_by_product, cert_file, expected):
    """Return True iff every expected plane has at least one science URI 200.

    `planes_by_product` is {productID: [uri, ...]} for one tile, as returned
    by `_cadc_p3d_planes_by_tile`. A product with multiple science URIs is
    considered present if ANY one of them HEADs 200.
    """
    for pid in expected:
        uris = planes_by_product.get(pid, [])
        if not uris:
            return False
        if not any(_head_minoc(u, cert_file) == 200 for u in uris):
            return False
    return True


def _live_ingest_session_tiles():
    """Return the set of tile ints that have a live `ingest<N>` CANFAR session.

    'Live' = status in {Pending, Running}. On skaha API failure, returns None
    (sentinel so the caller treats "unknown session state" as safe no-op,
    rather than wrongly concluding sessions are dead).
    """
    try:
        df = get_open_sessions(verbose=False)
    except Exception as e:  # noqa: BLE001  - want to be defensive about skaha here
        print(f"[reconcile_ingest] skaha session fetch failed: {e!r}; "
              "treating sessions as unknown (will skip reconciliation)")
        return None
    if df is None or len(df) == 0:
        return set()
    if "status" not in df.columns or "name" not in df.columns:
        return set()
    alive = df[df["status"].astype(str).str.lower().isin(_ALIVE_SKAHA_STATES)]
    tiles = set()
    for name in alive["name"].dropna().astype(str):
        if name.startswith("ingest"):
            suffix = name[len("ingest"):]
            if suffix.isdigit():
                tiles.add(int(suffix))
    return tiles


def _patch_status(conn, band_number, tile_number, new_status):
    """PATCH /3d-pipeline/tiles/update/3d_pipeline_ingest/
    with 3d_pipeline_ingest=<new_status>, or omitted to null."""
    endpoint = (
        "/3d-pipeline/tiles/update/3d_pipeline_ingest/"
        f"?band_number={band_number}&tile_number={tile_number}"
    )
    if new_status is not None:
        endpoint += f"&3d_pipeline_ingest={new_status}"
    return conn.patch(endpoint)


def reconcile_stuck_ingest_running(
    band_number,
    conn,
    cert_file=None,
    dry_run=False,
    strict_minoc=True,
):
    """Walk every IngestRunning tile and reconcile it based on CADC + skaha.

    Args:
        band_number: 1 or 2.
        conn: a `rest_api.PossumApiClient`.
        cert_file: path to CADC proxy cert (defaults to the one check_CADC uses).
        dry_run: print the planned action without PATCHing.
        strict_minoc: if True (default), also HEAD every science artifact URI
            at minoc before flipping to Ingested. Set to False only to recover
            the pre-strict behavior (TAP-plane count alone), which can wrongly
            flip "orphan-partial" tiles (TAP shows 14/14 but minoc 404s one or
            more files). Not recommended.

    Returns:
        dict with counts: {"flipped_ingested": int, "nulled": int,
                           "left_alone": int, "orphan_partial_nulled": int,
                           "stuck_total": int}
    """
    if band_number == 1:
        band = "943MHz"
    elif band_number == 2:
        band = "1367MHz"
    else:
        raise ValueError(f"band_number must be 1 or 2, got {band_number!r}")

    cert_file = cert_file or _DEFAULT_CERT
    expected = EXPECTED_3D_PLANES
    n_expected = len(expected)

    stuck = _get_stuck_tiles(conn, band_number)
    print(f"[reconcile_ingest] band {band_number}: {len(stuck)} tile(s) at "
          f"3d_pipeline_ingest='IngestRunning'  (strict_minoc={strict_minoc})")
    if not stuck:
        return {"flipped_ingested": 0, "nulled": 0, "left_alone": 0,
                "orphan_partial_nulled": 0, "stuck_total": 0}

    cadc_by_tile = _cadc_p3d_planes_by_tile(stuck, band, cert_file)
    live_sessions = _live_ingest_session_tiles()
    if live_sessions is None:
        print("[reconcile_ingest] skaha unavailable; skipping to avoid "
              "nulling tiles whose session may still be running")
        return {"flipped_ingested": 0, "nulled": 0,
                "left_alone": len(stuck),
                "orphan_partial_nulled": 0, "stuck_total": len(stuck)}

    flipped = nulled = left_alone = orphan_partial = 0
    for tile in stuck:
        planes_by_product = cadc_by_tile.get(tile, {})
        planes_present = set(planes_by_product.keys()) & expected
        n_at_cadc = len(planes_present)
        session_alive = tile in live_sessions
        tap_complete = n_at_cadc >= n_expected

        minoc_ok = None  # only computed when tap_complete
        orphan_partial_flag = False
        if tap_complete and strict_minoc:
            minoc_ok = _minoc_fully_present(planes_by_product, cert_file, expected)
            if not minoc_ok:
                orphan_partial_flag = True

        if tap_complete and (not strict_minoc or minoc_ok):
            action = "flip_ingested"
        elif session_alive:
            action = "leave_alone"
        else:
            action = "null_retry"

        minoc_tag = "" if minoc_ok is None else f" minoc_ok={minoc_ok}"
        orphan_tag = "  [ORPHAN-PARTIAL]" if orphan_partial_flag else ""
        print(f"[reconcile_ingest] tile {tile}: p3d={n_at_cadc}/{n_expected}"
              f"{minoc_tag}  session_alive={session_alive}  action={action}"
              f"{orphan_tag}  {'(DRY-RUN)' if dry_run else ''}")

        if dry_run:
            if action == "flip_ingested":
                flipped += 1
            elif action == "null_retry":
                nulled += 1
                if orphan_partial_flag:
                    orphan_partial += 1
            else:
                left_alone += 1
            continue

        try:
            if action == "flip_ingested":
                _patch_status(conn, band_number, tile, "Ingested")
                flipped += 1
            elif action == "null_retry":
                _patch_status(conn, band_number, tile, None)
                nulled += 1
                if orphan_partial_flag:
                    orphan_partial += 1
            else:
                left_alone += 1
        except Exception as e:  # noqa: BLE001 - want to keep going on a per-tile failure
            print(f"[reconcile_ingest] tile {tile}: PATCH failed: {e!r}")

    summary = {
        "flipped_ingested": flipped,
        "nulled": nulled,
        "left_alone": left_alone,
        "orphan_partial_nulled": orphan_partial,
        "stuck_total": len(stuck),
    }
    print(f"[reconcile_ingest] band {band_number}: summary {summary}")
    return summary
