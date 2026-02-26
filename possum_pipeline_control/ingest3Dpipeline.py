"""
Calls Sharon Goliath's ingest script ("possum_run") for a specific tile number and band
to ingest 3D pipeline products.


Arguments

tilenumber -- int -- which tile number to process
band       -- str -- either "943MHz" or "1367MHz" for band 1 or band 2 data

"""

import argparse
import datetime
import os
from time import sleep

import astropy.table as at
import astroquery.cadc as cadc
import gspread
import numpy as np
from dotenv import load_dotenv
from prefect import flow, task
from prefect.cache_policies import NO_CACHE

# important to grab _run() because run() is wrapped in sys.exit()
try:
    from possum2caom2.composable import _run as possum_run  # type: ignore
except ImportError:
    print(
        "Could not import possum_run from possum2caom2.composable. Make sure possum2caom2 is installed."
    )
    possum_run = None
from automation import database_queries as db
from possum_pipeline_control import util

# 14 (grouped) products for the 3D pipeline
all_3dproducts = [
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
]


@task
def replace_working_directory_and_save(file_path, tile_workdir):
    # Read the content of the original file
    with open(file_path, "r") as file:
        lines = file.readlines()

    # Path to the new file
    new_file_path = os.path.join(tile_workdir, "config.yml")
    print(f"Creating config file {new_file_path}")

    # Write the updated content to the new file
    with open(new_file_path, "w") as new_file:
        for line in lines:
            if line.startswith("working_directory:"):
                # make sure to change the working directory to the tile workdir
                new_file.write(f"working_directory: {tile_workdir}\n")
            elif line.startswith("proxy_file_name:"):
                # make sure the proxy file name points to the correct location of the CADC certificate
                new_file.write(
                    f"proxy_file_name: {os.path.expanduser('~/.ssl/cadcproxy.pem')}\n"
                )
            else:
                # blanket copy all other lines
                new_file.write(line)


    return new_file_path


@task
def launch_ingestscript(tile_workdir):
    """change workdir and launch ingest script"""

    # Start possum_run in correct workdir!!
    with util.TemporaryWorkingDirectory(tile_workdir):
        result = possum_run()
    return result


@task
def check_report(tile_workdir):
    """Check the report file for the number of inputs and successes"""
    report_file = f"{tile_workdir}_report.txt"

    if not os.path.exists(report_file):
        print(f"Report file {report_file} does not exist.")
        return False

    inputs, successes = None, None

    with open(report_file, "r") as file:
        for line in file:
            if "Number of Inputs" in line:
                inputs = int(line.split(":")[1].strip())
            if "Number of Successes" in line:
                successes = int(line.split(":")[1].strip())

    # Expect 24 files for the 3D pipeline data.
    if inputs == 24 and successes == 24:
        print("Ingest was successful.")
        return True
    else:
        print("Something has gone wrong with the ingest according to _report.txt")
        print(f"Expected 24 inputs and found {inputs}")
        print(f"Expected 24 successes and found {successes}")
        return False


@task(cache_policy=NO_CACHE)
def update_tile_database(tile_number, band_str, status, test_flag, conn):
    """
    Update the status of the specified tile in the VALIDATION database table.
    (see also log_processing_status.py)

    Args:
    tile_number (str): The tile number to update.
    band (str): The band of the tile.
    Google_API_token (str): The path to the Google API token JSON file.
    status (str): The status to set in the '3d_pipeline_ingest' column.
    test (bool):  if we want to test what happened to something with 'IngestFailed' status
    """
    print("Updating POSSUM pipeline validation database")
    band_number = util.get_band_number(band_str)
    row = db.get_3d_tile_data(tile_number, band_number, conn)
    if len(row) > 0:
        if not test_flag:
            # Status should be "IngestRunning" otherwise something went wrong
            if row[0][3] != "IngestRunning":
                raise ValueError(
                    f"Found status {row[0][3]} while it should be 'IngestRunning'"
                )
        else:
            print(
                f"Testing enabled. Current status of tile {tile_number} is {row[0][3]}"
            )

        # Update the status in the '3d_pipeline_ingest' column
        row_num = db.update_3d_pipeline_table(
            tile_number, band_number, status, "3d_pipeline_ingest", conn
        )
        if row_num > 0:
            print(
                f"Updated tile {tile_number} status to {status} in '3d_pipeline_ingest' column."
            )
        else:
            raise ValueError(f"Tile {tile_number} not found in the database.")


