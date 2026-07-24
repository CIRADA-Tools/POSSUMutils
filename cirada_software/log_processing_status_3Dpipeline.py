import argparse
import csv
import glob
import os

import astropy.table as at
import gspread
import numpy as np
from dotenv import load_dotenv
from prefect import flow, task

from automation import possum_api_client as rest_api
from possum_pipeline_control import util

"""
Usage: python log_processing_status.py tilenumber band

Log the processing status to Camerons POSSUM status monitor

https://docs.google.com/spreadsheets/d/1sWCtxSSzTwjYjhxr1_KVLWG2AnrHwSJf_RWQow7wbH0

and

/arc/projects/CIRADA/polarimetry/pipeline_runs/pipeline_status.csv

In the above file, either record 
    "Completed" - pipeline completed succesfully (still needs to be validated)
    "Failed"    - pipeline started but failed
    "NotStarted"- pipeline not started for some reason

    
In the Google sheet, either record
    "WaitingForValidation" - 3D pipeline completed succesfully. Waiting for human validation
    "Failed"               - pipeline started but failed
    "NotStarted"           - pipeline not started for some reason
"""


def check_pipeline_complete(log_file_path):
    with open(log_file_path, "r") as file:
        log_contents = file.read()

    if "Pipeline complete." in log_contents:
        return "Completed"
    else:
        return "Failed"


