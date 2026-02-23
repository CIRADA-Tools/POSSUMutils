"""
Poll Prefect RUNNING flow-runs and ensure a corresponding CANFAR session is RUNNING.

This module is necessary because if a CANFAR session dies unexpectedly
(e.g. due to CANFAR outage, or user manually killing it), it keeps the status "runnning" indefinitely.

You can call reconcile_running_prefect_with_canfar_task() as a Prefect task inside your flow 
before submitting a new CANFAR job, to ensure prefect consistency with CANFAR before launching new jobs.

EXAMPLE:

    @flow
    async def submit_canfar_job():
        # first reconcile
        summary = await reconcile_running_prefect_with_canfar_task()
        # then submit job
        launch_session(...)

    # using asyncio.run() to manage the event loop
    asyncio.run(submit_canfar_job())

Rules:
- Only flow-runs in Prefect state RUNNING are checked.
- Only flow-runs tagged with "canfar_session:<id>" are checked.
- CANFAR sessions are considered valid only if their status == "Running".
- If CANFAR API/data fetch fails, do not fail any flow-runs for that cycle.
- If a tagged RUNNING flow-run's CANFAR session is missing/not Running for MAX_RETRY consecutive polls,
  mark the flow-run FAILED.

  
  TODO: 
  - Figure out a way to restart the CANFAR job when a flow-run is marked as failed
      this will depend on the type of CANFAR job that we were running though.
  
"""
from __future__ import annotations
import os
import pprint

from canfar.sessions import Session
from dataclasses import dataclass

from prefect import task, flow
from prefect.client.orchestration import get_client
from prefect.server.schemas.filters import FlowRunFilter, FlowRunFilterState
from prefect.server.schemas.states import StateType
from prefect.states import Failed

from print_all_open_sessions import get_open_sessions
from vos import Client

TAG_PREFIX = "canfar_session:"
VOS_LOG_FOLDER = "arc:projects/CIRADA/polarimetry/ASKAP/Pipeline_logs/canfar_logs"


@dataclass(frozen=True)
class ReconcileResult:
    """Result of a single reconciliation run."""
    canfar_ok: bool
    running_flow_runs: int
    checked_tagged_running: int
    failed_marked: int
    skipped_untagged: int
    missing_or_not_running: int


def extract_session_id_from_tags(tags: list[str]) -> str | None:
    """Extract CANFAR session ID from Prefect flow-run tags."""
    for t in tags or []:
        if isinstance(t, str) and t.startswith(TAG_PREFIX):
            sid = t[len(TAG_PREFIX) :].strip()
            return sid or None
    return None


def build_running_canfar_session_set(session_df) -> set[str]:
    """
    Expects a dataframe with columns: 'status' and 'id' (per get_open_sessions).
    Returns session IDs whose status == 'Running' (case-insensitive).
    """
    if session_df is None or len(session_df) == 0:
        return set()
    if "status" not in session_df.columns or "id" not in session_df.columns:
        return set()

    running = session_df[session_df["status"].astype(str).str.lower() == "running"]
    return {str(x).strip() for x in running["id"].dropna().tolist() if str(x).strip()}


async def _fetch_prefect_running_flow_runs(limit: int = 200):
    """Fetch flow-runs in RUNNING state from Prefect API, up to the specified limit."""
    async with get_client() as client:
        fr_filter = FlowRunFilter(
            state=FlowRunFilterState(type={"any_": [StateType.RUNNING]})
        )
        return await client.read_flow_runs(flow_run_filter=fr_filter, limit=limit)

async def _fetch_prefect_completed_flow_runs(limit: int = 200):
    """Fetch flow-runs in COMPLETED state from Prefect API, up to the specified limit."""
    async with get_client() as client:
        fr_filter = FlowRunFilter(
            state=FlowRunFilterState(type={"any_": [StateType.COMPLETED]})
        )
        return await client.read_flow_runs(flow_run_filter=fr_filter, limit=limit)


async def _fail_flow_run(flow_run_id, reason: str) -> None:
    """Mark the specified flow-run as FAILED with the given reason/message."""
    async with get_client() as client:
        await client.set_flow_run_state(
            flow_run_id=flow_run_id,
            state=Failed(message=reason),
        )