@task
def check_CADC(
    tilenumber, band, CADC_cert_file="/arc/home/ErikOsinga/.ssl/cadcproxy.pem"
):
    """
    query CADC for 3D pipeline products

    Based on Cameron's update_CADC_tile_status.py
    """
    ## cadc-get-cert on CANFAR
    ## if I want to test on p1
    # CADC_cert_file = '/home/erik/.ssl/cadcproxy.pem'
    # use the same service link as the ingest script to have updated records
    os.environ["CADCTAP_SERVICE_URI"] = "ivo://cadc.nrc.ca/ams/cirada"
    CADC_session = cadc.Cadc()
    CADC_session.login(certificate_file=CADC_cert_file)

    query = CADC_session.create_async("""SELECT observationID,Plane.productID,Observation.lastModified FROM caom2.Plane AS Plane 
    JOIN caom2.Observation AS Observation ON Plane.obsID = Observation.obsID 
    WHERE  (Observation.collection = 'POSSUM') AND (observationID NOT LIKE '%pilot1') """)
    query.run().wait()
    query.raise_if_error()
    result = query.fetch_result().to_table()
    result.add_column(
        [x.split("_")[-2] for x in result["observationID"]], name="tile_number"
    )
    result.add_column(
        [x.split("MHz")[0] for x in result["observationID"]], name="freq"
    )  # str4

    freq = band.replace("MHz", "")

    # get all observationIDs, productIDs, etc (CADC output) for this tile number
    result_tile = result[result["tile_number"] == tilenumber]

    # get all products that have the correct frequency
    result_tile_band = result_tile[result_tile["freq"] == freq]

    print("Found products:")
    print(result_tile_band["productID"])

    # For 3D pipeline, there should be 17 products (and 3 inputs)
    for product in all_3dproducts:
        if product not in result_tile_band["productID"]:
            print(f"CADC is missing product {product}")
            return False, None

    print("CADC contains all products.")

    dt = [datetime.datetime.fromisoformat(x) for x in result_tile_band["lastModified"]]
    last_modified = max(dt)
    date = last_modified.date().isoformat()

    return True, date


@task
def update_status_spreadsheet(tile_number, band, Google_API_token, date):
    """
    Update the status of the specified tile in the Google Sheet.

    Args:
    tile_number (str): The tile number to update.
    band (str): The band of the tile.
    Google_API_token (str): The path to the Google API token JSON file.
    date (str): The timestamp to set in the '3d_pipeline' column.

    ## Note the STATUS spreadsheet only has the '3d_pipeline' column.
    ## the VALIDATION spreadsheet has more details.
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
        tile_sheet.update(range_name=f"{col_letter}{tile_index}", values=[[date]])
        print(f"Updated tile {tile_number} status to {date} in '3d_pipeline' column.")
        # Also update the DB
        conn = db.get_database_connection(test=False)
        db.update_3d_pipeline_table(tile_number, band_number, date, "3d_pipeline", conn)
        conn.close()
    else:
        print(f"Tile {tile_number} not found in the sheet.")


@flow(log_prints=True, name="3D pipeline ingest")
def do_ingest(
    tilenumber,
    band,
    test=False,
    CADC_cert_file="/arc/home/ErikOsinga/.ssl/cadcproxy.pem",
):
    """Does the ingest script

    1. Create config.yml based on template
    2. Execute "possum_run" in the correct directory
    """

    config_template = "/arc/projects/CIRADA/polarimetry/ASKAP/Pipeline_logs/config_templates/config_ingest.yml"
    tile_workdir = (
        f"/arc/projects/CIRADA/polarimetry/pipeline_runs/{band}/tile{tilenumber}/"
    )

    # Create config file and put it in the correct directory
    replace_working_directory_and_save(config_template, tile_workdir)

    if test:
        # dont run ingest script (for testing when already ingested)
        result = 0
    else:
        # Launch 'possum_run'
        result = launch_ingestscript(tile_workdir)

    if result == 0:
        print("Ingest script completed without errors")
    else:
        print("Ingest script completed with errors")

    # Check the ingest report file
    success = check_report(tile_workdir)

    # Wait 33 minutes, I think CADC takes a bit of time to update
    if not test:
        sleep(int(33 * 60))  ## not sure the exact time that we should wait...

    # Check the CADC also if indeed all files are there
    # if this fails with SSLError, try to run cadc-get-cert -u <username> on CANFAR
    CADCsuccess, date = check_CADC(tilenumber, band, CADC_cert_file)

    status = "IngestFailed"
    if success:
        if CADCsuccess:
            status = "Ingested"
        else:
            print("_report.txt reports succesful ingest, but CADC ingest failed")
    else:
        print("_report.txt reports that ingestion failed")

    # Record the status in the POSSUM Validation database
    conn = db.get_database_connection(test=False)
    update_tile_database(tilenumber, band, status, test_flag=test, conn=conn)
    conn.close()

    if status == "Ingested":
        # If succesful, also record the date of ingestion in POSSUM status spreadsheet
        # Update the POSSUM status monitor google sheet (see also log_processing_status.py)
        Google_API_token = util.initiate_possum_status_sheet_and_token()

        update_status_spreadsheet(tilenumber, band, Google_API_token, date)

    else:
        # Make sure to raise an error such that the flow run is marked as failed
        raise ValueError("Ingestion failed somehow.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Do a 3D pipeline ingest on CANFAR")
    parser.add_argument("tilenumber", type=int, help="The tile number to ingest")
    parser.add_argument(
        "band", choices=["943MHz", "1367MHz"], help="The frequency band of the tile"
    )
    parser.add_argument(
        "-test", action="store_true", help="Test already ingested tile? (Default False)"
    )
    parser.add_argument(
        "-CADC_cert_file",
        type=str,
        default="/arc/home/ErikOsinga/.ssl/cadcproxy.pem",
        help="Path to CADC certificate file",
    )

    args = parser.parse_args()
    tilenumber = args.tilenumber
    band = args.band
    test = args.test
    CADC_cert_file = args.CADC_cert_file
    # test = True

    # needs to be str for comparison
    tilenumber = str(tilenumber)

    print(f"Starting 3D pipeline ingest for tile {tilenumber}, band {band}")

    # load env for google spreadsheet constants
    load_dotenv(dotenv_path="./automation/config.env")

    do_ingest(tilenumber, band, test=test, CADC_cert_file=CADC_cert_file)
