"""
Checks POSSUM tile status (Cameron's survey overview google sheet) if 3D pipeline can be started.
Updates the POSSUM tile status (Cameron's survey overview google sheet) to "running" if 3D pipeline is submitted.

Should be executed on p1


A 3D pipeline run can be started if on the Survey Tiles - Band <number> sheet:

the aus_src column is not empty, and the 3d_pipeline column is empty

meaning that IQU cubes + MFS have been ingested into the CADC but 3D pipeline data not yet


Also checks CANFAR directory
/arc/projects/CIRADA/polarimetry/ASKAP/Tiles/
for the existence of the input IQU cubes + MFS image. For this, the script
create_symlinks.py should be run after possum_run_remote is executed that downloads the data
into time-blocked directories.



@author: Erik Osinga
"""
import argparse
import asyncio
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import astropy.table as at
import gspread
import numpy as np
from canfar.sessions import Session
from vos import Client

from automation import canfar_polling
from automation import database_queries as db
from possum_pipeline_control import util
from print_all_open_sessions import get_open_sessions

session = Session()

def get_tiles_for_pipeline_run_old(band_number, Google_API_token):
    """
    Get a list of tile numbers that should be ready to be processed by the 3D pipeline

    i.e.  'aus_src' column is not empty and '3d_pipeline' column is empty for the given band number.

    Args:
    band_number (int): The band number (1 or 2) to check.
    Google_API_token (str): The path to the Google API token JSON file.

    Returns:
    list: A list of tile numbers that satisfy the conditions.
    """
    # Authenticate and grab the spreadsheet
    gc = gspread.service_account(filename=Google_API_token)
    # Cameron's survey overview
    ps = gc.open_by_url(
        "https://docs.google.com/spreadsheets/d/1sWCtxSSzTwjYjhxr1_KVLWG2AnrHwSJf_RWQow7wbH0"
    )

    # Select the worksheet for the given band number
    tile_sheet = ps.worksheet(f"Survey Tiles - Band {band_number}")
    tile_data = tile_sheet.get_all_values()
    column_names = tile_data[0]
    tile_table = at.Table(np.array(tile_data)[1:], names=column_names)

    # Find the tiles that satisfy the conditions
    tiles_to_run = [
        row["tile_id"]
        for row in tile_table
        if row["aus_src"] != "" and row["3d_pipeline"] == ""
    ]

    return tiles_to_run

def get_canfar_tiles(band_number):
    client = Client()
    # force=True to not use cache
    # assumes directory structure doesnt change and symlinks are created
    if band_number == 1:
        canfar_tilenumbers = client.listdir(
            "vos://cadc.nrc.ca~arc/projects/CIRADA/polarimetry/ASKAP/Tiles/943MHz/",
            force=True,
        )
    elif band_number == 2:
        canfar_tilenumbers = client.listdir(
            "vos://cadc.nrc.ca~arc/projects/CIRADA/polarimetry/ASKAP/Tiles/1367MHz/",
            force=True,
        )
    else:
        raise ValueError(f"Band number {band_number} not defined")
    return canfar_tilenumbers

def launch_pipeline(tilenumber, band):
    # Launch the appropriate 3D pipeline script based on the band
    if band == "943MHz":
        command = [
            "python",
            "-m",
            "possum_pipeline_control.launch_3Dpipeline_band1",
            str(tilenumber),
        ]
    elif band == "1367MHz":
        command = [
            "python",
            "-m",
            "possum_pipeline_control.launch_3Dpipeline_band2",
            str(tilenumber),
        ]
        command = ""
        print(
            "Temporarily disabled launching band 2 because need to write that run script"
        )
    else:
        raise ValueError(f"Unknown band: {band}")

    print(f"Running command: {' '.join(command)}")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,               # line-buffered
        universal_newlines=True  # ensures \n splitting works across platforms
    )
    util.print_subprocess_output(process, command)

