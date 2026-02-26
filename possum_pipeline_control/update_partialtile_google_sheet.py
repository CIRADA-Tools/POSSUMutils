"""
This module checks the AUSSRC database for fields that are observed and processed by AUSSRC.
and thus ready for the 1D Partial Tile pipeline processing

i.e. the table possum.observation_state_band1 has to have
    single_sb_1d_pipeline is NULL/empty
    AND cube_state = 'COMPLETED'



It then launches a job on CANFAR via

    "check_status_and_launch_1Dpipeline_PartialTiles.py 'pre'"

that will download the tiles in a CANFAR job and populate the possum.partial_tile_1d_pipeline_band1 database.
"""

#!/usr/bin/env python
import argparse
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import astropy.table as at
from canfar.sessions import Session
import gspread
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from automation import database_queries as db
from possum_pipeline_control import util



def get_sheet_table(band):
    """
    Connects to the POSSUM Status Monitor Google Sheet and returns a sub-table
    containing only the rows where the 'sbid' and 'aus_src' fields are not empty and 'single_SB_1D_pipeline' is empty.

    Uses the following Google API token on p1:
        /home/erik/.ssh/psm_gspread_token.json

    And the sheet URL:
        https://docs.google.com/spreadsheets/d/1sWCtxSSzTwjYjhxr1_KVLWG2AnrHwSJf_RWQow7wbH0

    The worksheet is selected based on the band:
        '1' if band == '943MHz', otherwise '2'.
    """
    # POSSUM Status Monitor
    # Google_API_token = os.getenv("POSSUM_STATUS_TOKEN")
    Google_API_token = util.initiate_possum_status_sheet_and_token()
    # Authenticate and grab the spreadsheet
    gc = gspread.service_account(filename=Google_API_token)
    ps = gc.open_by_url(os.getenv("POSSUM_STATUS_SHEET"))

    # Select the worksheet for the given band number
    if band == "943MHz":
        band_number = "1"
    elif band == "1367MHz":
        band_number = "2"
    else:
        raise ValueError("Band must be either '943MHz' or '1367MHz'")
    tile_sheet = ps.worksheet(f"Survey Fields - Band {band_number}")
    tile_data = tile_sheet.get_all_values()
    column_names = tile_data[0]
    tile_table = at.Table(np.array(tile_data)[1:], names=column_names)

    # Find all rows that have "sbid" not empty and "aus_src" not empty and 'single_SB_1D_pipeline' empty
    mask = (
        (tile_table["sbid"] != "")
        & (tile_table["aus_src"] != "")
        & (tile_table["single_SB_1D_pipeline"] == "")
    )
    ready_table = tile_table[mask]
    return ready_table, tile_table


def get_ready_fields(band: str) -> tuple[at.Table, at.Table]:
    """
    Get fields ready for single SB partial tile pipeline processing from the database.

    Returns a list of fieldnames ready and the full pipeline summary table from Cameron as astropy Tables.

    the latter is only used to make progress plots.
    """

    if band == "943MHz":
        band_number = "1"
    elif band == "1367MHz":
        band_number = "2"
    else:
        raise ValueError("Band must be either '943MHz' or '1367MHz'")
    
    conn = db.get_database_connection(test=False)
    ready_table = db.get_fields_ready_single_SB_pipeline(band_number, conn)
    conn.close()
    # get rid of the ASKAP- prefix in sbid for easier matching with google sheet
    sbids = [row["sbid"].strip("ASKAP-") for row in ready_table]
    ready_table["sbid"] = sbids

    ready_table_sheet, full_table_sheet = get_sheet_table(band)

    if len(ready_table_sheet) != len(ready_table):
        print("Warning: Mismatch between database and Google Sheet for ready fields.")
        print(
            f"Database ready fields: {len(ready_table)}, Google Sheet ready fields: {len(ready_table_sheet)}"
        )
        print("    Fields in database but not in Google Sheet:")
        sheet_fieldnames = set(ready_table_sheet["name"])
        for fn in ready_table["name"]:
            if fn not in sheet_fieldnames:
                print(f"     - {fn}")
        print("    Fields in Google Sheet but not in database:")
        for fn in sheet_fieldnames:
            if fn not in ready_table["name"]:
                print(f"     - {fn}")

    ## getting full table from the database works also, but its a different table structure than Camerons sheet.
    # full_table: at.Table = db.get_full_table_single_SB_pipeline(band_number, conn, as_table=True)

    return ready_table, full_table_sheet


