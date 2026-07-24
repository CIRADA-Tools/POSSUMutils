"""
The tile3d table is initialised to all-NULL in the 3d_pipeline column.
However, we've already processed some tiles, so we need to fix the status of those tiles
in the 3d_pipeline column to a processed timestamp so that we dont re-process them.

We will set the timestamp to NOW() for all tiles that we have processed until now (2025-11-25)
"""

from __future__ import annotations

import datetime
from typing import Optional

import gspread
import numpy as np
from astropy import table as at

# assume this script is run as a module from the POSSUMutils package
from automation import possum_api_client

GOOGLE_API_TOKEN = "/home/erik/.ssh/psm_gspread_token.json"
VALIDATION_SHEET_URL = "https://docs.google.com/spreadsheets/d/1sWCtxSSzTwjYjhxr1_KVLWG2AnrHwSJf_RWQow7wbH0/"


def run_update_query(
    tiles: np.ndarray,
    timestamp: Optional[datetime.datetime] = None,
) -> str:
    """
    Build an UPDATE query that sets possum.tile_state_band1."3d_pipeline"
    for the given tiles.

    Parameters
    ----------
    tiles : np.ndarray
        1D array of tile ids (integers).
    timestamp : datetime.datetime or None
        If None, uses NOW() in the database.
        Otherwise, uses the given timestamp (to seconds) as a literal.

    Returns
    -------
    str
        The SQL query string.
    """
    tiles = np.asarray(tiles)

    if tiles.size == 0:
        raise ValueError("No tiles provided to update.")

    api = possum_api_client.PossumApiClient()

    updated_rows = 0
    for t in tiles:
        response = api.patch(
            "/3d-pipeline/tiles/update/3d_pipeline/",
            json={
             "band_number": 1,
             "tile_number": t,
             "timestamp": timestamp
            }, #avoid encoding problem in url
        )
        
        num_rows = response.get('rows_updated')
        updated_rows += num_rows

    return updated_rows


def update_3d_pipeline_for_tiles(
    tiles: np.ndarray,
    timestamp: Optional[datetime.datetime] = None,
) -> None:
    """
    Execute the UPDATE to set "3d_pipeline" for the given tiles.

    Parameters
    ----------
    tiles : np.ndarray
        1D array of tile ids (integers).
    timestamp : datetime.datetime or None
        If None, uses NOW() in the database.
    """
    num_rows = run_update_query(tiles, timestamp=timestamp)

    print(f"Update executed successfully. Updated {num_rows} rows")


def load_tile_validation_sheet(band: str = "943MHz") -> at.Table:
    """
    Load the Cameron's STATUS SHEET for the POSSUM 3D Tile Pipeline for a given band.

    Args:
        band (str): Supported value: '943MHz' or '1367MHz' (Band 1).

    Returns:
        astropy.table.Table: Table containing the data from the validation sheet.
    """
    gc = gspread.service_account(filename=GOOGLE_API_TOKEN)
    ps = gc.open_by_url(VALIDATION_SHEET_URL)

    if band not in ["943MHz", "1367MHz"]:
        raise ValueError(
            "Unsupported band. Supported values are '943MHz' and '1367MHz'."
        )

    band_number = "1" if band == "943MHz" else "2"
    tile_sheet = ps.worksheet(f"Survey Tiles - Band {band_number}")
    tile_data = tile_sheet.get_all_values()

    column_names = tile_data[0]
    tile_table = at.Table(np.array(tile_data)[1:], names=column_names)
    return tile_table


if __name__ == "__main__":
    # Load the validation sheet for Band 1 (943MHz)
    tile_table = load_tile_validation_sheet(band="943MHz")

    # Find tiles that have been processed (3d_pipeline == 'WaitingForValidation')
    processed_tiles = (
        tile_table[(tile_table["3d_pipeline"] == "WaitingForValidation")]["tile_id"]
        .astype(int)
        .data
    )

    print(f"Number of tiles to update: {len(processed_tiles)}")

    # Option A: use NOW() on the DB side
    update_3d_pipeline_for_tiles(processed_tiles, timestamp=None)

    # Option B: use an explicit timestamp
    # explicit_ts = datetime.datetime(2025, 1, 1, 12, 0, 0)
    # update_3d_pipeline_for_tiles(example_tiles, timestamp=explicit_ts)
