import argparse
import ast
import asyncio
import os
from datetime import datetime

from canfar.sessions import Session
from prefect import flow

from automation import canfar_polling

session = Session()


def arg_as_list(s):
    v = ast.literal_eval(s)
    if type(v) is not list:
        raise argparse.ArgumentTypeError('Argument "%s" is not a list' % (s))
    return v


def launch_session(run_name, field_ID, tilenumbers, SBnumber, image, cores, ram):
    """Launch 1D pipeline Partial Tile run"""
    t1, t2, t3, t4 = tilenumbers

    # Template bash script to run
    args = f"/arc/projects/CIRADA/polarimetry/software/POSSUMutils/cirada_software/run_1Dpipeline_PartialTiles_band1.sh {run_name} {field_ID} {SBnumber} {t1} {t2} {t3} {t4}"

    print("Launching session")
    print(f"Command: bash {args}")

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


@flow(name="launch_1d_partialtiles", log_prints=True)
async def main_flow(field_ID, tilenumbers, SBnumber):
    timestr = ((datetime.now().strftime("%d/%m/%Y %H:%M:%S"))[11:]).replace(
        ":", "-"
    )  # ":" is not allowed character
    # max 15 characters for run name. SBID+timenow: e.g. 50413-11-39-21
    run_name = f"{SBnumber}-{timestr}"

    # optionally :latest for always the latest version. CANFAR has a bug with that though.
    image = os.getenv("IMAGE")
    # good default values
    cores = 4
    # Allocate different RAM based on how many tiles, lets say ~ 20 GB per tile needed
    number_of_tiles = len([t for t in tilenumbers if t != ""])
    ram = 20 * number_of_tiles

    # check if there are any stuck jobs before launching new ones
    reconcile_summary = await canfar_polling.reconcile_running_prefect_with_canfar_task()

    # Check allowed values at canfar.net/science-portal, 10, 20, 30, 40 GB should be allowed
    launch_session(
            run_name, field_ID, tilenumbers, SBnumber, image, cores, ram
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Launch a 1D pipeline Partial Tiles run"
    )
    parser.add_argument(
        "field_ID", type=str, help="The field ID to process, e.g. 1412-28"
    )
    parser.add_argument(
        "tilenumbers",
        type=arg_as_list,
        help="A list of 4 tile numbers to process. Empty strings for less tilenumbers. e.g. ['8843','8971','',''] ",
    )
    parser.add_argument("SBnumber", type=int, help="The SB number to process")

    args = parser.parse_args()
    field_ID = args.field_ID
    tilenumbers = args.tilenumbers
    SBnumber = args.SBnumber    
    
    res = asyncio.run(main_flow(field_ID, tilenumbers, SBnumber))
