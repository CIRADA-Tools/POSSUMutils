import argparse
import ast
from datetime import datetime
from skaha.session import Session
from control_1D_pipeline_PartialTiles import get_open_sessions

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
        raise argparse.ArgumentTypeError("Argument \"%s\" is not a list" % (s))
    return v

def launch_session(run_name, field_ID, SBnumber, image, cores, ram, ptype):
    """Launch 1D pipeline Partial Tile summary run or download run"""

    df_sessions = get_open_sessions()

    if ptype == "post":
        # Template bash script to run
        args = f"/arc/projects/CIRADA/polarimetry/software/POSSUMutils/cirada_software/run_1Dpipeline_PartialTiles_band1_summary.sh {run_name} {field_ID} {SBnumber}"
    elif ptype == "pre":
        # Template bash script to run
        args = f"/arc/projects/CIRADA/polarimetry/software/POSSUMutils/cirada_software/run_1Dpipeline_PartialTiles_band1_srl_and_googlesheet.sh {run_name} {field_ID} {SBnumber}"

        if df_sessions[df_sessions['status'] == 'Running']['name'].str.contains("pre-dl").any() or df_sessions[df_sessions['status'] == 'Pending']['name'].str.contains("pre-dl").any():
            # check if any download jobs are currently running on CANFAR. Prevents overloading the system and confusing the dl jobs
            print("A pre-dl job is already running. Skipping this run.")
            return

    print("Launching session")
    print(f"Command: bash {args}")

    session_id = session.create(
        name=run_name.replace('_', '-'),  # Prevent Error 400: name can only contain alpha-numeric chars and '-'
        image=image,
        cores=cores,
        ram=ram,
        kind="headless",
        cmd="bash",
        args=args,
        replicas=1,
        env={},
    )

    print("Check sessions at https://ws-uv.canfar.net/skaha/v0/session")
    print(f"Check logs at https://ws-uv.canfar.net/skaha/v0/session/{session_id[0]}?view=logs")

    return

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Launch a 1D pipeline Partial Tiles run")
    parser.add_argument("field_ID", type=str, help="The field ID to process, e.g. 1227-69")
    parser.add_argument("SBnumber", type=int, help="The SB number to process, e.g. 61103")
    parser.add_argument("type", choices=["pre", "post"], help="Whether to run pre-processing or post-processing step")

    args = parser.parse_args()
    field_ID = args.field_ID
    SBnumber = args.SBnumber
    ptype = args.type
    
    timestr = ((datetime.now().strftime("%d/%m/%Y %H:%M:%S"))[11:]).replace(':','-') # ":" is not allowed character

    # optionally :latest for always the latest version. CANFAR has a bug with that though.
    # image = "images.canfar.net/cirada/possumpipelineprefect-3.12:latest"
    image = "images.canfar.net/cirada/possumpipelineprefect-3.12:v1.11.0" # v1.12.1 has astropy issue https://github.com/astropy/astropy/issues/17497
    # good default values
    cores = 4

    if ptype == "post":
        ram = 30 # 23 GB is just about enough for summary plot without density calc
        run_name = f"{SBnumber}-{timestr}"
    elif ptype == "pre":
        ram = 10 # dont need a lot of ram for catalogue writing
        # max 15 characters for run name. SBID+timenow: e.g. 50413-11-39-21
        run_name = "pre-dl" # makes it clear a 'pre' download job is running. Dont want too many of these.

    # Check allowed values at canfar.net/science-portal, 10, 20, 30, 40 GB should be allowed

    launch_session(run_name, field_ID, SBnumber, image, cores, ram, ptype)
