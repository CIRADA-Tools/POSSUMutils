"""Reconcile DB state against CADC + minoc + skaha once ingest is done.

The reconciler keeps the DB consistent with the three sources of truth
(CADC TAP, minoc, skaha session state). The Google Status Sheet is a
derived display, not an authority, and is intentionally NOT written here
— keeping it in sync is a separate concern.

The reconciler never writes during processing. A live `ingest<N>` skaha
session owns the status write for that tile; the reconciler only acts
once the session is gone. Earlier revisions wrote during processing and
raced with do_ingest, leaving the DB status flipped by the reconciler
and the date column empty because do_ingest's own write path raised on
the already-changed status.

Three post-processing cases need reconciling:
  - 'IngestRunning': do_ingest's status write never landed (CANFAR outage,
     OOM, crash). Session is gone.
  - 'IngestFailed': do_ingest marked the row failed but a complete set of
     products may still be present at CADC (transient per-file errors on
     a run that nonetheless uploaded everything, or a prior partial run
     already published the planes).
  - 'IngestedNoDate': status='Ingested' but 3d_pipeline date column is
     NULL. Drift left by the earlier reconciler-during-processing race
     (e.g. tiles 5617, 11418). Backfill the date from TAP.

Decision table (session_alive=True is the ONLY leave_alone case).

Every row below assumes session_alive=False (do_ingest not in flight).
When session_alive=True we leave_alone regardless of any other state.

Only-touch-what-needs-touching: writes are minimal. If the DB already
has the correct value for a column, that column is not re-written.

  Current DB state         CADC state                 Action
  --------------------------------------------------------------------
  IngestRunning,           TAP 14/14 + minoc OK       flip to Ingested
    date MISSING                                        - write date, then status
                                                        - date = max TAP
                                                          lastModified (today
                                                          as rare fallback)

  IngestRunning,           TAP 14/14 + minoc OK       flip to Ingested
    date PRESENT                                        - skip date PATCH
                                                        - write status only

  IngestRunning            TAP 14/14 + minoc 404      null (orphan-partial;
                                                       pipeline retries)

  IngestRunning            TAP < 14/14                null (incomplete;
                                                       session died mid-upload)

  IngestFailed, any date   TAP 14/14 + minoc OK       flip to Ingested
                                                       (same date rule as
                                                        IngestRunning)

  IngestFailed             TAP 14/14 + minoc 404      null (orphan-partial)

  IngestFailed             TAP < 14/14                null (truly failed on
                                                       this attempt; let the
                                                       pipeline retry. If the
                                                       underlying failure is
                                                       persistent — e.g. tile
                                                       11719's sc2repo
                                                       underflow — this cycles
                                                       each tick. Detection
                                                       lives in the audit TODO.)

  Ingested, date MISSING   TAP 14/14 + minoc OK       backfill_date
    ('IngestedNoDate')                                  - write date only
                                                        - status untouched

  Ingested, date MISSING   anything else              null (DB wrong per CADC;
    ('IngestedNoDate')                                  trust CADC. Transient
                                                       TAP outages don't reach
                                                       this branch — the TAP
                                                       query raises and the
                                                       whole tick aborts.)

A second pass runs every tick, purely for observability: `ingest<N>` skaha
sessions whose status is still 'Running'/'Pending' past the staleness
threshold (default 8 h; a nominal do_ingest completes in ~20-30 min but
with retries + CADC congestion a legitimate run can push past 2h) are
reported in a WARNING log line with an inline curl delete command, and
surfaced in a Prefect Markdown artifact `stale-skaha-sessions`. Deletion is
left to the operator — this module emits the signal, not the action.

Call reconcile_stuck_ingest_running(band_number, conn) at the start of
check_ingest_3Dpipeline.ingest_3Dpipeline() BEFORE get_tiles_for_ingest.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

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

# Threshold for marking an `ingest<N>` skaha session as stale (observability
# only in this version — we log + emit a Prefect artifact; we do NOT delete).
# A real do_ingest end-to-end is nominally ~20-30 min, but with `retry_count:3`
# and `retry_failures: True` in the ingest config (set 2026-06-15), plus
# occasional CADC/minoc congestion and caom2utils FITS verification on
# malformed headers, a legitimate run can push past 2h. On 2026-11-02 we
# observed tile 10438 still progressing at 2h+ and silent (not advancing) at
# ~3h — not a clear ghost. The 10 real ghosts we hit the same day were 23-32h.
# 8h = ~16x typical, giving generous margin for retries+congestion while still
# catching real ghosts within one third of a day.
_STALE_INGEST_SESSION_HOURS = 8.0

# Only `ingest<N>` sessions are considered (3D-pipeline ingest sessions).
# 1D sessions use a different name convention (<sbid>-HH-MM-SS) and can run
# legitimately for many hours, so we never touch them here.
_INGEST_SESSION_RE = re.compile(r"^ingest\d+$")

# Skaha session delete endpoint for the inline copy-paste command we emit in
# the WARNING log line and in the Prefect Markdown artifact.
_SKAHA_SESSION_URL = "https://ws-uv.canfar.net/skaha/v1/session"

# Default CADC cert for TAP. Prefect's "Stage CADC certificate" task
# (util.stage_cadc_certificate) deposits the cert at ~/.ssl/cadcproxy.pem of
# the Prefect worker user before the flow runs. The absolute path hardcoded
# in ingest3Dpipeline.check_CADC ("/arc/home/ErikOsinga/.ssl/cadcproxy.pem")
# only works when running headless on CANFAR as that user, which the
# reconciler does NOT.
_DEFAULT_CERT = os.path.expanduser("~/.ssl/cadcproxy.pem")


def _get_stuck_tiles_by_status(conn, band_number):
    """Return {tile: (status_label, has_date)} for every tile the reconciler
    may need to look at.

    `has_date` captures whether the DB's `3d_pipeline` (date) column is
    already populated. It lets the main loop apply the "only write what's
    missing" rule: when flipping a stuck row to Ingested, skip the date
    write if the DB already has one.

    Three post-processing cases:
      - 'IngestRunning': do_ingest's status write never landed (CANFAR
         outage, OOM, crash). The ingest skaha session may or may not still
         be alive; the main loop uses session_alive to decide.
      - 'IngestFailed': do_ingest marked the row failed but a complete set
         of products may still be present at CADC.
      - 'IngestedNoDate': 3d_pipeline_ingest='Ingested' but 3d_pipeline
         (the date column) is NULL. Drift left by the earlier reconciler-
         during-processing race (e.g. tiles 5617, 11418). Backfill the
         date from TAP's max(lastModified).

    'Ingested + no date' uses a dedicated label (not the raw 'Ingested')
    because the action is different: no status flip, just a date write.
    """
    rows = conn.get_json(
        f"/3d-pipeline/tiles/order_by_3d_pipeline_val/band{band_number}/"
    )
    out = {}
    for r in rows:
        current = str(r.get("3d_pipeline_ingest", "")).strip()
        date_val = r.get("3d_pipeline")
        date_str = str(date_val).strip() if date_val is not None else ""
        has_date = date_str not in ("", "None")
        try:
            tile_id = int(r["tile"])
        except (KeyError, TypeError, ValueError):
            continue
        if current in ("IngestRunning", "IngestFailed"):
            out[tile_id] = (current, has_date)
        elif current == "Ingested" and not has_date:
            out[tile_id] = ("IngestedNoDate", False)
    return out


def _cadc_p3d_planes_by_tile(tiles, band, cert_file):
    """One TAP query returning (planes_by_tile, last_modified_by_tile).

    planes_by_tile: {tile: {productID: [science_uri, ...]}}
    last_modified_by_tile: {tile: 'YYYY-MM-DD'}  (max Observation.lastModified
                                                  across the tile's planes;
                                                  mirrors ingest3Dpipeline.check_CADC)

    Only *_p3d_v1 planes are counted and only artifacts with
    productType='science' are kept (previews are skipped — not needed for
    minoc verification). Tiles with no artifacts at CADC map to {}, and are
    absent from last_modified_by_tile.
    """
    os.environ["CADCTAP_SERVICE_URI"] = "ivo://cadc.nrc.ca/ams/cirada"
    sess = cadc.Cadc()
    sess.login(certificate_file=cert_file)

    if not tiles:
        return {}, {}
    like_clauses = " OR ".join(
        f"o.observationID LIKE '{band}_%_{t}_v1'" for t in tiles
    )
    query = f"""
    SELECT o.observationID, o.lastModified, p.productID, a.uri, a.productType
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
    last_mod = {}
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
        if tile not in out:
            continue
        out[tile].setdefault(pid, []).append(uri)
        try:
            dt = datetime.fromisoformat(str(row["lastModified"]))
        except (ValueError, TypeError):
            continue
        prev = last_mod.get(tile)
        if prev is None or dt > prev:
            last_mod[tile] = dt
    date_by_tile = {t: dt.date().isoformat() for t, dt in last_mod.items()}
    return out, date_by_tile


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
        # get_open_sessions() takes no arguments in the current
        # print_all_open_sessions.py. An earlier version accepted
        # `verbose=False`; the kwarg was removed at some point but this
        # call site was never updated, so from PR #15 onward every call
        # raised TypeError and the reconciler bailed to the "skaha
        # unavailable" branch. Hid until PR #20 hoisted the reconciler
        # above the throttle gate and it actually started running.
        df = get_open_sessions()
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


