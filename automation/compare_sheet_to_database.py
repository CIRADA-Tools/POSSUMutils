"""
Script to verify that the entries in the database match those in the validation Google sheet.

This is intended to be used once during the transition from the validation sheet
to the database as the source of truth.

Make sure config.env is set.
"""

import astropy.table as at
import gspread
import numpy as np
import tqdm

# assume this script is run as a module from the POSSUMutils package
from automation import possum_api_client # noqa: E402

GOOGLE_API_TOKEN = "/home/erik/.ssh/neural-networks--1524580309831-c5c723e2468e.json"
VALIDATION_SHEET_URL = (
    "https://docs.google.com/spreadsheets/"
    "d/1_88omfcwplz0dTMnXpCj27x-WSZaSmR-TEsYFmBD43k"
)


def get_tile_num(tile_str: str) -> str:
    """
    Normalise tile number to a string. If the value is None, return an empty string.

    Args:
        tile_str: Tile identifier, can be None or numeric/string.

    Returns:
        str: Normalised tile str ('' for None).
    """
    return str(tile_str).replace("None", "")


def get_sbid_num(sbid: str | int) -> str:
    """
    Remove ASKAP- prefix from sbid if present and normalise to string.

    Args:
        sbid: SBID value as stored in sheet or database.

    Returns:
        str: SBID without the ASKAP- prefix.
    """
    sbid_str = str(sbid)
    if sbid_str.startswith("ASKAP-"):
        return sbid_str.replace("ASKAP-", "")
    return sbid_str