async def reconcile_running_prefect_with_canfar(limit: int = 200) -> ReconcileResult:
    """
    Single-shot reconciliation.
    """
    print("Starting reconciliation of Prefect RUNNING flow-runs with CANFAR sessions...")

    # Grab CANFAR truth
    try:
        session_df = get_open_sessions()
        running_canfar_sessions = build_running_canfar_session_set(session_df)
        canfar_ok = True
    except Exception:
        # Outage-safe: don't fail any flow-runs if CANFAR can't be queried
        running_canfar_sessions = set()
        canfar_ok = False

    # Grab Prefect truth
    running_flow_runs = await _fetch_prefect_running_flow_runs(limit=limit)

    skipped_untagged = 0
    checked_tagged_running = 0
    missing_or_not_running = 0
    failed_marked = 0

    if not canfar_ok: # CANFAR API is down, so we cannot reliably check anything.

        # Still return counts but do not fail anything
        for fr in running_flow_runs:
            if extract_session_id_from_tags(fr.tags or []):
                checked_tagged_running += 1
            else:
                skipped_untagged += 1

        print("CANFAR API/data fetch failed. Skipping reconciliation and not failing any flow-runs for this cycle.")

        return ReconcileResult(
            canfar_ok=False,
            running_flow_runs=len(running_flow_runs),
            checked_tagged_running=checked_tagged_running,
            failed_marked=0,
            skipped_untagged=skipped_untagged,
            missing_or_not_running=0,
        )

    # reconcile
    for fr in running_flow_runs:
        session_id = extract_session_id_from_tags(fr.tags or [])
        if not session_id:
            skipped_untagged += 1
            continue

        checked_tagged_running += 1

        if session_id not in running_canfar_sessions:
            missing_or_not_running += 1
            reason = (
                "CANFAR session missing or not RUNNING while Prefect flow-run is RUNNING. "
                f"session_id={session_id}."
            )
            await _fail_flow_run(fr.id, reason)
            failed_marked += 1

    print("Reconciliation summary:")
    print(f"  CANFAR OK: {canfar_ok}")
    print(f"  Running flow-runs: {len(running_flow_runs)}")
    print(f"  Checked tagged running: {checked_tagged_running}")
    print(f"  Failed marked: {failed_marked}")
    print(f"  Skipped untagged: {skipped_untagged}")
    print(f"  Missing or not running: {missing_or_not_running}")

    return ReconcileResult(
        canfar_ok=True,
        running_flow_runs=len(running_flow_runs),
        checked_tagged_running=checked_tagged_running,
        failed_marked=failed_marked,
        skipped_untagged=skipped_untagged,
        missing_or_not_running=missing_or_not_running,
    )


@task(name="Reconcile Prefect RUNNING flow-runs with CANFAR sessions")
async def reconcile_running_prefect_with_canfar_task(limit: int = 200) -> dict:
    """
    Prefect task wrapper if you want to call it inside another flow/task graph.
    Returns a JSON-serializable dict.
    """
    result = await reconcile_running_prefect_with_canfar(limit=limit)
    return {
        "canfar_ok": result.canfar_ok,
        "running_flow_runs": result.running_flow_runs,
        "checked_tagged_running": result.checked_tagged_running,
        "missing_or_not_running": result.missing_or_not_running,
        "failed_marked": result.failed_marked,
        "skipped_untagged": result.skipped_untagged,
    }

@flow(name="Get completed sessions logs and push to Canfar directory", log_prints=True)
def get_completed_session_logs():
    """
    Get recently completed CANFAR sessions and push their logs to Canfar directory.
    This includes Succeeded, Completed, Failed or Error sessions. 
    The directory is /arc/projects/CIRADA/polarimetry/ASKAP/Pipeline_logs/canfar_logs.
    """
    session = Session()
    statuses = ["Succeeded", "Completed", "Failed", "Error"]
    # Get all session ids of completed sessions
    all_session_ids = [
        s["id"]
        for status in statuses
        for s in (session.fetch(kind="headless", status=status) or [])
    ]
    for session_id in all_session_ids:
        # Get logs for each session
        get_logs(session, session_id)

def push_logs_to_canfar(tmp_file):
    """
    Copy Canfar session logs to Canfar directory and remove local file
    Args:
        tmp_file: Local log file to copy and delete
    """
    client = Client()
    
    # Ensure remote directory exists
    try:
        client.mkdir(VOS_LOG_FOLDER)
    except:
        pass  # Directory might already exist
    
    try:
        remote_file = f"{VOS_LOG_FOLDER}/{tmp_file}"
        client.copy(tmp_file, remote_file)
        print(f"Upload Canfar log to {remote_file} completed successfully!\n")
    except Exception as e:
        print(f"Failed to upload {tmp_file} to Canfar: {e}\n")
        return
    
    # Clean up local file
    os.remove(tmp_file)    

def log_exists_in_canfar_dir(session_id):
    """
    Check if log already exists in Canfar directory.
    Args:
        session_id: ID of the session
    Returns:
        True if log exists, False otherwise
    """
    client = Client()
    file_name = f"{VOS_LOG_FOLDER}/{session_id}.log"
    try:
        return client.isfile(file_name)
    except:
        # Folder doesn't exist yet
        return False
        

def get_logs(session, session_id):
    """
    Get logs for a Canfar session.

    Args:
        session: Canfar session object
        session_id: ID of the session to monitor
        output_file: File to write logs to
    """
    if log_exists_in_canfar_dir(session_id):
        return
    
    tmp_file = f"{session_id}.log"
    
    with open(tmp_file, "w") as f:
        # Fetch logs before exiting
        logs_dict = session.logs(session_id).get(session_id)
        f.write(pprint.pformat(logs_dict, depth=4))
        f.flush
    push_logs_to_canfar(tmp_file)       