def launch_pipeline_command(fieldname, sbid):
    """
    Launches the 1D pipeline pre-or-post script for a given field and sbid.

    The command executed is:
        python -m possum_pipeline_control.launch_1Dpipeline_PartialTiles_band1_pre_or_post {fieldname} {sbid} pre
    """
    command = f"python -m possum_pipeline_control.launch_1Dpipeline_PartialTiles_band1_pre_or_post {fieldname} {sbid} pre"
    print(f"Executing command: {command}")
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,               # line-buffered
        universal_newlines=True  # ensures \n splitting works across platforms
    )
    util.print_subprocess_output(process, command)


def extract_date(entry):
    # Remove extra whitespace
    entry = entry.strip()
    # Use regex to match the exact pattern "PartialTiles(BI) - YYYY-MM-DD"
    # This regex allows for optional whitespace around the hyphen
    match = re.match(r"^PartialTiles(?:BI)?\s*-\s*(\d{4}-\d{2}-\d{2})$", entry)

    if match:
        return match.group(1)
    else:
        return np.nan


def create_progress_plot(full_table):
    """to be run on p1

    Create progress plots for the 1D pipeline:
     - A scatter plot per field.
     - A cumulative plot showing the total number of fields processed vs. date.
    """
    # how many SBids are observed
    n_total_observed_incl_reject = np.sum(full_table["sbid"] != "")
    print(
        f"Total number of observed fields (including REJECTED): {n_total_observed_incl_reject}"
    )

    # Remove rows where the "validated" column contains "REJECTED"
    full_table = full_table[
        ["REJECTED" not in str(val).upper() for val in full_table["validated"]]
    ]

    # Remove rows where the row is equal to ''
    full_table = full_table[
        [
            not all(str(val).strip() == "" for val in row)
            for row in full_table["validated"]
        ]
    ]

    n_total_observed = np.sum(full_table["sbid"] != "")
    print(
        f"Number of observed fields (excluding REJECTED): {n_total_observed} / {n_total_observed_incl_reject}"
    )

    # Extract validated times
    valid_times = [
        datetime.strptime(t, "%Y-%m-%d") for t in full_table["validated"] if pd.notna(t)
    ]

    # Extract finish times using helper
    full_table["pipeline_finish_time"] = [
        extract_date(x) for x in full_table["single_SB_1D_pipeline"]
    ]

    # Remove rows where pipeline_finish_time is NaN
    # Note: If extract_date returns np.nan for invalid entries, use pd.isna to filter
    full_table = full_table[(full_table["pipeline_finish_time"] != "nan")]

    # Convert finish times to datetime objects
    times = [
        datetime.strptime(t, "%Y-%m-%d") for t in full_table["pipeline_finish_time"]
    ]

    # ----------------------------
    # 1. Per-field Scatter Plot
    # ----------------------------
    plt.figure(figsize=(10, 6))
    plt.scatter(times, full_table["name"], c="blue", alpha=0.5)
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    plt.gca().xaxis.set_major_locator(mdates.DayLocator())
    plt.title("1D Pipeline Progress")
    plt.xlabel("Finish Time")
    plt.ylabel("Field Name")
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.grid()
    if not Path("./plots/").exists():
        Path("./plots/").mkdir()
    plt.savefig("./plots/1D_pipeline_progress_per_field.png")
    plt.show()
    plt.close()

    # ----------------------------
    # 2. Cumulative Progress Plot
    # ----------------------------
    # Create a DataFrame with the finish times
    df = pd.DataFrame({"date": times})
    # Normalize dates to midnight (in case there are time differences)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    # Count how many fields were processed each day
    daily_counts = df.groupby("date").size().sort_index()
    # Calculate the cumulative sum of fields processed over time
    cumulative_counts = daily_counts.cumsum()

    plt.figure(figsize=(10, 6))
    plt.plot(
        cumulative_counts.index, cumulative_counts.values, marker="o", linestyle="-"
    )
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    plt.gca().xaxis.set_major_locator(mdates.DayLocator())
    plt.title(
        f"Cumulative 1D Pipeline Progress as of {datetime.now().strftime('%Y-%m-%d')}"
    )
    plt.xlabel("Date")
    plt.ylabel("Cumulative Fields Processed")
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.grid()
    plt.savefig("./plots/1D_pipeline_progress_cumulative.png")
    plt.show()
    plt.close()

    # ----------------------------
    # 3. Fields vs Days Since Start Plot
    # ----------------------------
    # compute days since first processed field
    start_date = cumulative_counts.index.min()
    days_since_start = (cumulative_counts.index - start_date).days
    counts = cumulative_counts.values

    # estimate remaining days to reach n_total_observed fields
    elapsed = days_since_start[-1]
    processed = counts[-1]
    rate_per_day = processed / elapsed if elapsed > 0 else np.nan
    days_left = (
        (n_total_observed - processed) / rate_per_day
        if rate_per_day and rate_per_day > 0
        else np.nan
    )

    plt.figure(figsize=(10, 6))
    plt.plot(days_since_start, counts, marker="o", linestyle="-")
    plt.title(f"Fields Processed vs Days Since {start_date:%Y-%m-%d}")
    plt.xlabel("Days Since Start")
    plt.ylabel("Cumulative Fields Processed")
    # plt.xticks(days_since_start)
    ax = plt.gca()
    ax.xaxis.set_major_locator(ticker.MultipleLocator(5))
    plt.tight_layout()
    plt.grid()

    # annotate estimated days left
    txt = f"~{days_left:.1f} days left to reach {n_total_observed} fields"
    plt.gca().text(0.05, 0.95, txt, transform=plt.gca().transAxes, va="top")

    plt.savefig("./plots/fields_vs_days_since_start.png")
    plt.show()
    plt.close()

    # ----------------------------
    # 4. Validated vs Observing Start Plot
    # ----------------------------

    # Determine observing start date
    obs_start = min(valid_times)

    # Build cumulative counts for validated
    df_val = pd.DataFrame({"date": valid_times})
    df_val["date"] = pd.to_datetime(df_val["date"]).dt.normalize()
    daily_val = df_val.groupby("date").size().sort_index()
    cum_val = daily_val.cumsum()

    # Compute days since observing start for both series
    days_val = (cum_val.index - obs_start).days
    days_proc_obs = (cumulative_counts.index - obs_start).days
    proc_counts = cumulative_counts.values
    val_counts = cum_val.values

    # Plot both processed and validated vs days since observing start
    plt.figure(figsize=(10, 6))
    plt.plot(days_proc_obs, proc_counts, marker="o", linestyle="-", label="Processed")
    plt.plot(days_val, val_counts, marker="s", linestyle="--", label="Validated")
    ax = plt.gca()

    # --- Linear fits & intersection (full-range) ---
    slope_proc, intercept_proc = np.polyfit(days_proc_obs, proc_counts, 1)
    slope_val, intercept_val = np.polyfit(days_val, val_counts, 1)

    if slope_proc != slope_val:
        x_int_full = (intercept_val - intercept_proc) / (slope_proc - slope_val)
        days_left_full = x_int_full - days_proc_obs[-1]
    else:
        x_int_full = np.nan
        days_left_full = np.nan

    # Extrapolate both lines out to intersection (full-range)
    x_ext_full = (
        np.arange(0, int(np.ceil(x_int_full)) + 1)
        if np.isfinite(x_int_full)
        else np.arange(0, max(days_proc_obs.max(), days_val.max()) + 1)
    )
    y_ext_val_full = slope_val * x_ext_full + intercept_val

    # only start processing line after processing actually started (full-range)
    startday = days_proc_obs[0]
    x_ext_proc_full = np.arange(startday, x_ext_full[-1] + 1)
    y_ext_proc_full = slope_proc * x_ext_proc_full + intercept_proc

    plt.plot(x_ext_proc_full, y_ext_proc_full, linestyle=":", label="Proc trend")
    plt.plot(x_ext_full, y_ext_val_full, linestyle=":", label="Val trend")

    # --- 60-day window trends & intersection ---
    latest_date = max(cumulative_counts.index.max(), cum_val.index.max())
    cutoff_60d = latest_date - pd.Timedelta(days=60)

    # recent processed
    recent_proc = cumulative_counts[cumulative_counts.index >= cutoff_60d]
    recent_val = cum_val[cum_val.index >= cutoff_60d]

    # Require at least 2 points to fit; otherwise skip
    if (recent_proc.size >= 2) and (recent_val.size >= 2):
        days_proc_60 = (recent_proc.index - obs_start).days
        counts_proc_60 = recent_proc.values

        first_day_60d_ago = days_proc_60[0]

        days_val_60 = (recent_val.index - obs_start).days
        counts_val_60 = recent_val.values

        slope_proc_60, intercept_proc_60 = np.polyfit(days_proc_60, counts_proc_60, 1)
        slope_val_60, intercept_val_60 = np.polyfit(days_val_60, counts_val_60, 1)

        if slope_proc_60 != slope_val_60:
            x_int_60 = (intercept_val_60 - intercept_proc_60) / (
                slope_proc_60 - slope_val_60
            )
            days_left_60 = x_int_60 - days_proc_obs[-1]
        else:
            x_int_60 = np.nan
            days_left_60 = np.nan

        # Extrapolate dashed 60-day trend lines
        x_ext_60 = (
            np.arange(first_day_60d_ago, int(np.ceil(x_int_60)) + 1)
            if np.isfinite(x_int_60)
            else np.arange(
                first_day_60d_ago, max(days_proc_obs.max(), days_val.max()) + 1
            )
        )

        # show slope over last 60 days
        x_ext_proc_60 = x_ext_60[x_ext_60 >= first_day_60d_ago]
        y_ext_proc_60 = slope_proc_60 * x_ext_proc_60 + intercept_proc_60
        y_ext_val_60 = slope_val_60 * x_ext_proc_60 + intercept_val_60

        plt.plot(x_ext_proc_60, y_ext_proc_60, linestyle="--", label="Proc trend (60d)")
        plt.plot(x_ext_60, y_ext_val_60, linestyle="--", label="Val trend (60d)")
    else:
        x_int_60 = np.nan
        days_left_60 = np.nan

    # --- Annotations ---
    txt_full = f"Days until processing catches up with validated observations: {days_left_full:.1f}"
    ax.text(0.05, 0.95, txt_full, transform=ax.transAxes, va="top")

    txt_60 = f"Using last 60 days: {days_left_60:.1f} days"
    ax.text(0.05, 0.90, txt_60, transform=ax.transAxes, va="top")

    txt_summary = f"Fields processed: {proc_counts[-1]} / {n_total_observed}"
    ax.text(0.05, 0.85, txt_summary, transform=ax.transAxes, va="top")

    # --- Smarter x-axis locator & limits ---
    xmin = -30
    # include all relevant candidates: observed maxima and both intersections (if finite)
    candidates = [days_proc_obs.max(), days_val.max()]
    if np.isfinite(x_int_full):
        candidates.append(x_int_full)
    if np.isfinite(x_int_60):
        candidates.append(x_int_60)

    xmax = int(np.ceil(max(candidates))) if len(candidates) > 0 else 0
    xmax = max(xmax, 0)  # ensure non-negative right bound

    ax.set_xlim(xmin, xmax)

    # 20 major intervals from -30 to xmax
    n_intervals = 20
    step = (xmax - xmin) / n_intervals if n_intervals > 0 else 1
    # Guard against zero/negative step
    step = step if step > 0 else 1
    # round to nearest 10
    step = np.round(step / 10, decimals=0) * 10
    ax.xaxis.set_major_locator(ticker.MultipleLocator(step))

    ax.set_xlabel("Days since observing start")
    ax.set_ylabel("Cumulative count")
    ax.legend()

    # Also plot a vline at today
    today_days_since_start = (datetime.now() - obs_start).days
    plt.axvline(
        x=today_days_since_start,
        color="gray",
        linestyle="--",
        label=f"Today ({today_days_since_start} days)",
    )

    plt.title(
        f"Fields Processed & Validated vs Days Since Observing Start ({obs_start:%Y-%m-%d})"
    )
    plt.xlabel("Days Since Observing Start")
    plt.ylabel("Cumulative Fields")
    plt.legend(loc="center left", ncol=3)
    plt.tight_layout()
    plt.grid()
    plt.savefig("./plots/processed_validated_vs_days_since_observing_start.png")
    plt.show()
    plt.close()


