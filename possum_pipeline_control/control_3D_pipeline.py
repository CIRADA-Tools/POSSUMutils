import os
import subprocess
import time
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from dotenv import load_dotenv
from prefect import flow

from automation import possum_api_client as rest_api, database_queries as db
from possum_pipeline_control import util
from print_all_open_sessions import get_open_sessions


def create_3d_progress_plot():
    """Create a progress plot for the 3D pipeline."""

    load_dotenv(dotenv_path="./automation/config.env")
    conn = rest_api.PossumApiClient()
    rows = conn.get("/3d-pipeline/tiles/plotting/band1/")
    # tile, "3d_pipeline_val", "3d_val_link", "3d_pipeline_ingest", "3d_pipeline", "cube_state"
    tile3d_table = db.rows_to_table(
        rows,
        colnames=[
            "tile",
            "3d_pipeline_val",
            "3d_val_link",
            "3d_pipeline_ingest",
            "3d_pipeline",
            "cube_state",
        ],
        dtype=[int, str, str, str, str, str],
    )

    # Count the number of tiles in each state. Remember the states can be the following:
    # valid_states_val = ["WaitingForValidation", "Failed", "Good", "Running", "None"]
    # valid_states_ingest = ["Ingested", "IngestRunning", "IngestFailed", "None"]
    # valid_states_final = any timestamp, or None

    # Create cumulative counts for each state in "3d_pipeline_val"
    # Explicit, enforced order for visual consistency
    val_order = [
        "Good",
        "Failed",
        "Running",
        "WaitingForValidation",
        "None",
    ]

    ingest_order = [
        "Ingested",
        "IngestRunning",
        "IngestFailed",
        "None",
    ]

    final_order = [
        "Completed",
        "Not completed",
    ]

    counts_val = {
        state: np.sum(tile3d_table["3d_pipeline_val"] == state) for state in val_order
    }

    counts_ingest = {
        state: np.sum(tile3d_table["3d_pipeline_ingest"] == state)
        for state in ingest_order
    }

    counts_final = {
        "Completed": np.sum(tile3d_table["3d_pipeline"] != "None"),
        "Not completed": np.sum(tile3d_table["3d_pipeline"] == "None"),
    }

    # Now plot the results as a histogram with numbers above the bars
    fig, axes = plt.subplots(3, 1, figsize=(10, 15))

    # --- Validation ---
    axes[0].bar(val_order, [counts_val[s] for s in val_order], color="skyblue")
    axes[0].set_title("3D Pipeline Validation Status")
    axes[0].set_ylabel("Number of Tiles")
    for i, state in enumerate(val_order):
        count = counts_val[state]
        axes[0].text(i, count + 1, str(count), ha="center")

    # --- Ingest ---
    axes[1].bar(
        ingest_order, [counts_ingest[s] for s in ingest_order], color="lightgreen"
    )
    axes[1].set_title("3D Pipeline Ingest Status")
    axes[1].set_ylabel("Number of Tiles")
    for i, state in enumerate(ingest_order):
        count = counts_ingest[state]
        axes[1].text(i, count + 1, str(count), ha="center")

    # --- Final ---
    axes[2].bar(final_order, [counts_final[s] for s in final_order], color="salmon")
    axes[2].set_title("3D Pipeline Final Completion Status")
    axes[2].set_ylabel("Number of Tiles")
    for i, state in enumerate(final_order):
        count = counts_final[state]
        axes[2].text(i, count + 1, str(count), ha="center")

    for ax in axes:
        ax.set_yscale("log")

    today_str = date.today().isoformat()

    fig.suptitle(
        f"3D Pipeline Progress Overview - {today_str}",
        fontsize=16,
    )

    plt.tight_layout()
    plt.savefig("./plots/3D_pipeline_progress_cumulative.png")
    plt.close()