def _parse_skaha_start_time(s):
    """Parse a skaha `startTime` string into a timezone-aware datetime.

    skaha returns ISO-8601 with a trailing 'Z' (e.g. '2026-11-02T02:22:16Z').
    datetime.fromisoformat accepts 'Z' from Python 3.11; we normalise anyway
    so this works on 3.10 and earlier Prefect base images.
    Returns None on parse failure or a 'Pending' placeholder.
    """
    if not isinstance(s, str) or s == "Pending":
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _find_stale_ingest_sessions(threshold_hours=_STALE_INGEST_SESSION_HOURS):
    """Return a list of `ingest<N>` sessions that have been Running past the
    staleness threshold.

    A real 3D ingest completes in ~20-30 minutes. Sessions running past
    `threshold_hours` are overwhelmingly skaha-side ghosts (the Python
    process has died but skaha's state didn't flip to Completed). They
    throttle the 3D flow by making it look like too many sessions are in
    flight, so new ingests from the null queue never get submitted.

    This function only observes — it does NOT delete anything. The caller
    logs each stale session with an inline curl delete command, and emits
    a Prefect Markdown artifact so the operator can act from the flow run
    page. Deciding to auto-delete later is a separate PR.
    """
    try:
        df = get_open_sessions()
    except Exception as e:  # noqa: BLE001 - skaha fetch may fail; be defensive
        print(f"[reconcile_ingest] skaha session fetch for staleness check "
              f"failed: {e!r}; no stale list this tick")
        return []
    if df is None or len(df) == 0:
        return []
    if not {"name", "status", "startTime", "id"}.issubset(df.columns):
        return []

    now = datetime.now(tz=timezone.utc)
    stale = []
    for _, row in df.iterrows():
        name = str(row["name"])
        status = str(row["status"])
        if not _INGEST_SESSION_RE.match(name):
            continue
        if status.lower() not in _ALIVE_SKAHA_STATES:
            continue
        start = _parse_skaha_start_time(row.get("startTime"))
        if start is None:
            continue
        age_hours = (now - start).total_seconds() / 3600.0
        if age_hours < threshold_hours:
            continue
        stale.append({
            "name": name,
            "id": str(row["id"]),
            "status": status,
            "startTime": str(row["startTime"]),
            "age_hours": age_hours,
        })
    return stale