def update_status(tile_number, band, Google_API_token, status):
    """
    Update the status of the specified tile in Cameron's Google Sheet & the AUSSRC tile_state database.

    Args:
        tile_number (str): The tile number to update.
        band (str): The band of the tile. ('943MHz' or '1367MHz')
        Google_API_token (str): The path to the Google API token JSON file.
        status (str): The status to set in the '3d_pipeline' column.
    """
    # Make sure its not int
    tile_number = str(tile_number)

    # Authenticate and grab the spreadsheet
    gc = gspread.service_account(filename=Google_API_token)
    ps = gc.open_by_url(os.getenv("POSSUM_STATUS_SHEET"))

    # Select the worksheet for the given band number
    band_number = util.get_band_number(band)
    tile_sheet = ps.worksheet(f"Survey Tiles - Band {band_number}")
    tile_data = tile_sheet.get_all_values()
    column_names = tile_data[0]
    tile_table = at.Table(np.array(tile_data)[1:], names=column_names)

    # Find the row index for the specified tile number
    tile_index = None
    for idx, row in enumerate(tile_table):
        if row["tile_id"] == tile_number:
            tile_index = (
                idx + 2
            )  # +2 because gspread index is 1-based and we skip the header row
            break

    if tile_index is not None:
        # Update the status in the '3d_pipeline' column
        col_letter = gspread.utils.rowcol_to_a1(
            1, column_names.index("3d_pipeline") + 1
        )[0]
        # as of >v6.0.0 .update requires a list of lists
        tile_sheet.update(range_name=f"{col_letter}{tile_index}", values=[[status]])
        print(
            f"Updated tile {tile_number} status to {status} in '3d_pipeline' column in Google Sheet."
        )
        # Also update the DB
        conn = db.get_database_connection(test=False)
        db.update_3d_pipeline_table(
            tile_number, band_number, status, "3d_pipeline_val", conn
        )
        conn.close()
    else:
        print(f"Tile {tile_number} not found in the sheet.")

def check_download_running(jobname="3dtile-dl"):
    """
    Check whether a 3d pipeline tile download session (i.e. possum_run_remote) is running

    returns True if it is, False if not.
    """
    df_sessions = get_open_sessions()

    if len(df_sessions) == 0:
        # no sessions running
        return False

    # corresponds to jobname as set in launch_download_session()
    if (
        df_sessions[df_sessions["status"] == "Running"]["name"]
        .str.contains(jobname)
        .any()
        or df_sessions[df_sessions["status"] == "Pending"]["name"]
        .str.contains(jobname)
        .any()
    ):
        return True

    # didnt find any running jobs with the jobname
    return False


def read_last_download_launch_time(state_file: Path) -> datetime | None:
    """
    Read the last download launch time from a state file.

    Returns a timezone-aware UTC datetime, or None if the file is missing/unreadable.
    """
    try:
        text = state_file.read_text(encoding="utf-8").strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (FileNotFoundError, ValueError, OSError):
        return None


