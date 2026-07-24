import argparse
import ast
import glob

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


def update_partial_tile_1d_pipeline(field_ID, tile_numbers, band, status, conn):
    """
    Update the status of the specified tile in the partial_tile_1d_pipeline database table.

    Args:
    field_ID (str): The field ID.
    tile_number (str): The tile number to update.
    band (str): The band of the tile.
    status (str): The status to set in the '1d_pipeline' column.
    """
    fieldname = util.get_full_field_name(field_ID, band)
    band_number = util.get_band_number(band)
    conn.patch("/1d-pipeline/partial-tiles/update/status/",
                            json={
                            "band_number": band_number,
                            "field_name": fieldname,
                            "tile_numbers": tile_numbers,
                            "status": status,
                            },
            )
    ## TODO: validation in case all tiles have been completed

    # # Find the validation file path
    # psm_val = glob.glob(f"/arc/projects/CIRADA/polarimetry/pipeline_runs/{bxand}/tile{tilenumber}/*validation.html")
    # if len(psm_val) == 1:
    #     psm_val = os.path.basename(psm_val[0])
    #     validation_link = f"https://ws-uv.canfar.net/arc/files/projects/CIRADA/polarimetry/pipeline_runs/{band}/tile{tilenumber}/{psm_val}"
    # elif len(psm_val) > 1:
    #     validation_link = "MultipleHTMLFiles"
    # else:
    #     validation_link = "HTMLFileNotFound"
    # # Update the '3d_val_link' column
    # col_link_letter = gspread.utils.rowcol_to_a1(1, column_names.index('3d_val_link') + 1)[0]
    # tile_sheet.update(range_name=f'{col_link_letter}{tile_index}', values=[[validation_link]])
    # print(f"Updated tile {tilenumber} validation link to {validation_link}")


def tilenumbers_to_tilestr(tilenumbers):
    """
    Parse a list of 4 to a single tilestr

    e.g. ['8716','8891', '', '']

    becomes "8716+8891"
    """
    tilestr = ("+").join([t for t in tilenumbers if t != ""])
    return tilestr


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
        "tilenumbers",
        type=arg_as_list,
        help="A list of 4 tile numbers to process. Empty strings for less tilenumbers. e.g. ['8843','8971','',''] ",
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
    field_ID = args.field_ID
    SB_num = args.SB_num
    tilenumbers = args.tilenumbers
    tilestr = tilenumbers_to_tilestr(tilenumbers)
    band = args.band
    database_config_path = args.database_config_path

    # Where to find pipeline outputs
    basedir = f"/arc/projects/CIRADA/polarimetry/pipeline_runs/partial_tiles/{band}"
    basedir = f"{basedir}/{field_ID}/{SB_num}/{tilestr}"
    # e.g. /arc/projects/CIRADA/polarimetry/pipeline_runs/partial_tiles/943MHz/1412-28/50413/8715/

    # Find the POSSUM pipeline log file for the given tilenumber
    log_files = sorted(glob.glob(f"{basedir}/*pipeline_config*_{tilestr}.log"))

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

    print(f"Tilenumbers {tilestr} status: {status}, band: {band}")

    # Update the POSSUM partial_tile_1d_pipeline database table
    conn = rest_api.PossumApiClient(database_config_path)
    update_partial_tile_1d_pipeline(field_ID, tilenumbers, band, status, conn)
    