def launch_collate_job():
    """
    Launches the collate job for the 1D pipeline. Once per day
    """
    session = Session()


    # e.g. for band 1
    basedir = "/arc/projects/CIRADA/polarimetry/pipeline_runs/partial_tiles/943MHz/"
    # Template bash script to run
    args = f"/arc/projects/CIRADA/polarimetry/software/POSSUMutils/cirada_software/collate_1Dpipeline_PartialTiles.sh {basedir}"

    print("Launching collate job")
    print(f"Command: bash {args}")

    run_name = "collate"
    image = os.getenv("IMAGE")
    # good default values
    cores = 4
    # ram will have to grow as catalogue grows...
    ram = 56  # Check allowed values at canfar.net/science-portal

    session_id = session.create(
        name=run_name.replace(
            "_", "-"
        ),  # Prevent Error 400: name can only contain alpha-numeric chars and '-'
        image=image,
        cores=cores,
        ram=ram,
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

    if session_id_str is not None:
        print(f"Session launched successfully with ID: {session_id_str}")
    else:
        raise ValueError("Failed to launch session. No session ID returned. CANFAR is down?")

    return session_id_str

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update Partial Tile Google Sheet")
    parser.add_argument(
        "--band",
        choices=["943MHz", "1367MHz"],
        help="The frequency band of the tile",
        default="943MHz",
    )
    parser.add_argument(
        "--database_config_path",
        type=str,
        help="Path to .env file with database connection parameters.",
    )
    args = parser.parse_args()

    band = args.band

    # load env for google spreadsheet constants
    if args.database_config_path:
        load_dotenv(dotenv_path=args.database_config_path)
        # Google_API_token = os.getenv("POSSUM_STATUS_SHEET")
    # consider chmod 600 <POSSUM_STATUS_TOKEN_FILE> to prevent access!!!

    # Get fields ready for processing according to the AUSSRC database.
    ready_table, full_table = get_ready_fields(band)
    print(
        f"Found {len(ready_table)} fields ready for single SB partial tile pipeline processing in band {band}"
    )

    # If the plot hasnt been updated in a day, re-run the plot
    local_file = "./plots/1D_pipeline_progress_cumulative.png"
    Path('./plots/').mkdir(exist_ok=True)

    if not os.path.exists(local_file):
        print("Creating 1D pipeline progress plot")
        # create progress plot from Cameron's status sheet.
        create_progress_plot(full_table)
    else:
        file_mod_time = os.path.getmtime(local_file)
        if (time.time() - file_mod_time) > 86400:  # 86400 seconds = 1 day
            print("Updating 1D pipeline progress plot")
            create_progress_plot(full_table)

    # Loop over each row in the returned table to launch the pipeline command.
    # The 'fieldname' is taken from the column "name" with "EMU_" stripped if present.
    # The sbid is taken from the column "sbid".
    for row in ready_table:
        fieldname = row["name"]
        sbid = str(row["sbid"])

        if fieldname.startswith("EMU_"):
            fieldname = fieldname[len("EMU_") :]
        if fieldname.startswith("WALLABY_"):
            fieldname = fieldname[len("WALLABY_") :]

        # Launch the job that downloads the tiles and populates the partial tile database
        launch_pipeline_command(fieldname, sbid)

        ## NOTE: moved to do this in the launched script to make sure it only happens if the job is actually launched
        # # update the status in Cameron's spreadsheet
        # status_to_put = "PartialTiles - Running"
        # update_status_spreadsheet(fieldname, sbid, band, Google_API_token, status_to_put, 'single_SB_1D_pipeline')

        break  # only do one every time the script is called