def write_last_download_launch_time(state_file: Path, when: datetime) -> None:
    """
    Persist the last download launch time as an ISO-8601 string (UTC).
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    when_utc = when.astimezone(timezone.utc)
    state_file.write_text(when_utc.isoformat(), encoding="utf-8")


def should_launch_download_session(
    *,
    download_running: bool,
    state_file: Path,
    min_age: timedelta = timedelta(days=1),
    now: datetime | None = None,
) -> bool:
    """
    Launch only if:
      - no download is currently running, AND
      - last launch time is missing OR older than min_age.
    """
    if download_running:
        return False

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    last_launch = read_last_download_launch_time(state_file)
    if last_launch is None:
        return True

    return (now_utc - last_launch) >= min_age


def launch_download_session(jobname="3dtile-dl"):
    # Template bash script to run
    args = "/arc/projects/CIRADA/polarimetry/software/POSSUMutils/cirada_software/3d_pipeline_tile_download_ingest.sh"
    print("Launching download session")
    print(f"Command: {args}")

    image = os.getenv("IMAGE")
    print(f"Image: {image}")
    # download can use flexible resources
    session_id = session.create(
        name=jobname.replace(
            "_", "-"
        ),  # Prevent Error 400: name can only contain alpha-numeric chars and '-'
        image=image,
        cores=None,  # flexible mode
        ram=None,  # flexible mode
        kind="headless",
        cmd="bash",
        args=args,
        replicas=1,
    )

    session_id_str = session_id_str = session_id[0] if len(session_id) > 0 else None

    print("Check sessions at https://ws-uv.canfar.net/skaha/v1/session")
    print(
        f"Check logs at https://ws-uv.canfar.net/skaha/v1/session/{session_id_str}?view=logs"
    )

    return session_id_str

def launch_create_symlinks(jobname="3dsymlinks"):
    """
    Launch session on CANFAR to create symbolic links after possum_run_remote has downloaded
    the tiles into "timeblocked" directories.

    This sorts the tiles into symbolic links in a much more readable format.
    """

    # Template bash script to run
    args = "/arc/projects/CIRADA/polarimetry/software/POSSUMutils/cirada_software/create_symlinks.sh"

    print("Launching symlinks session")
    print(f"Command: {args}")

    # image = "images.canfar.net/cirada/possumpipelineprefect-3.12:v2.0.2"
    image = os.getenv("IMAGE") # set in prefect deployment configuration

    # download can use flexible resources
    session_id = session.create(
        name=jobname.replace(
            "_", "-"
        ),  # Prevent Error 400: name can only contain alpha-numeric chars and '-'
        image=image,
        cores=None,  # flexible mode
        ram=None,  # flexible mode
        kind="headless",
        cmd="bash",
        args=args,
        replicas=1,
    )

    session_id_str = session_id_str = session_id[0] if len(session_id) > 0 else None

    print("Check sessions at https://ws-uv.canfar.net/skaha/v1/session")
    print(
        f"Check logs at https://ws-uv.canfar.net/skaha/v1/session/{session_id_str}?view=logs"
    )

    return session_id_str

def needs_prefect_db_backup(
    home_dir: str | Path,
    *,
    max_age_days: int = 14,
    backups_subdir: str = "prefect-backups",
    suffix: str = ".sql",
) -> bool:
    """
    Check for Prefect database backup files in <home_dir>/<backups_subdir>/.

    Returns True if:
      - the backups directory does not exist, or
      - no matching backup files exist, or
      - the newest backup is older than max_age_days.
    Otherwise returns False.
    """
    home_path = Path(home_dir).expanduser().resolve()
    backups_dir = home_path / backups_subdir

    if not backups_dir.is_dir():
        return True

    db_files = [
        p for p in backups_dir.iterdir() if p.is_file() and p.name.endswith(suffix)
    ]
    if not db_files:
        return True

    newest = max(db_files, key=lambda p: p.stat().st_mtime)
    newest_mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
    return newest_mtime < cutoff

async def launch_band1_3Dpipeline(database_config_path=None):
    """
    Check for Band 1 tiles that are ready to be processed with the 3D pipeline and launch the pipeline for the first available tile.
    3D pipeline can be launched if the tile is processed by AUSsrc (aus_src column not empty) but 3D pipeline not yet run (3d_pipeline column empty).
    """
    band = "943MHz"
    # on p1/aussrc
    Google_API_token = util.initiate_possum_status_sheet_and_token()


    dl_jobname = "3dtile-dl"
    # First check whether a download session is running (i.e. possum_run_remote)
    # Get information about currently open sessions
    download_running = check_download_running(dl_jobname)

    state_file = Path.home() / ".possum" / "last_3dtile_download_launch_utc.txt"
    now_utc = datetime.now(timezone.utc)

    # Then if the download session was not running, check when we last launched one
    if should_launch_download_session(
        download_running=download_running,
        state_file=state_file,
        min_age=timedelta(days=1),
        now=now_utc,
    ):
        print("No download job is running, and last launch was >= 1 day ago.")
        launch_download_session(dl_jobname)
        write_last_download_launch_time(state_file, now_utc)
        
        # also launch a job to create new symlinks since the previous download job finished.
        launch_create_symlinks()

    else:
        if download_running:
            print("A download job (possum_run_remote) is already running.")
        else:
            last_launch = read_last_download_launch_time(state_file)
            if last_launch is None:
                print("No download job is running, and last launch time is unknown; Shouldnt happen.")
            else:
                age = now_utc - last_launch
                remaining = timedelta(days=1) - age
                print(
                    "No download job is running, but last launch was too recent "
                    f"({age} ago). Next allowed in ~{remaining}."
                )


    # Check whether we've made a backup of the database less than two weeks ago
    if needs_prefect_db_backup(Path.home()):
        run_prefect_db_backup()


    # Check database for band 1 tiles that have been processed by AUSSRC
    # but not yet processed with 3D pipeline
    conn = db.get_database_connection(test=False, database_config_path=database_config_path)
    # We are getting the tiles from the DB instead of the sheet now
    tile_numbers = db.get_tiles_for_pipeline_run(conn, band_number=1)
    # tile_numbers is a list of single-element tuples, convert to 1D list
    tile_numbers = [str(tup[0]) for tup in tile_numbers]

    conn.close()

    # Also check whether the tiles have been downloaded to CANFAR
    canfar_tilenumbers = get_canfar_tiles(band_number=1)

    if len(tile_numbers) > 0:
        print(
            f"Found {len(tile_numbers)} tiles in AUSSRC database in Band 1 ready to be processed with 3D pipeline"
        )
        print(f"On CANFAR, found {len(canfar_tilenumbers)} tiles downloaded for Band 1")

        if len(tile_numbers) > len(canfar_tilenumbers):
            tiles_in_cadc_not_canfar = set(tile_numbers) - set(canfar_tilenumbers)
            print(
                f"{len(tiles_in_cadc_not_canfar)} tiles processed by AUSSRC but not on CANFAR"
            )
            print(f"    First 5: {list(tiles_in_cadc_not_canfar)[:5]}")

        # else:
        # This set difference also returns the tiles on CANFAR that are already fully 3D processed, so not so useful
        #     tiles_on_canfar_not_cadc = set(canfar_tilenumbers) - set(tile_numbers)
        #     print(f"{len(tiles_on_canfar_not_cadc)} tiles on CANFAR but not processed by AUSSRC: {tiles_on_canfar_not_cadc}")

        tiles_on_both = set(tile_numbers) & set(canfar_tilenumbers)
        print(
            f"\nNumber of tiles both ready according to AUSSRC and available on CANFAR: {len(tiles_on_both)}"
        )
        print(f"    First 5: {list(tiles_on_both)[:5]}")

        if tiles_on_both:
            # Launch the first tile number (assumes this script will be called many times)
            tilenumber = list(tiles_on_both)[0]
            print(f"\nLaunching headless job for 3D pipeline with tile {tilenumber}")

            # check if there are any stuck jobs before launching new ones
            reconcile_summary = await canfar_polling.reconcile_running_prefect_with_canfar_task()

            # Launch the pipeline
            launch_pipeline(tilenumber, band)

            # Update the status to "Running" in the aussrc database and the google sheet
            update_status(tilenumber, band, Google_API_token, "Running")

        else:
            print("No tiles are available on both CADC and CANFAR.")
    else:
        print("Found no tiles ready to be processed.")

    print("3D pipeline check and launch complete.")
    print("\n")  

def run_prefect_db_backup():
    bkpscript = "backup_prefect_server.sh"
    print(f"Prefect database should be backed up. Running {bkpscript}")

    # create file name for backup
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    OUTDIR = Path.home() / "prefect-backups"
    OUTDIR.mkdir(parents=True, exist_ok=True)
    db_backup = OUTDIR / f"prefect-{ts}.sql"
    
    cmd = ["bash", bkpscript, str(db_backup)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.splitlines():
            print(f"[STDOUT] {line}")
    if result.stderr:
        for line in result.stderr.splitlines():
            print(f"[STDERR] {line}")
    # Check return code manually
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd,
            output=result.stdout,
            stderr=result.stderr
        )
    # --- Copy to CANFAR ---
    print("Copying the backup to CANFAR...")
    client = Client()
    client.copy(str(db_backup), "arc:projects/CIRADA/polarimetry/software/prefect-backups")
    print("Backup completed successfully")    

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Checks POSSUM validation status ('POSSUM Pipeline validation' google sheet) if 3D pipeline outputs can be ingested."
    )
    parser.add_argument(
        "--database_config_path",
        type=str,
        help="Path to .env file with database connection parameters.",
    )
    args = parser.parse_args()
    
    from prefect.utilities.asyncutils import run_coro_as_sync
    run_coro_as_sync(launch_band1_3Dpipeline(args.database_config_path))

