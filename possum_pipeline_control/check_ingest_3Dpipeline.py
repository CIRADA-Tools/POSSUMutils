import argparse
import os

from canfar.sessions import Session
from vos import Client

from automation import database_queries as db
from possum_pipeline_control import util

session = Session()


"""
Checks POSSUM validation status ("POSSUM Pipeline validation" google sheet) if 3D pipeline outputs can
be ingested.

Should be executed on p1


If the "3d_pipeline_val" column is marked as "Good", we can launch an ingest job.
    In that case, the status of 3d_pipeline_ingest will be changed to IngestRunning


@author: Erik Osinga
"""
def get_tiles_for_ingest(band_number, conn):
    """
    Get a list of 3D pipeline tile numbers that should be ready to be ingested.

    i.e.  '3d_pipeline_val' column is equal to "Good", meaning that it has been validated by a human.
    and   '3d_pipeline_ingest' column is empty, meaning that it has not yet been tried to ingest.

    Args:
    band_number (int): The band number (1 or 2) to check.

    Returns:
    list: A list of tile numbers that satisfy the conditions.
    """
    # Find the tiles that satisfy the conditions
    return db.get_tiles_for_ingest(band_number, conn)


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

def launch_ingest(tilenumber, band):
    """Launch 3D pipeline ingest script"""

    band_number = util.get_band_number(band)

    run_name = f"ingest{tilenumber}"
    # optionally :latest for always the latest version (SEEMS TO BE BROKEN)
    # image = "images.canfar.net/cirada/possumpipelineprefect-3.12:latest"
    image = os.getenv("IMAGE")
    # good default values for ingest script
    cores = 2
    ram = 32  # Check allowed values at canfar.net/science-portal

    # Template bash script to run
    args = f"/arc/projects/CIRADA/polarimetry/software/POSSUMutils/cirada_software/ingest_3Dpipeline_band{band_number}_prefect.sh {tilenumber} {band}"

    print("Launching session")
    print(f"Command: bash {args}")
    print(f"Image: {image}")

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

    return session_id_str

def update_status(tile_number, band, status, conn):
    """
    Update the status of the specified tile in the database.

    Args:
    tile_number (str): The tile number to update.
    band (str): The band of the tile.
    status (str): The status to set in the '3d_pipeline_ingest' column.
    """
    band_no = util.get_band_number(band)
    return db.update_3d_pipeline_table(
        tile_number, band_no, status, "3d_pipeline_ingest", conn
    )

def ingest_3Dpipeline(band_number=1):
    if band_number == 1:
        band = "943MHz"
    elif band_number == 2:
        band = "1367MHz"

    # Check database for band 1 tiles that have been processed AND validated
    conn = db.get_database_connection(test=False)
    tile_numbers = get_tiles_for_ingest(band_number, conn)
    tile_numbers = [
        str(tn) for tn in tile_numbers
    ]  # make sure they are strings for comparison
    conn.close()

    canfar_tilenumbers = get_canfar_tiles(band_number=band_number)

    if len(tile_numbers) > 0:
        print(
            f"Found {len(tile_numbers)} tiles in Band {band_number} ready to be ingested according to AUSSRC database"
        )
        print(
            f"    On CANFAR, found {len(canfar_tilenumbers)} tiles in Band {band_number}"
        )

        if len(tile_numbers) > len(canfar_tilenumbers):
            tiles_ready_aussrc_not_canfar = set(tile_numbers) - set(canfar_tilenumbers)
            print(
                f"{len(tiles_ready_aussrc_not_canfar)} tiles ready according to AUSSRC but not on CANFAR"
            )
            print(f"    first 5 : {list(tiles_ready_aussrc_not_canfar)[:5]}")

        # Check whether tiles are indeed available on CANFAR (should be)
        tiles_on_both = set(tile_numbers) & set(canfar_tilenumbers)
        print(
            f"Number of tiles both ready to be ingested and on CANFAR: {len(tiles_on_both)}"
        )

        if tiles_on_both:
            # Launch the first tile number (assumes this function will be called many times)
            tilenumber = list(tiles_on_both)[0]
            print(f"\nLaunching headless job for 3D pipeline with tile {tilenumber}")

            # Launch the pipeline
            launch_ingest(tilenumber, band)

            # Update the status of 3d_pipeline_ingest to "IngestRunning"
            conn = db.get_database_connection(test=False)
            row_count = update_status(tilenumber, band, "IngestRunning", conn)
            conn.close()
            if row_count == 0:
                print(f"Tile {tilenumber} not found in the sheet.")

        else:
            print("No tiles are available on both CADC and CANFAR.")
    else:
        print("Found no tiles ready to be processed.")

    print("3D pipeline ingest check complete.")
    print("\n")  

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Checks POSSUM validation status ('POSSUM Pipeline validation' google sheet) if 3D pipeline outputs can be ingested."
    )
    parser.add_argument(
        "-b",
        "--band-number",
        type=int,
        choices=[1, 2],
        default=1,
        help="Band number to process: 1 for 943MHz or 2 for 1367MHz",
    )
    parser.add_argument(
        "--database_config_path",
        type=str,
        help="Path to .env file with database connection parameters.",
    )
    args = parser.parse_args()

    # Band number 1 (943MHz) or 2 ("1367MHz")
    band_number = args.band_number

    # load env for google spreadsheet constants
    util.initiate_possum_status_sheet_and_token(args.database_config_path)

    ## Assumes this script is called by run_3D_pipeline_intermittently.py
    ingest_3Dpipeline(band_number=band_number)
    