def check_progress_plot():
    """Check and make a progress plot for 3D pipeline, if it has not been updated in a day."""

    # If the plot hasnt been updated in a day, re-run the plot
    local_file = Path("./plots/3D_pipeline_progress_cumulative.png")
    Path("./plots/").mkdir(exist_ok=True)

    if not local_file.exists():
        print("Creating 3D pipeline progress plot")
        # create progress plot for 3D pipeline
        create_3d_progress_plot()
    else:
        file_mod_time = os.path.getmtime(local_file)
        if (time.time() - file_mod_time) > 86400:  # 86400 seconds = 1 day
            print("Updating 1D pipeline progress plot")
            create_3d_progress_plot()


def run_script_intermittently(
    script_paths, max_pending=20, max_running=50
):
    """simply runs some children scripts if there arent too many jobs running or pending."""

    n_headless_pending = 0
    n_headless_running = 0
    try:
        # Get information about currently open sessions
        df_sessions = get_open_sessions()
        print("Open sessions:")
        print(df_sessions)
        print("\n")

        if len(df_sessions) > 0:
            # Count the number of headless sessions with status 'Pending'
            mask_pending = (df_sessions["type"] == "headless") & (
                df_sessions["status"] == "Pending"
            )
            n_headless_pending = df_sessions[mask_pending].shape[0]
            print(
                f"Number of headless sessions with status 'Pending': {n_headless_pending}"
            )
            # and only the ones that are related to the 3D pipeline
            mask_pending = mask_pending & (
                df_sessions["name"].str.contains("tile")
                | df_sessions["name"].str.contains("ingest")
            )
            n_headless_pending = df_sessions[mask_pending].shape[0]
            print(
                f"Number of *3D pipeline* headless sessions with status 'Pending': {n_headless_pending}"
            )

            # Count the number of headless sessions with status 'Running'
            mask_running = (df_sessions["type"] == "headless") & (
                df_sessions["status"] == "Running"
            )
            n_headless_running = df_sessions[mask_running].shape[0]
            print(
                f"Number of headless sessions with status 'Running': {n_headless_running}"
            )
            # and only the ones that are related to the 3D pipeline
            mask_running = mask_running & (
                df_sessions["name"].str.contains("tile")
                | df_sessions["name"].str.contains("ingest")
            )
            n_headless_running = df_sessions[mask_running].shape[0]
            print(
                f"Number of *3D pipeline* headless sessions with status 'Running': {n_headless_running}"
            )

        # If the number of pending headless sessions is less than e.g. 10, run the script
        if n_headless_pending < max_pending and n_headless_running < max_running:
            for script_path in script_paths:
                print(f"Running script: {script_path}")
                command = ["python", "-m", script_path]
                process = subprocess.Popen(command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,               # line-buffered
                    universal_newlines=True  # ensures \n splitting works across platforms
                )
                util.print_subprocess_output(process, command)
        else:
            if n_headless_pending >= max_pending:
                print("Too many pending headless sessions. Skipping this run.")
            if n_headless_running >= max_running:
                print("Too many running headless sessions. Skipping this run.")
                
        # Check and update the 3D pipeline progress plot
        check_progress_plot()

    except subprocess.CalledProcessError as e:
        print(f"Error occurred while running the script: {e}")
        raise
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        raise
    

@flow(name="control_3D_pipeline", log_prints=True)
def main_flow():    
    # Path to the script to be run intermittently
    script_paths = [
        "possum_pipeline_control.check_status_and_launch_3Dpipeline_v2",
        "possum_pipeline_control.check_ingest_3Dpipeline",
    ]

    # Maximum number of headless jobs pendings, will not submit a session if theres more
    max_pending = 5  # 3d pipeline jobs are quite heavy, so 5 pending is enough

    # Maximum number of headless jobs running. will not submit if theres more
    max_running = 10  # only for 3D pipeline jobs

    run_script_intermittently(
        script_paths, max_pending, max_running
    )

if __name__ == "__main__":
    main_flow()