def _report_stale_sessions(stale):
    """Log a WARNING per stale session and emit a Prefect Markdown artifact.

    Writes nothing when `stale` is empty. The Prefect import is deferred and
    wrapped: this module should remain importable (and unit-testable) outside
    a Prefect flow context. Returns the artifact key when created, else None.
    """
    if not stale:
        return None

    for s in stale:
        delete_cmd = (
            "curl -X DELETE --cert ~/.ssl/cadcproxy.pem "
            f"--key ~/.ssl/cadcproxy.pem {_SKAHA_SESSION_URL}/{s['id']}"
        )
        print(f"[reconcile_ingest] WARNING STALE skaha session  "
              f"name={s['name']}  id={s['id']}  status={s['status']}  "
              f"started={s['startTime']}  age={s['age_hours']:.1f}h  "
              f"-- delete with: {delete_cmd}")

    try:
        from prefect.artifacts import create_markdown_artifact
    except Exception:  # noqa: BLE001 - not running under Prefect; log-only is fine
        return None

    lines = [
        "## Stale `ingest<N>` skaha sessions",
        "",
        f"Threshold: **{_STALE_INGEST_SESSION_HOURS} h**  "
        f"(a real do_ingest completes in ~20-30 min).",
        "",
        "These sessions are overwhelmingly skaha-side ghosts and are "
        "throttling the 3D flow by inflating the \"running\" count. "
        "Delete manually from the CANFAR portal, or run the inline curl.",
        "",
        "| Session | Age (h) | Status | Started | Delete command |",
        "|---|---|---|---|---|",
    ]
    for s in stale:
        delete_cmd = (
            f"`curl -X DELETE --cert ~/.ssl/cadcproxy.pem "
            f"--key ~/.ssl/cadcproxy.pem {_SKAHA_SESSION_URL}/{s['id']}`"
        )
        lines.append(
            f"| `{s['name']}` ({s['id']}) | {s['age_hours']:.1f} | "
            f"{s['status']} | {s['startTime']} | {delete_cmd} |"
        )
    md = "\n".join(lines)

    try:
        create_markdown_artifact(key="stale-skaha-sessions", markdown=md)
        return "stale-skaha-sessions"
    except Exception as e:  # noqa: BLE001 - artifact create outside flow run
        print(f"[reconcile_ingest] stale-session artifact create failed "
              f"(not under a flow run?): {e!r}")
        return None


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


