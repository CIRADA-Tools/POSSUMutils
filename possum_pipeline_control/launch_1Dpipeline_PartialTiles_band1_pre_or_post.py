import argparse
import ast
import os
from datetime import datetime

from canfar.sessions import Session
from possum_pipeline_control.control_1D_pipeline_PartialTiles import get_open_sessions

"""
Submit a headless job to do either pre-processing or post-processing of 1D Partial Tile Pipeline.
See arguments in __main__ below

"pre"processing -> create the SNR cutoff and per-tile catalogues and submit jobs to the google sheet "POSSUM Pipeline Validation"

"post"processing -> collate all partial tile catalogues into one catalogue per field+sbid and create summary plot

"""


# from skaha.models import ContainerRegistry

# Shouldnt put these on github...
# see https://shinybrar.github.io/skaha/
# registry = ContainerRegistry(username="ErikOsinga", secret="CLI")

# session = Session(registry=registry)
session = Session()


def arg_as_list(s):
    v = ast.literal_eval(s)
    if type(v) is not list:
        raise argparse.ArgumentTypeError('Argument "%s" is not a list' % (s))
    return v


def launch_session(
    run_name, field_ID, SBnumber, image, cores, ram, ptype, max_dl_jobs=2
):
    """Launch 1D pipeline Partial Tile summary run or download run"""

    df_sessions = get_open_sessions()

    if ptype == "post":
        # Template bash script to run
        args = f"/arc/projects/CIRADA/polarimetry/software/POSSUMutils/cirada_software/run_1Dpipeline_PartialTiles_band1_summary.sh {run_name} {field_ID} {SBnumber}"
    elif ptype == "pre":
        # Template bash script to run
        args = f"/arc/projects/CIRADA/polarimetry/software/POSSUMutils/cirada_software/run_1Dpipeline_PartialTiles_band1_srl_and_googlesheet.sh {run_name} {field_ID} {SBnumber}"

        if len(df_sessions) == 0:
            print("No open sessions. Can launch a pre-dl job.")

        else:
            running_or_pending_pre_dl = df_sessions[
                (df_sessions["status"].isin(["Running", "Pending"]))
                & (df_sessions["name"].str.contains("pre-dl"))
            ]

            if len(running_or_pending_pre_dl) >= max_dl_jobs:
                print(
                    f"Greater than or equal to {max_dl_jobs} download jobs are already running. Skipping this run."
                )
                return

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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Launch a 1D pipeline Partial Tiles run"
    )
    parser.add_argument(
        "field_ID", type=str, help="The field ID to process, e.g. 1227-69"
    )
    parser.add_argument(
        "SBnumber", type=int, help="The SB number to process, e.g. 61103"
    )
    parser.add_argument(
        "type",
        choices=["pre", "post"],
        help="Whether to run pre-processing or post-processing step",
    )

    args = parser.parse_args()
    field_ID = args.field_ID
    SBnumber = args.SBnumber
    ptype = args.type

    timestr = ((datetime.now().strftime("%d/%m/%Y %H:%M:%S"))[11:]).replace(
        ":", "-"
    )  # ":" is not allowed character

    # optionally :latest for always the latest version. CANFAR has a bug with that though.
    # image = "images.canfar.net/cirada/possumpipelineprefect-3.12:latest"
    # image = "images.canfar.net/cirada/possumpipelineprefect-3.12:v1.11.0" # v1.12.1 has astropy issue https://github.com/astropy/astropy/issues/17497
    image = os.getenv("IMAGE")
    
    # good default values
    cores = 4
    max_dl_jobs = 2

    if ptype == "post":
        ram = 40  # 40 GB is just about enough for summary plot without density calc
        run_name = f"{SBnumber}-{timestr}"
    elif ptype == "pre":
        ram = 2  # dont need a lot of ram for catalogue writing & downloading
        cores = 1  # neither do we need a lot of cores
        # max 15 characters for run name. SBID+timenow: e.g. 50413-11-39-21
        run_name = f"pre-dl-{SBnumber}"  # makes it clear a 'pre' download job is running. Dont want too many of these.


    # Check allowed values at canfar.net/science-portal, 10, 20, 30, 40 GB should be allowed
    launch_session(
        run_name, field_ID, SBnumber, image, cores, ram, ptype, max_dl_jobs=max_dl_jobs
    )
