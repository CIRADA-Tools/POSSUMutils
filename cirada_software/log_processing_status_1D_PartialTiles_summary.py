import argparse
import ast
import glob
import os
from time import sleep

import astropy.table as at
import gspread
import numpy as np
from dotenv import load_dotenv
from prefect import flow, task

from automation import possum_api_client as rest_api
from possum_pipeline_control import util

"""
Usage: python log_processing_status.py fieldstr sbid tilestr

where tilestr = always a list of 4 tiles e.g. ['8716','8891', '', '']

Log the processing status to Erik's POSSUM status monitor

https://docs.google.com/spreadsheets/d/1_88omfcwplz0dTMnXpCj27x-WSZaSmR-TEsYFmBD43k/edit?gid=1133887512#gid=1133887512

In the Google sheet, either record
    "Completed"            - 1D pipeline completed succesfully. Waiting for human validation
    "Failed"               - pipeline started but failed
    "NotStarted"           - pipeline not started for some reason
"""


def arg_as_list(s):
    v = ast.literal_eval(s)
    if type(v) is not list:
        raise argparse.ArgumentTypeError('Argument "%s" is not a list' % (s))
    return v


def check_pipeline_complete(log_file_path):
    with open(log_file_path, "r") as file:
        log_contents = file.read()

    if "Pipeline complete." in log_contents:
        return "Completed"
    else:
        return "Failed"


def update_1d_database(field_ID, SBid, band, status, conn):
    """
    Update the status of the specified tile in the 1D pipeline table.

    Args:
    field_id         : the field id
    SBid             : the SB number
    band (str): The band of the tile.
    Google_API_token (str): The path to the Google API token JSON file.
    status (str): The status to set in the 'status_column' column.
    conn : REST API session

    """
    print("Updating POSSUM pipeline validation database with summary plot status")

    band_number = util.get_band_number(band)
    full_field_name = util.get_full_field_name(field_ID, band)


    response = conn.patch("/1d-pipeline/observations/update/1d_pipeline_validation/?"
                          f"band_number={band_number}&"
                          f"field_name={full_field_name}&"
                          f"status={status}")
    rows_to_update = response.get("rows_updated")

    
    if rows_to_update == 0:
        print(f"No rows found for field {full_field_name} and SBID {SBid}")
        return False
    elif rows_to_update > 0:
        print(
            f"Updated all {rows_to_update} rows for field {full_field_name} and SBID {SBid} to status '{status}' in '1d_pipeline_validation' column."
        )
    else:
        print("Failed to update the database.")
    # Check if there are boundary issues for this field and SBID
    boundary_issue = conn.get(f"/1d-pipeline/partial-tiles/boundary-issues/band{band_number}/{full_field_name}/")[0]
    
    return boundary_issue


def update_status_spreadsheet(
    field_ID,
    SBid,
    band,
    Google_API_token,
    status,
    status_column,
    database_config_path="./automation/config.env",
):
    """
    Update the status of the specified tile in the STATUS Google Sheet (Cameron's sheet)
                                and in the observation_state_band1 database table.

    Args:
    field_id         : the field id
    SBid             : the SB number
    band (str): The band of the tile.
    Google_API_token (str): The path to the Google API token JSON file.
    status (str): The status to set in the 'status column' column.
    status_column: The column to update in the Google Sheet.
    """

    # Authenticate and grab the spreadsheet
    gc = gspread.service_account(filename=Google_API_token)
    load_dotenv(dotenv_path=database_config_path)
    ps = gc.open_by_url(os.getenv("POSSUM_STATUS_SHEET"))

    # Select the worksheet for the given band number
    band_number = util.get_band_number(band)
    tile_sheet = ps.worksheet(f"Survey Fields - Band {band_number}")
    tile_data = tile_sheet.get_all_values()
    column_names = tile_data[0]
    tile_table = at.Table(np.array(tile_data)[1:], names=column_names)
    full_field_name = util.get_full_field_name(field_ID, band)

    # Update all rows belonging to field and sbid
    rows_to_update = [
        idx + 2  # +2 because gspread index is 1-based and skips the header row
        for idx, row in enumerate(tile_table)
        if row["name"] == full_field_name and row["sbid"] == str(SBid)
    ]
    if rows_to_update:
        print(
            f"Updating POSSUM Status Monitor with 1D pipeline status for field {full_field_name} and SBID {SBid}"
        )
        col_letter = gspread.utils.rowcol_to_a1(
            1, column_names.index(status_column) + 1
        )[0]
        api = rest_api.PossumApiClient(database_config_path)
        for row_index in rows_to_update:
            sleep(2)  # 60 writes per minute only
            tile_sheet.update(range_name=f"{col_letter}{row_index}", values=[[status]])
            api.patch(f"/1d-pipeline/observations/update/{status_column.lower()}/?"
                      f"band_number={band_number}&"
                      f"field_name={full_field_name}&"
                      f"status={status}")
        print(
            f"Updated all {len(rows_to_update)} rows for field {full_field_name} and SBID {SBid} to status '{status}' in '{status_column}' column."
        )
    else:
        print(f"No rows found for field {full_field_name} and SBID {SBid}.")