def _patch_date(conn, band_number, tile_number, date):
    """PATCH /3d-pipeline/tiles/update/3d_pipeline/ with the ingest date.

    Mirrors the DB-side write that ingest3Dpipeline.update_status_spreadsheet
    performs at the end of do_ingest. The Google Sheet write is intentionally
    NOT performed by the reconciler: the sheet is a derived display, not a
    source of truth, and keeping it in sync is a separate concern. Sources
    of truth consulted by the reconciler are DB + TAP + minoc + skaha.
    """
    return conn.patch(
        "/3d-pipeline/tiles/update/3d_pipeline/",
        json={
            "band_number": band_number,
            "tile_number": tile_number,
            "timestamp": date,
        },
    )


def _write_consistent_ingested(conn, band_number, tile_number, date):
    """Write date then flip status to 'Ingested' (date first, status last).

    Called when the reconciler is replacing a stuck IngestRunning or
    IngestFailed status with a confirmed Ingested state. The write order
    enforces an invariant: the status flag never advances past the date.
    If _patch_date fails, status stays at IngestRunning/IngestFailed and
    the reconciler's stuck-list sweep re-picks the row on the next tick
    (_patch_date is idempotent — same date on retry).
    """
    _patch_date(conn, band_number, tile_number, date)
    _patch_status(conn, band_number, tile_number, "Ingested")


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

    # Staleness check runs independently of the DB reconciliation below, so a
    # stale-session alert is emitted every tick even when there are no stuck
    # DB rows to resolve. Pure observability: we do not delete here.
    stale_sessions = _find_stale_ingest_sessions()
    _report_stale_sessions(stale_sessions)

    stuck_by_status = _get_stuck_tiles_by_status(conn, band_number)
    n_running = sum(1 for s, _ in stuck_by_status.values() if s == "IngestRunning")
    n_failed = sum(1 for s, _ in stuck_by_status.values() if s == "IngestFailed")
    n_nodate = sum(1 for s, _ in stuck_by_status.values() if s == "IngestedNoDate")
    print(f"[reconcile_ingest] band {band_number}: {len(stuck_by_status)} tile(s) "
          f"to review  (IngestRunning={n_running}, IngestFailed={n_failed}, "
          f"IngestedNoDate={n_nodate}, strict_minoc={strict_minoc})")
    if not stuck_by_status:
        return {"flipped_from_running": 0, "flipped_from_failed": 0,
                "date_backfilled": 0,
                "nulled": 0, "left_alone": 0, "orphan_partial_nulled": 0,
                "stale_skaha_detected": len(stale_sessions),
                "stuck_total": 0}

    stuck_tiles = sorted(stuck_by_status.keys())
    cadc_by_tile, date_by_tile = _cadc_p3d_planes_by_tile(
        stuck_tiles, band, cert_file
    )
    live_sessions = _live_ingest_session_tiles()
    if live_sessions is None:
        # Session state is the hard gate that prevents writing during
        # processing. Without it we cannot distinguish "ingest in flight"
        # from "session dead", so we skip the whole tick — no writes.
        print("[reconcile_ingest] skaha unavailable; skipping (will not write "
              "during unknown session state)")
        return {"flipped_from_running": 0, "flipped_from_failed": 0,
                "date_backfilled": 0,
                "nulled": 0, "left_alone": len(stuck_by_status),
                "orphan_partial_nulled": 0,
                "stale_skaha_detected": len(stale_sessions),
                "stuck_total": len(stuck_by_status)}

    flipped_running = flipped_failed = date_backfilled = 0
    nulled = left_alone = orphan_partial = 0
    for tile, (current_status, db_has_date) in stuck_by_status.items():
        planes_by_product = cadc_by_tile.get(tile, {})
        planes_present = set(planes_by_product.keys()) & expected
        n_at_cadc = len(planes_present)
        session_alive = tile in live_sessions
        tap_complete = n_at_cadc >= n_expected

        minoc_ok = None
        orphan_partial_flag = False
        if tap_complete and strict_minoc:
            minoc_ok = _minoc_fully_present(planes_by_product, cert_file, expected)
            if not minoc_ok:
                orphan_partial_flag = True

        tile_date = date_by_tile.get(tile)
        fully_consistent_cadc = tap_complete and (not strict_minoc or minoc_ok)

        # Hard gate (the ONLY leave_alone case): a live ingest<N> session
        # means do_ingest is still running. The reconciler never writes
        # during processing — the owner (do_ingest) will write its own
        # final state when it finishes. In every other case the
        # reconciler reconciles the DB against CADC: no wildcards, no
        # "needs human attention" sidelines. Persistent failures (e.g.
        # the sc2repo underflow on tile 11719) will loop between NULL
        # and IngestFailed; detection of such loops is the audit TODO.
        if session_alive:
            action = "leave_alone"
            leave_reason = "session_alive"
        elif current_status == "IngestedNoDate":
            # DB status is 'Ingested' but date column is NULL. Reconcile
            # against CADC: if CADC agrees, backfill the date; if CADC
            # disagrees, trust CADC and NULL the status so the pipeline
            # re-ingests. Transient TAP failure is NOT masqueraded as
            # "disagrees": _cadc_p3d_planes_by_tile raises on TAP error
            # and we abort the tick before reaching this branch.
            if fully_consistent_cadc:
                action = "backfill_date"
                leave_reason = None
            else:
                action = "null_retry"
                leave_reason = None
        elif fully_consistent_cadc:
            action = "flip_ingested"
            leave_reason = None
        elif tap_complete and strict_minoc and not minoc_ok:
            # Orphan-partial (TAP says 14/14 but minoc has 404s).
            action = "null_retry"
            leave_reason = None
        else:
            # tap_incomplete, for either IngestRunning (session died
            # mid-upload) or IngestFailed (never completed). Both
            # reconcile to NULL so the pipeline retries. If the
            # underlying failure is persistent, the next tick will
            # see the same incomplete state and null again; audit TODO
            # (future PR) will flag tiles stuck in this loop.
            action = "null_retry"
            leave_reason = None

        minoc_tag = "" if minoc_ok is None else f" minoc_ok={minoc_ok}"
        orphan_tag = "  [ORPHAN-PARTIAL]" if orphan_partial_flag else ""
        date_tag = (
            f"  date={tile_date}"
            if action in ("flip_ingested", "backfill_date") else ""
        )
        reason_tag = f"  reason={leave_reason}" if leave_reason else ""
        has_date_tag = f"  db_has_date={db_has_date}" if action == "flip_ingested" else ""
        print(f"[reconcile_ingest] tile {tile}: current={current_status}  "
              f"p3d={n_at_cadc}/{n_expected}{minoc_tag}  "
              f"session_alive={session_alive}  action={action}"
              f"{date_tag}{has_date_tag}{reason_tag}{orphan_tag}  "
              f"{'(DRY-RUN)' if dry_run else ''}")

        if dry_run:
            if action == "flip_ingested":
                if current_status == "IngestRunning":
                    flipped_running += 1
                else:
                    flipped_failed += 1
            elif action == "backfill_date":
                date_backfilled += 1
            elif action == "null_retry":
                nulled += 1
                if orphan_partial_flag:
                    orphan_partial += 1
            else:
                left_alone += 1
            continue

        try:
            if action == "flip_ingested":
                # Only touch what needs touching. If the DB already has
                # a date, skip the date PATCH and just flip the status.
                # If the date is missing, write date FIRST, status LAST,
                # so any failure before the status flip leaves the row
                # at IngestRunning/IngestFailed and the reconciler re-
                # picks it on the next tick.
                # tile_date fallback: TAP has 14/14 and minoc OK but no
                # parseable lastModified (extremely rare — ISO-8601
                # parse should always succeed on CAOM2). Use today's
                # date so the DB has a concrete value and the row
                # exits the stuck list rather than looping.
                if db_has_date:
                    _patch_status(conn, band_number, tile, "Ingested")
                else:
                    write_date = tile_date or datetime.now(tz=timezone.utc).date().isoformat()
                    _write_consistent_ingested(
                        conn, band_number, tile, write_date
                    )
                if current_status == "IngestRunning":
                    flipped_running += 1
                else:
                    flipped_failed += 1
            elif action == "backfill_date":
                # Status is already 'Ingested'; only the date is missing
                # (by definition of IngestedNoDate — db_has_date is False
                # here). Write just the date; do not re-flip status.
                write_date = tile_date or datetime.now(tz=timezone.utc).date().isoformat()
                _patch_date(conn, band_number, tile, write_date)
                date_backfilled += 1
            elif action == "null_retry":
                _patch_status(conn, band_number, tile, None)
                nulled += 1
                if orphan_partial_flag:
                    orphan_partial += 1
            else:
                left_alone += 1
        except Exception as e:  # noqa: BLE001 - keep going on a per-tile failure
            print(f"[reconcile_ingest] tile {tile}: PATCH failed: {e!r}")

    summary = {
        "flipped_from_running": flipped_running,
        "flipped_from_failed": flipped_failed,
        "date_backfilled": date_backfilled,
        "nulled": nulled,
        "left_alone": left_alone,
        "orphan_partial_nulled": orphan_partial,
        "stale_skaha_detected": len(stale_sessions),
        "stuck_total": len(stuck_by_status),
    }
    print(f"[reconcile_ingest] band {band_number}: summary {summary}")
    return summary