def normalize_value(value: str | None) -> str:
    """
    Normalise a value for comparison between sheet and database.

    - None, 'None', 'nan', 'NaN' -> ''
    - Strips surrounding whitespace
    - Everything is compared as a string

    Args:
        value: Any value from sheet or database.

    Returns:
        str: Normalised string representation.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in {"none", "nan"}:
        return ""
    return s


def load_validation_sheet(band: str = "943MHz") -> at.Table:
    """
    Load the POSSUM Partial Tile Pipeline validation Google sheet for the given band.

    Args:
        band (str): Supported value: '943MHz' (Band 1).
                    (Not supported yet: '1367MHz' / Band 2.)

    Returns:
        astropy.table.Table: Table containing the data from the validation sheet.
    """
    gc = gspread.service_account(filename=GOOGLE_API_TOKEN)
    ps = gc.open_by_url(VALIDATION_SHEET_URL)

    band_number = "1" if band == "943MHz" else "2"
    tile_sheet = ps.worksheet(f"Partial Tile Pipeline - regions - Band {band_number}")
    tile_data = tile_sheet.get_all_values()

    column_names = tile_data[0]
    tile_table = at.Table(np.array(tile_data)[1:], names=column_names)
    return tile_table


def get_partial_tiles_database(band_number: int = 1) -> list[tuple]:
    """
    Get the full partial tile table for a given band from the database.

    In the database, entries live in possum.partial_tile_1d_pipeline_band{band_number}
    and we join to possum.observation to get the SBID.

    Args:
        band_number (int): The band number (1 or 2) to check.

    Returns:
        list[tuple]: Each tuple is a row from the database with p.* and o.sbid as last field.
    """
    conn = possum_api_client.PossumApiClient()
    
    print(
        f"Fetching full partial tiles data table for 1D pipeline run for band {band_number} "
        "from the database."
    )
    rows = conn.get(f"/partial_tiles/sbid/band{band_number}/")
    
    return rows


def build_column_indices(
    tile_table: at.Table, sample_db_entry: tuple
) -> dict[str, int]:
    """
    Build a mapping from logical column names to indices in the DB entry tuple.

    We rely on the known order of columns in possum.partial_tile_1d_pipeline_band{n}
    and the appended sbid at the end of the SELECT.

    Args:
        tile_table: Astropy table from the validation sheet.
        sample_db_entry: One representative DB row (tuple).

    Returns:
        dict[str, int]: Mapping from logical field names to indices in the DB tuple.
    """
    db_len = len(sample_db_entry)

    columns_to_index = {
        # In DB row: index 0 is primary key id, then field_name
        "field_name": tile_table.colnames.index("field_name") + 1,
        # For the rest, the +1 from the PK is canceled by -1 because sbid is in the sheet but not in p.*
        "tile1": tile_table.colnames.index("tile1"),
        "tile2": tile_table.colnames.index("tile2"),
        "tile3": tile_table.colnames.index("tile3"),
        "tile4": tile_table.colnames.index("tile4"),
        "type": tile_table.colnames.index("type"),
        "number_sources": tile_table.colnames.index("number_sources"),
        "1d_pipeline": tile_table.colnames.index("1d_pipeline"),
        # 1d pipeline validation is not in database, because its moved to the observations table
        # "1d_pipeline_validation": tile_table.colnames.index("1d_pipeline_validation"),
        # sbid is last column according to the query
        "sbid": db_len - 1,
    }

    return columns_to_index


def build_sheet_indexes(
    tile_table: at.Table,
) -> tuple[
    dict[tuple[str, str, tuple[str, str, str, str], str], list[at.Row]],
    dict[tuple[str, str], list[at.Row]],
]:
    """
    Build lookup indexes for the sheet:

    - full_index: keyed by (field_name, sbid, (tile1..4), type)
    - field_sbid_index: keyed by (field_name, sbid)

    Args:
        tile_table: Astropy table from the validation sheet.

    Returns:
        (full_index, field_sbid_index)
    """
    full_index: dict[tuple[str, str, tuple[str, str, str, str], str], list[at.Row]] = {}
    field_sbid_index: dict[tuple[str, str], list[at.Row]] = {}

    for row in tile_table:
        field_name = row["field_name"]
        sbid = get_sbid_num(row["sbid"])

        tiles = (
            get_tile_num(row["tile1"]),
            get_tile_num(row["tile2"]),
            get_tile_num(row["tile3"]),
            get_tile_num(row["tile4"]),
        )
        tile_type = row["type"]

        full_key = (field_name, sbid, tiles, tile_type)
        field_sbid_key = (field_name, sbid)

        full_index.setdefault(full_key, []).append(row)
        field_sbid_index.setdefault(field_sbid_key, []).append(row)

    return full_index, field_sbid_index


def extract_db_key_parts(
    entry: tuple, columns_to_index: dict[str, int]
) -> tuple[str, str, tuple[str, str, str, str], str]:
    """
    Extract and normalise the key components from a DB entry.

    Args:
        entry: DB row as a tuple.
        columns_to_index: Mapping of logical names to indices in the tuple.

    Returns:
        (field_name, sbid, (tile1..4), type)
    """
    field_name = entry[columns_to_index["field_name"]]
    sbid = get_sbid_num(entry[columns_to_index["sbid"]])

    tiles = (
        get_tile_num(entry[columns_to_index["tile1"]]),
        get_tile_num(entry[columns_to_index["tile2"]]),
        get_tile_num(entry[columns_to_index["tile3"]]),
        get_tile_num(entry[columns_to_index["tile4"]]),
    )
    tile_type = entry[columns_to_index["type"]]

    return field_name, sbid, tiles, tile_type


def build_db_indexes(
    db_rows: list[tuple], columns_to_index: dict[str, int]
) -> tuple[
    dict[tuple[str, str, tuple[str, str, str, str], str], list[tuple]],
    dict[tuple[str, str], list[tuple]],
]:
    """
    Build lookup indexes for the database:

    - full_index: keyed by (field_name, sbid, (tile1..4), type)
    - field_sbid_index: keyed by (field_name, sbid)

    Args:
        db_rows: List of DB rows as tuples.
        columns_to_index: Mapping of logical names to indices in the tuple.

    Returns:
        (full_index, field_sbid_index)
    """
    full_index: dict[tuple[str, str, tuple[str, str, str, str], str], list[tuple]] = {}
    field_sbid_index: dict[tuple[str, str], list[tuple]] = {}

    for entry in db_rows:
        field_name, sbid, tiles, tile_type = extract_db_key_parts(
            entry, columns_to_index
        )

        full_key = (field_name, sbid, tiles, tile_type)
        field_sbid_key = (field_name, sbid)

        full_index.setdefault(full_key, []).append(entry)
        field_sbid_index.setdefault(field_sbid_key, []).append(entry)

    return full_index, field_sbid_index


def get_observation_state_validation(band_number: int = 1) -> dict[str, str]:
    """
    Load 1d_pipeline_validation state per field from possum.observation_state_band{band_number}.

    Assumes the table has columns:
        - field_name
        - 1d_pipeline_validation

    Args:
        band_number: Band index (1 or 2).

    Returns:
        dict mapping field_name -> normalised 1d_pipeline_validation state.
    """
    conn = possum_api_client.PossumApiClient()
    rows = conn.get_json(f"/1d-pipeline/observations/single-sb-1d-pipeline/full-table/band{band_number}/")
    
    state_by_field = {}
    for row in rows:
        state_by_field[row["name"]] = normalize_value(row["1d_pipeline_validation"])
 
    return state_by_field


def compare_database_to_sheet(
    db_rows: list[tuple],
    sheet_full_index: dict[
        tuple[str, str, tuple[str, str, str, str], str], list[at.Row]
    ],
    sheet_field_sbid_index: dict[tuple[str, str], list[at.Row]],
    columns_to_index: dict[str, int],
) -> list[tuple[str, str, str]]:
    """
    Compare database entries against the sheet.

    Checks:
    - Presence of (field_name, sbid) in sheet
    - Presence of matching (field_name, sbid, tile1..4, type)
    - Equality of number_sources, 1d_pipeline

    Args:
        db_rows: List of DB rows.
        sheet_full_index: Sheet index keyed by full key.
        sheet_field_sbid_index: Sheet index keyed by (field_name, sbid).
        columns_to_index: Mapping of DB tuple indices.

    Returns:
        List of mismatch tuples: (field_name, sbid, reason).
    """
    mismatches: list[tuple[str, str, str]] = []

    for entry in tqdm.tqdm(db_rows, desc="Comparing database entries to Google Sheet"):
        field_name, sbid, tiles, tile_type = extract_db_key_parts(
            entry, columns_to_index
        )

        field_sbid_key = (field_name, sbid)
        if field_sbid_key not in sheet_field_sbid_index:
            mismatches.append((field_name, sbid, "Whole field not found in sheet"))
            continue

        full_key = (field_name, sbid, tiles, tile_type)
        matches = sheet_full_index.get(full_key, [])

        if not matches:
            mismatches.append((field_name, sbid, "Tile numbers or type mismatch"))
            continue

        if len(matches) > 1:
            mismatches.append(
                (field_name, sbid, "Multiple matching rows found in sheet")
            )
            continue

        # At this point we have exactly one matching row; check additional columns.
        sheet_row = matches[0]

        sheet_number_sources = normalize_value(sheet_row["number_sources"])
        db_number_sources = normalize_value(entry[columns_to_index["number_sources"]])

        if sheet_number_sources != db_number_sources:
            mismatches.append(
                (
                    field_name,
                    sbid,
                    f"number_sources mismatch (sheet={sheet_number_sources}, db={db_number_sources})",
                )
            )

        sheet_1d_pipeline = normalize_value(sheet_row["1d_pipeline"])
        db_1d_pipeline = normalize_value(entry[columns_to_index["1d_pipeline"]])

        if sheet_1d_pipeline != db_1d_pipeline:
            mismatches.append(
                (
                    field_name,
                    sbid,
                    f"1d_pipeline mismatch (sheet={sheet_1d_pipeline}, db={db_1d_pipeline})",
                )
            )

    return mismatches


def compare_sheet_to_database(
    tile_table: at.Table,
    db_full_index: dict[tuple[str, str, tuple[str, str, str, str], str], list[tuple]],
    db_field_sbid_index: dict[tuple[str, str], list[tuple]],
    columns_to_index: dict[str, int],
) -> list[tuple[str, str, str]]:
    """
    Check that every sheet entry is present in the database and that
    number_sources and 1d_pipeline agree.

    Args:
        tile_table: Astropy table from the validation sheet.
        db_full_index: DB index keyed by full key
                       (field_name, sbid, (tile1..4), type).
        db_field_sbid_index: DB index keyed by (field_name, sbid).
        columns_to_index: Mapping of logical DB column names to tuple indices.

    Returns:
        List of mismatch tuples: (field_name, sbid, reason).
    """
    mismatches: list[tuple[str, str, str]] = []

    for row in tqdm.tqdm(tile_table, desc="Checking Google Sheet entries in database"):
        field_name = row["field_name"]
        sbid = get_sbid_num(row["sbid"])

        tiles = (
            get_tile_num(row["tile1"]),
            get_tile_num(row["tile2"]),
            get_tile_num(row["tile3"]),
            get_tile_num(row["tile4"]),
        )
        tile_type = row["type"]

        field_sbid_key = (field_name, sbid)
        if field_sbid_key not in db_field_sbid_index:
            mismatches.append((field_name, sbid, "Whole field not found in database"))
            continue

        full_key = (field_name, sbid, tiles, tile_type)
        matches = db_full_index.get(full_key, [])

        if not matches:
            mismatches.append(
                (
                    field_name,
                    sbid,
                    "Tile numbers or type mismatch (missing in database)",
                )
            )
            continue

        if len(matches) > 1:
            mismatches.append(
                (field_name, sbid, "Multiple matching rows found in database")
            )
            # Still continue checking values against the first match or skip?
            # Here we skip to keep behaviour consistent with DB->sheet check.
            continue

        # Exactly one DB row for this sheet row: now compare values.
        db_entry = matches[0]

        sheet_number_sources = normalize_value(row["number_sources"])
        db_number_sources = normalize_value(
            db_entry[columns_to_index["number_sources"]]
        )

        if sheet_number_sources != db_number_sources:
            mismatches.append(
                (
                    field_name,
                    sbid,
                    "number_sources mismatch (sheet="
                    f"{sheet_number_sources}, db={db_number_sources})",
                )
            )

        sheet_1d_pipeline = normalize_value(row["1d_pipeline"])
        db_1d_pipeline = normalize_value(db_entry[columns_to_index["1d_pipeline"]])

        if sheet_1d_pipeline != db_1d_pipeline:
            mismatches.append(
                (
                    field_name,
                    sbid,
                    "1d_pipeline mismatch (sheet="
                    f"{sheet_1d_pipeline}, db={db_1d_pipeline})",
                )
            )

    return mismatches


def compare_sheet_validation_to_observation_state(
    tile_table: at.Table,
    band_number: int = 1,
) -> list[tuple[str, str]]:
    """
    Compare 1d_pipeline_validation between the sheet and possum.observation_state_band{band_number}.

    The sheet may have multiple rows per field_name, but they should all have the same
    1d_pipeline_validation value for that field. The database has one row per field_name.

    Checks (sheet -> database only):
      1. Within the sheet, for each field_name, all 1d_pipeline_validation values
         must be identical (after normalisation).
      2. The DB must have an entry for each field_name present in the sheet.
      3. The DB value must match the sheet value for that field_name.

    Args:
        tile_table: Astropy table from the validation sheet.
        band_number: Band index to use for possum.observation_state_band{band_number}.

    Returns:
        List of (field_name, reason) for mismatches.
    """
    mismatches: list[tuple[str, str]] = []

    # 1. Aggregate sheet values per field_name, enforcing consistency.
    sheet_state_by_field: dict[str, str] = {}

    for row in tqdm.tqdm(
        tile_table, desc="Aggregating 1d_pipeline_validation from sheet"
    ):
        field_name = str(row["field_name"])
        sheet_val = normalize_value(row["1d_pipeline_validation"])

        # If the sheet uses blank/None as a valid state, we keep that too.
        if field_name not in sheet_state_by_field:
            sheet_state_by_field[field_name] = sheet_val
        else:
            if sheet_state_by_field[field_name] != sheet_val:
                mismatches.append(
                    (
                        field_name,
                        f"Inconsistent 1d_pipeline_validation in sheet: "
                        f"{sheet_state_by_field[field_name]} vs {sheet_val}",
                    )
                )

    # 2. Load DB observation_state and compare.
    db_state_by_field = get_observation_state_validation(band_number=band_number)

    for field_name, sheet_val in tqdm.tqdm(
        sheet_state_by_field.items(),
        desc="Comparing sheet 1d_pipeline_validation to observation_state",
    ):
        if field_name not in db_state_by_field:
            mismatches.append(
                (
                    field_name,
                    f"Field not found in possum.observation_state_band{band_number}",
                )
            )
            continue

        db_val = db_state_by_field[field_name]
        if sheet_val != db_val:
            mismatches.append(
                (
                    field_name,
                    f"1d_pipeline_validation mismatch (sheet={sheet_val}, db={db_val})",
                )
            )

    return mismatches


def main() -> None:
    band = "943MHz"
    band_number = 1

    # Load the validation sheet for the band
    tile_table = load_validation_sheet(band=band)

    # Get database rows
    partial_tiles_db = get_partial_tiles_database(band_number=band_number)

    # Quick sanity check: same number of rows
    assert len(tile_table) == len(partial_tiles_db), (
        "Number of entries in sheet and database do not match."
    )

    # Build column index mapping for DB tuples
    columns_to_index = build_column_indices(tile_table, partial_tiles_db[0])

    # Build indexes
    sheet_full_index, sheet_field_sbid_index = build_sheet_indexes(tile_table)
    db_full_index, db_field_sbid_index = build_db_indexes(
        partial_tiles_db, columns_to_index
    )

    # Compare DB -> Sheet (including extra columns)
    db_vs_sheet_mismatches = compare_database_to_sheet(
        partial_tiles_db,
        sheet_full_index,
        sheet_field_sbid_index,
        columns_to_index,
    )

    sheet_vs_db_mismatches = compare_sheet_to_database(
        tile_table, db_full_index, db_field_sbid_index, columns_to_index
    )

    if not db_vs_sheet_mismatches and not sheet_vs_db_mismatches:
        print("All entries match between database and Google Sheet (both directions).")
        return

    if db_vs_sheet_mismatches:
        db_vs_sheet_mismatches = np.unique(db_vs_sheet_mismatches, axis=0).tolist()
        print("Mismatches found when comparing database entries to Google Sheet:")
        for field_name, sbid, reason in db_vs_sheet_mismatches:
            print(field_name, sbid, "-", reason)
    else:
        print(" ====== All database entries match the Google Sheet. =====")

    if sheet_vs_db_mismatches:
        sheet_vs_db_mismatches = np.unique(sheet_vs_db_mismatches, axis=0).tolist()
        print("Mismatches found when checking Google Sheet entries in database:")
        for field_name, sbid, reason in sheet_vs_db_mismatches:
            print(field_name, sbid, "-", reason)
    else:
        print(" ====== All Google Sheet entries are present in the database. =====")

    # Compare "1d_pipeline_validation" column from sheet -> to new "possum.observation_state_band1" table
    validation_mismatches = compare_sheet_validation_to_observation_state(
        tile_table, band_number=band_number
    )

    if validation_mismatches:
        print(
            "Mismatches found when comparing sheet 1d_pipeline_validation "
            f"to possum.observation_state_band{band_number}:"
        )
        for field_name, reason in validation_mismatches:
            print(field_name, "-", reason)


if __name__ == "__main__":
    main()