def tilenumbers_to_tilestr(tilenumbers):
    """
    Parse a list of 4 to a single tilestr

    e.g. ['8716','8891', '', '']

    becomes "8716+8891"
    """
    tilestr = ("+").join([t for t in tilenumbers if t != ""])
    return tilestr


def delete_field_from_canfar(field_ID, SB_num, band):
    """
    Delete the field from CANFAR.

    Args:
    field_ID (str): The field ID.
    SB_num (int): The SB number.
    band (str): The band of the tile.
    """
    if band == "943MHz":
        fielddir = "/arc/projects/CIRADA/polarimetry/ASKAP/PartialTiles/943MHz/"
    elif band == "1367MHz":
        fielddir = "/arc/projects/CIRADA/polarimetry/ASKAP/PartialTiles/1367MHz/"
    else:
        raise ValueError(f"Unknown band: {band}")

    fielddir = os.path.join(fielddir, field_ID)
    # Here we are assuming there's only 1 SBID per field
    # pawsey doesnt actually distinguish which SBID a field has

    # delete all the PSM*fits files in the fielddir
    for file in os.listdir(fielddir):
        if file.startswith("PSM") and file.endswith(".fits"):
            os.remove(os.path.join(fielddir, file))
            print(f"Deleted {file} from {fielddir}")

    return


@flow(log_prints=True, name="log_PartialTiles_summary")
def main(args):
    field_ID = args.field_ID
    SB_num = args.SB_num
    band = args.band
    database_config_path = args.database_config_path

    if not os.path.isfile(database_config_path):
        raise FileNotFoundError(f".env file not found at {database_config_path}")

    # Where to find pipeline outputs
    basedir = f"/arc/projects/CIRADA/polarimetry/pipeline_runs/partial_tiles/{band}"
    basedir = f"{basedir}/{field_ID}/{SB_num}/"
    # e.g. /arc/projects/CIRADA/polarimetry/pipeline_runs/partial_tiles/943MHz/1412-28/50413/

    # Find the POSSUM pipeline log file for the given tilenumber
    log_files = sorted(glob.glob(f"{basedir}/*pipeline_config*_summary.log"))

    # Load constants for Google spreadsheets
    load_dotenv(dotenv_path=database_config_path)
    log_file_path = None

    if len(log_files) > 1:
        log_file_path = log_files[-1]

        print("WARNING: Multiple log files found. Please remove failed run log files.")
        print("Taking the last log file")

        # Write the warning to file
        with open(f"{basedir}/log_processing_status.log", "a") as log_file:
            log_file.write(
                "WARNING: Multiple log files found. Taking the last log file.\n"
            )
            log_file.write(f"{log_file_path} \n")

        status = check_pipeline_complete(log_file_path)

    elif len(log_files) < 1:
        status = "NotStarted"
    else:
        log_file_path = log_files[0]
        status = check_pipeline_complete(log_file_path)

    print(f"field {field_ID} sbid {SB_num} summary plot status: {status}, band: {band}")

    # Update the POSSUM Validation database table
    t1 = task(update_1d_database, name="update_1d_database")
    # execute tasks serially such that logging is preserved (instead of .submit)
    api_client = rest_api.PossumApiClient(database_config_path)
    has_boundary_issue = t1(field_ID, SB_num, band, status, api_client)

    if status == "Completed":
        # Update the POSSUM Pipeline Status spreadsheet as well. A complete field has been processed!
        Google_API_token = util.initiate_possum_status_sheet_and_token()
        # put the status as PartialTiles - today's date (e.g. PartialTiles - 2025-03-22)
        status_to_put = f"PartialTiles - {np.datetime64('today', 'D')}"

        if has_boundary_issue:
            # put BI flag for Boundary Issue
            status_to_put = f"PartialTilesBI - {np.datetime64('today', 'D')}"

        t2 = task(update_status_spreadsheet, name="update_status_spreadsheet")
        t2(
            field_ID,
            SB_num,
            band,
            Google_API_token,
            status_to_put,
            "single_SB_1D_pipeline",
        )

        # Then delete the field from CANFAR
        t3 = task(delete_field_from_canfar, name="delete_field_from_canfar")
        t3(field_ID, SB_num, band)

    else:
        # Make sure prefect dashboard show crashed run
        raise ValueError(
            f"Status is {status} for field {field_ID} SB {SB_num}. Please check the log file {log_file_path} for details."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Check pipeline status and update CSV file"
    )
    parser.add_argument(
        "field_ID",
        metavar="field",
        help="Field ID. e.g. 1412-28",
    )
    parser.add_argument(
        "SB_num",
        metavar="SB",
        type=int,
        help="SB number. e.g. 50413",
    )
    parser.add_argument(
        "band", choices=["943MHz", "1367MHz"], help="The frequency band of the tile"
    )
    parser.add_argument(
        "--database_config_path",
        type=str,
        default="automation/config.env",
        help="Path to .env file with database connection parameters.",
    )
    # consider chmod 600 <file> to prevent access

    args = parser.parse_args()

    main(args)