def update_status_csv(tilenumber, status, band, csv_file_path, all_tiles):
    # Check if the CSV file exists
    file_exists = os.path.isfile(csv_file_path)

    # Read existing data
    data = {}
    if file_exists:
        with open(csv_file_path, "r") as csvfile:
            reader = csv.reader(csvfile)
            next(reader, None)  # Skip header row
            for row in reader:
                if len(row) == 3:
                    data[f"{row[0]}_{row[2]}"] = (row[1], row[2])

    # Add all tile numbers with status "ReadyToProcess" and the correct band if not already present
    for tile, tile_band in all_tiles:
        key = f"{tile}_{tile_band}"
        if key not in data:
            data[key] = ("ReadyToProcess", tile_band)

    # Update the status for the given tilenumber and band
    key = f"{tilenumber}_{band}"
    data[key] = (status, band)

    # Write updated data to the CSV file
    with open(csv_file_path, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        # Write header row
        writer.writerow(["#tilenumber", "status", "band"])
        # Write data rows
        for key, value in data.items():
            tilenumber, band = key.split(
                "_"
            )  # Extract the tilenumber and band from the key
            writer.writerow([tilenumber, value[0], value[1]])


@task
def update_status_spreadsheet(tile_number, band, Google_API_token, status):
    """
    Update the status of the specified tile in the Google Sheet.

    Args:
    tile_number (str): The tile number to update.
    band (str): The band of the tile.
    Google_API_token (str): The path to the Google API token JSON file.
    status (str): The status to set in the '3d_pipeline' column.
    """
    print("Updating POSSUM status sheet")

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
        # as of >v6.0.0 the .update function requires a list of lists
        tile_sheet.update(range_name=f"{col_letter}{tile_index}", values=[[status]])
        print(f"Updated tile {tile_number} status to {status} in '3d_pipeline' column.")
    else:
        print(f"Tile {tile_number} not found in the sheet.")


@task
def update_3d_tile_database(tile_number, band, status):
    """
    Update the status of the specified tile in the VALIDATION database.

    Args:
    tile_number (str): The tile number to update.
    band (str): The band of the tile.
    status (str): The status to set in the '3d_pipeline' column.
    """

    band_number = util.get_band_number(band)

    # Find the validation file path
    psm_val = glob.glob(
        f"/arc/projects/CIRADA/polarimetry/pipeline_runs/{band}/tile{tile_number}/*validation.html"
    )
    if len(psm_val) == 1:
        psm_val = os.path.basename(psm_val[0])
        validation_link = f"https://ws-uv.canfar.net/arc/files/projects/CIRADA/polarimetry/pipeline_runs/{band}/tile{tile_number}/{psm_val}"
    elif len(psm_val) > 1:
        validation_link = "MultipleHTMLFiles"
    else:
        validation_link = "HTMLFileNotFound"

    # execute query
    conn = rest_api.PossumApiClient()
    response = conn.patch(f"/3d-pipeline/tiles/update/3d_pipeline_val/?band_number={band_number}"
                          f"&tile_number={tile_number}"
                          f"&3d_pipeline_val={status}")
    rows_updated = response.get('rows_updated')
    conn.patch(f"/3d-pipeline/tiles/update/3d_val_link/?band_number={band_number}"
               f"&tile_number={tile_number}"
               f"&3d_val_link={validation_link}")

    # Print results
    if rows_updated <= 0:
        print(f"Tile {tile_number} not found in the sheet.")
    if rows_updated > 1:
        print(f"Warning: Multiple rows updated for tile {tile_number}.")
    else:
        print(
            f"Updated tile {tile_number} status to {status} in '3d_pipeline_val' column."
        )
        print(f"Updated tile {tile_number} validation link to {validation_link}")

    return


@flow(log_prints=True, name="log_3d_pipeline_status")
def main():
    parser = argparse.ArgumentParser(
        description="Check pipeline status and update CSV file"
    )
    parser.add_argument("tilenumber", type=int, help="The tile number to check")
    parser.add_argument(
        "band", choices=["943MHz", "1367MHz"], help="The frequency band of the tile"
    )

    args = parser.parse_args()
    tilenumber = args.tilenumber
    band = args.band

    # Get all tile numbers from the directories
    base_tile_dir_943 = "/arc/projects/CIRADA/polarimetry/ASKAP/Tiles/943MHz/"
    base_tile_dir_1367 = "/arc/projects/CIRADA/polarimetry/ASKAP/Tiles/1367MHz/"

    all_tile_dirs_943 = [
        d
        for d in os.listdir(base_tile_dir_943)
        if os.path.isdir(os.path.join(base_tile_dir_943, d)) and d.isdigit()
    ]
    all_tile_dirs_1367 = [
        d
        for d in os.listdir(base_tile_dir_1367)
        if os.path.isdir(os.path.join(base_tile_dir_1367, d)) and d.isdigit()
    ]

    all_tiles = [(str(tile), "943MHz") for tile in all_tile_dirs_943] + [
        (str(tile), "1367MHz") for tile in all_tile_dirs_1367
    ]

    # Find the POSSUM pipeline log file for the given tilenumber
    log_files = sorted(
        glob.glob(
            f"/arc/projects/CIRADA/polarimetry/pipeline_runs/{band}/tile{tilenumber}/*pipeline_config_{tilenumber}.log"
        )
    )

    if len(log_files) > 1:
        log_file_path = log_files[-1]

        print("WARNING: Multiple log files found. Please remove failed run log files.")
        print("Taking the last log file")

        # Write the warning to file
        with open(
            f"/arc/projects/CIRADA/polarimetry/pipeline_runs/{band}/tile{tilenumber}/log_processing_status.log",
            "a",
        ) as log_file:
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

    print(f"Tilenumber {tilenumber} status: {status}, band: {band}")

    # Update the simple CANFAR status CSV file (legacy)
    csv_file_path = "/arc/projects/CIRADA/polarimetry/pipeline_runs/pipeline_status.csv"
    update_status_csv(tilenumber, status, band, csv_file_path, all_tiles)

    # Update the POSSUM status monitor google sheet
    # # Load constants for google spreadsheet
    load_dotenv(dotenv_path="./automation/config.env")    
    # # read the location of the API token from the Prefect secret, and the prefect server access from ./automation/config.env
    Google_API_token = util.initiate_possum_status_sheet_and_token()

    # Make sure it's clear that the status is only fully complete if 3D pipeline outputs have been ingested
    if status == "Completed":
        status = "WaitingForValidation"
    # Update Cameron's POSSUM status monitor spreadsheet
    update_status_spreadsheet(tilenumber, band, Google_API_token, status)
    # Update the AUSSRC database possum.tile_state_band1 or possum.tile_state_band2 table
    update_3d_tile_database(tilenumber, band, status)

    if status != "WaitingForValidation":
        # Make sure prefect dashboard show crashed run
        raise ValueError(
            f"Status is {status} for tile number {tilenumber}. Please check the log file {log_file_path} for details."
        )


if __name__ == "__main__":
    main()
