"""Detect and resolve stuck 3d_pipeline_ingest='IngestRunning' tiles.

When a CANFAR ingest headless session dies before writing its DB status back
(CANFAR outage, OOM, crash), 3d_pipeline_ingest stays at 'IngestRunning'
indefinitely. The pipeline then neither retries nor completes the tile.

This module walks the stuck list each pipeline cycle and resolves each tile
based on real evidence (CADC + skaha), not a timeout:

  CADC has all expected *_p3d_v1 planes       -> PATCH 'Ingested'
  CADC incomplete AND no live `ingest<N>`    -> PATCH NULL (pipeline retries)
  CADC incomplete AND session alive          -> no-op (still in flight)

Call reconcile_stuck_ingest_running(band_number, conn) at the start of
check_ingest_3Dpipeline.ingest_3Dpipeline() BEFORE get_tiles_for_ingest.
"""
from __future__ import annotations

import os

import astroquery.cadc as cadc

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

# Default CADC cert for TAP; same path check_CADC uses on CANFAR.
_DEFAULT_CERT = "/arc/home/ErikOsinga/.ssl/cadcproxy.pem"


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
    """One TAP query returning {tile: set_of_p3d_planes_present_at_CADC}.

    Only *_p3d_v1 planes are counted. Pre-3D planes are ignored.
    Tiles with no artifacts at CADC map to the empty set.
    """
    os.environ["CADCTAP_SERVICE_URI"] = "ivo://cadc.nrc.ca/ams/cirada"
    sess = cadc.Cadc()
    sess.login(certificate_file=cert_file)

    if not tiles:
        return {}
    freq = band.replace("MHz", "")
    like_clauses = " OR ".join(
        f"o.observationID LIKE '{band}_%_{t}_v1'" for t in tiles
    )
    query = f"""
    SELECT o.observationID, p.productID
    FROM caom2.Observation o
    JOIN caom2.Plane p ON o.obsID = p.obsID
    WHERE o.collection = 'POSSUM'
      AND p.productID LIKE '%_p3d_v1'
      AND ({like_clauses})
    """
    async_q = sess.create_async(query)
    async_q.run().wait()
    async_q.raise_if_error()
    result = async_q.fetch_result().to_table()

    out = {t: set() for t in tiles}
    for row in result:
        obs = str(row["observationID"])
        pid = str(row["productID"])
        try:
            tile = int(obs.rsplit("_v1", 1)[0].rsplit("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if tile in out:
            out[tile].add(pid)
    return out


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
):
    """Walk every IngestRunning tile and reconcile it based on CADC + skaha.

    Args:
        band_number: 1 or 2.
        conn: a `rest_api.PossumApiClient`.
        cert_file: path to CADC proxy cert (defaults to the one check_CADC uses).
        dry_run: print the planned action without PATCHing.

    Returns:
        dict with counts: {"flipped_ingested": int, "nulled": int,
                           "left_alone": int, "stuck_total": int}
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
          f"3d_pipeline_ingest='IngestRunning'")
    if not stuck:
        return {"flipped_ingested": 0, "nulled": 0, "left_alone": 0, "stuck_total": 0}

    cadc_by_tile = _cadc_p3d_planes_by_tile(stuck, band, cert_file)
    live_sessions = _live_ingest_session_tiles()
    if live_sessions is None:
        print("[reconcile_ingest] skaha unavailable; skipping to avoid "
              "nulling tiles whose session may still be running")
        return {"flipped_ingested": 0, "nulled": 0,
                "left_alone": len(stuck), "stuck_total": len(stuck)}

    flipped = nulled = left_alone = 0
    for tile in stuck:
        planes_at_cadc = cadc_by_tile.get(tile, set())
        n_at_cadc = len(planes_at_cadc & expected)
        session_alive = tile in live_sessions

        if n_at_cadc >= n_expected:
            action = "flip_ingested"
        elif session_alive:
            action = "leave_alone"
        else:
            action = "null_retry"

        print(f"[reconcile_ingest] tile {tile}: p3d={n_at_cadc}/{n_expected}  "
              f"session_alive={session_alive}  action={action}  "
              f"{'(DRY-RUN)' if dry_run else ''}")

        if dry_run:
            if action == "flip_ingested":
                flipped += 1
            elif action == "null_retry":
                nulled += 1
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
            else:
                left_alone += 1
        except Exception as e:  # noqa: BLE001 - want to keep going on a per-tile failure
            print(f"[reconcile_ingest] tile {tile}: PATCH failed: {e!r}")

    summary = {
        "flipped_ingested": flipped,
        "nulled": nulled,
        "left_alone": left_alone,
        "stuck_total": len(stuck),
    }
    print(f"[reconcile_ingest] band {band_number}: summary {summary}")
    return summary
