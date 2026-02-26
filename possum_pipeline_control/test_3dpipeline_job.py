"""
A test module to check whether a user has set up everything around the POSSUM pipeline orchestration

This script:

- Checks whether it can access the AUSSRC Prefect server. 
- Checks whether it can access the POSSUM database (only reads)
- Checks whether it can access the POSSUM Status Sheet Google Sheet (only reads)
- launches a small test 3D pipeline headless job to CANFAR
- Has it report back to AUSSRC prefect server

It can be run from any machine, but you should set the environment variables for your machine
to connect to the aussrc prefect server:

export PREFECT_API_AUTH_STRING=SECRET
export PREFECT_API_URL=SECRET

Ask someone what the secret values are. 


Assumes that this script is called from the POSSUMutils root dir as

python -m possum_pipeline_control.test_3dpipeline_job


@author: Erik Osinga
"""


from canfar.sessions import Session
from dotenv import load_dotenv

from possum_pipeline_control.test_database_access import (
    check_acces_to_google_spread,
    check_acces_to_prod_db,
)

session = Session()


def launch_test_session(jobname="testjob"):

    # Template bash script to run
    args = "/arc/projects/CIRADA/polarimetry/software/POSSUMutils/cirada_software/test_3dpipeline_job.sh"

    print("Launching test 3D pipeline session")
    print(f"Command: bash {args}")

    image = "images.canfar.net/cirada/possumpipelineprefect-3.12:v2.0.2"
    # could also use flexible resources ?
    session_id = session.create(
        name=jobname.replace(
            "_", "-"
        ),  # Prevent Error 400: name can only contain alpha-numeric chars and '-'
        image=image,
        cores=4,  # set to None for flexible mode
        ram=10,  # set to None flexible mode
        kind="headless",
        cmd="bash",
        args=args,
        replicas=1,
        env={},
    )

    print("Check sessions at https://ws-uv.canfar.net/skaha/v1/session")
    print(
        f"Check logs at https://ws-uv.canfar.net/skaha/v1/session/{session_id[0]}?view=logs"
    )
    print("Also check the prefect dashboard at localhost:4200 or possum-prefect.aussrc.org")

    session_id_str = session_id[0] if len(session_id) > 0 else None

    if session_id_str is not None:
        print(f"Session launched successfully with ID: {session_id_str}")
    else:
        raise ValueError("Failed to launch session. No session ID returned. CANFAR is down?")

    return session_id_str


if __name__ == "__main__":
    # load env for database credentials and google spreadsheet credential
    load_dotenv(dotenv_path="./automation/config.env")

    # Check access to production database from p1
    check_acces_to_prod_db()

    # Check access to Cameron's sheet from p1
    check_acces_to_google_spread()

    # Launch 3d pipeline test job on CANFAR
    launch_test_session()
