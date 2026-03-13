"""
Database query functions for interacting with the ausSRC database.
"""

import os

import psycopg2
from astropy.table import Table
from dotenv import load_dotenv
from prefect.blocks.system import Secret


def rows_to_table(rows, colnames=None, dtype=None):
    """
    Convert a list of row tuples and column names into an astropy Table.
    """
    if not rows:
        # Empty table, but we might still know the column names
        return Table(names=colnames or [], dtype=dtype)

    if colnames is None:
        ncols = len(rows[0])
        colnames = [f"col{i}" for i in range(ncols)]

    return Table(rows=rows, names=colnames, dtype=dtype)


def get_database_connection(
    test: bool, database_config_path=None
):
    """
    Initiate a database connection.
    Args:
    - test: True if this should connect to the test database set up in test.env
    """
    conn_params = get_database_parameters(test, database_config_path)
    return psycopg2.connect(**conn_params)


def get_database_parameters(
    test=False, database_config_path=None
):
    """
    Get database parameters from env file (test.env for test, config.env otherwise)
    """
    test_db_name = 'possum_test'
    if test:
        load_dotenv(dotenv_path='automation/unit_tests/test.env')
        # Check that we're not accidentally connecting to prod DB in test mode
        if test_db_name != os.getenv('TEST_DATABASE_NAME'):
            raise ConnectionError("""Please make sure your test database is named possum_test!
                        This is to avoid accidentally using real prod database in tests.""")
        db_name = os.getenv('TEST_DATABASE_NAME')
        db_user = os.getenv('TEST_DATABASE_USER')
        db_pass = os.getenv('TEST_DATABASE_PASSWORD')
        db_host = os.getenv('TEST_DATABASE_HOST')
        db_port = os.getenv('TEST_DATABASE_PORT')
    else:
        if database_config_path:
            if "test" in database_config_path.lower():
                raise ValueError(
                    "Database config path should not point to a test environment when test=False"
                )

            # Get database connection details from config.env file
            print(f"Loading database config from {database_config_path}")
            load_dotenv(dotenv_path=database_config_path)
            db_name = os.getenv("DATABASE_NAME")
            db_user = os.getenv("DATABASE_USER")
            db_pass = os.getenv("DATABASE_PASSWORD")
            db_host = os.getenv("DATABASE_HOST")
            db_port = os.getenv("DATABASE_PORT")
        else:
            # Load Prefect secrets
            print("Loading database config from Prefect secrets")
            db_name = Secret.load("database-name", _sync=True).get()
            db_user = Secret.load("database-user", _sync=True).get()
            db_pass = Secret.load("database-password", _sync=True).get()
            db_host = Secret.load("database-host", _sync=True).get()
            db_port = Secret.load("database-port", _sync=True).get()

    if not all([db_name, db_user, db_pass, db_host, db_port]):
        # fall back on Prefect secrets if any of the parameters are missing
        print("Some database parameters are missing from the config file. Loading from Prefect secrets.")
        db_name = db_name or Secret.load("database-name", _sync=True).get()
        db_user = db_user or Secret.load("database-user", _sync=True).get()
        db_pass = db_pass or Secret.load("database-password", _sync=True).get()
        db_host = db_host or Secret.load("database-host", _sync=True).get()
        db_port = db_port or Secret.load("database-port", _sync=True).get()

    return {
        "dbname": db_name,
        "user": db_user,
        "password": db_pass,
        "host": db_host,
        "port": db_port,
    }

def execute_update_query(query, conn, params=None, verbose=False):
    """
    Execute an update SQL query and return the number of rows affected.

    Args:
    query (str): The SQL query to execute.
    params (tuple): Optional parameters for the SQL query.

    Returns:
    list: The number of rows affected.
    """
    rows_affected = 0
    try:
        with conn.cursor() as cursor:
            # Execute the query
            if verbose:
                print(f"Executing database query: {query}")
                if params:
                    print(f"With parameters: {params}")
            cursor.execute(query, params)
            conn.commit()
            rows_affected = cursor.rowcount
            if verbose:
                print(f"{rows_affected} rows affected.")
    except Exception as e:
        print(f"An error occurred: {e}")
        raise
    return rows_affected


def execute_query(
    query, database_connection, params=None, verbose=False, return_colnames=False
):
    """
    Execute a SQL query and return the results.

    Args:
        query (str): The SQL query to execute.
        database_connection: An open DB-API 2.0 connection.
        params (tuple): Optional parameters for the SQL query.
        verbose (bool): If True, print the query before executing.
        return_colnames (bool): If True, also return the column names.

    Returns:
        list or (list, list): The results of the query, and optionally
        a list of column names.
    """
    results = []
    colnames = []
    try:
        with database_connection.cursor() as cursor:
            if verbose:
                if params:
                    print(f"Executing database query: {query} with {params}")
                else:
                    print(f"Executing database query: {query}")

            cursor.execute(query, params)

            if cursor.description is not None:
                colnames = [desc[0] for desc in cursor.description]
                results = cursor.fetchall()
    except Exception as e:
        print(f"An error occurred: {e}")
        raise

    if return_colnames:
        return results, colnames
    return results


def update_3d_pipeline_table(tile_number, band_number, status, column_name, conn):
    """
    Update the 'tile_state_band{band_number}' table with given column name.
    Possible values for '3d_pipeline_ingest':
        - Ingested
        - IngestFailed
        _ IngestRunning
    Possible values for '3d_pipeline_val':
        - Running (Job has been submitted and is currently running)
        - Failed (Job failed to run)
        - WaitingForValidation (Job has finished running successfully, waiting for human validation)
        - Good/Bad (Currently has to be manually set after human validation)
    Possible values for '3d_pipeline':
        - A timestamp when the job has completed.

    Args:
    tile_number (str): The tile number to update.
    band_number (str): 1 or 2
    status (str): The status to set in the respective column.
    column_name (str) : The column to set.
    conn: Database connection

    Return: 0 if tile was not found, 1 if successful
    """
    # validate params
    validate_band_number(band_number)
    if column_name not in (
        "3d_pipeline",
        "3d_pipeline_val",
        "3d_pipeline_ingest",
        "3d_val_link",
    ):
        raise ValueError(
            f"Updating {column_name} in possum.tile_state_band{band_number} is not allowed!"
        )

    print(
        f"Updating POSSUM tile database table for band{band_number} with {column_name} to {status}"
    )
    query = f"""
        UPDATE possum.tile_state_band{band_number}
        SET "{column_name}" = %s -- status
        WHERE tile = %s; -- tile_number
    """
    return execute_update_query(query, conn, (status, tile_number))


def update_1d_pipeline_table(field_name, band_number, status, column_name, conn):
    """
    Update the single_1d_pipeline_validation{band_number} column in the observation table.
    This is to the equivalent to POSSUM pipeline status sheet: Survey Fields - Band {band_number}

    Args:
    field_name       : observation.field_name
    band_number      : '1' or '2'
    status (str): The status to set in the 'status_column' column.
    column_name.     : The column to set

    """
    validate_band_number(band_number)
    if column_name.lower() not in ("1d_pipeline_validation", "single_sb_1d_pipeline"):
        raise ValueError(
            f"Not allowed to update {column_name} in observation_state_band{band_number}!"
        )
    print(
        f"Updating POSSUM observation_state_band{band_number} table with {column_name} status"
    )
    query = f"""
        INSERT INTO possum.observation_state_band{band_number}
        (name, "{column_name}")
        VALUES (%s,%s) -- field_name, status
        ON CONFLICT (name) DO UPDATE
        SET "{column_name}" = %s; -- status
    """
    return execute_update_query(query, conn, (field_name, status, status))


def find_boundary_issues(sbid, observation, band_number, conn):
    """
    Check if there are any entries in partial_tile_1d_pipeline for the given sbid and observation
    where type indicates it crosses a projection boundary.
    This is to identify potential issues with tiles that cross projection boundaries.
    """
    print(
        f"Checking for projection boundary issues for SBID: {sbid}, Observation: {observation}"
    )
    query = f"""
        SELECT EXISTS (
            SELECT 1
            FROM possum.partial_tile_1d_pipeline_band{band_number}
            WHERE observation = %s AND LOWER(type) like '%%crosses projection boundary%%'
        ) AS match_found;
    """
    results = execute_query(query, conn, (observation,))
    issues_found = results[0][0]
    if issues_found is True:
        print("Boundary issues found.")
    else:
        print("No boundary issues found.")
    return issues_found


def validate_band_number(band_number):
    """
    Making sure band number is valid
    """
    if str(band_number) not in ["1", "2"]:
        raise ValueError("band_number must be either 1 or 2")


def get_tiles_for_pipeline_run(conn, band_number):
    """
    Get a list of tile numbers that should be ready to be processed by the 3D pipeline

    In the database, this is when:
    tile_state_band.cube_state = 'COMPLETED' and tile.3d_pipeline_val = [null]

    In POSSUM pipeline status sheet, this is the equivalent of:
    'aus_src' column is not empty and '3d_pipeline' column is empty for the given band number.

    Args:
    band_number (int): The band number (1 or 2) to check.

    Returns:
    list: A list of tile numbers that satisfy the conditions.
    """
    validate_band_number(band_number)
    print(
        f"Fetching tiles ready for 3D pipeline run for band {band_number} from the database."
    )
    query = f"""
        SELECT tile FROM possum.tile_state_band{band_number}
        WHERE UPPER(cube_state) = 'COMPLETED'
        AND UPPER(mfs_state) = 'COMPLETED'
        AND ("3d_pipeline_val" IS NULL OR TRIM("3d_pipeline_val") = '')
    """
    return execute_query(query, conn)


def get_tiles_for_ingest(band_number, conn):
    """
    Get a list of 3D pipeline tile numbers that should be ready to be ingested.
    i.e. tile_state_band1.'3d_pipeline_val' = 'Good' and
    tile_state_band1.'3d_pipeline_ingest' is NULL

    Args:
    band_number (int): The band number (1 or 2) to check.

    Returns:
    list: A list of tile numbers that satisfy the conditions.
    """
    validate_band_number(band_number)
    print(
        f"Fetching tiles ready for 3D pipeline run for band {band_number} from the database."
    )
    query = f"""
        SELECT DISTINCT tile
        FROM possum.tile_state_band{band_number} tile_3d
        WHERE LOWER(tile_3d."3d_pipeline_val") = 'good' AND
        (tile_3d."3d_pipeline_ingest" IS NULL OR
        TRIM(tile_3d."3d_pipeline_ingest") = '')
        ORDER BY tile
    """
    results = execute_query(query, conn)
    # flatten tile ids into an array
    return [row[0] for row in results]


def update_partial_tile_1d_pipeline_status(
    field_name, tile_numbers, band_number, status, conn
):
    """
    Update 1d_pipeline in partial_tile_1d_pipeline_band{band_number} table for a set of tiles.
    This replaces the 1d_pipeline column in the POSSUM pipeline validation Google sheet:
    Partial Tile Pipeline - regions - Band {band_number}

    Args:
    field_name (str): The field ID with 'EMU_' or 'WALLABY_' prefix.
    tile_numbers (tuple): The tile numbers to update.
    band_number: '1' or '2'
    status (str): The new validation status to set.
    """
    print(
        f"Updating POSSUM partial_tile_1d_pipeline_band{band_number}.1d_pipeline in the database"
    )
    t1, t2, t3, t4 = tile_numbers
    query = f"""
        UPDATE possum.partial_tile_1d_pipeline_band{band_number}
        SET "1d_pipeline" = %s -- status
        WHERE observation = %s -- field_name
    """
    args = (status, field_name)
    # Check for NULLS in tile numbers and make sure the query says IS NULL and not = NULL so it works
    for i, tile in enumerate([t1, t2, t3, t4], start=1):
        if tile is None or tile.strip() == "":  # If tile is None, use IS NULL
            # put condition on newline
            query += f"\n AND tile{i} IS NULL"
        else:  # Otherwise, use equality
            # put condition on newline, careful with the commenting!
            query += f"\n AND tile{i} = %s -- tile"
            args = args + (tile,)

    row_num = execute_update_query(query, conn, args)
    if row_num == 1:
        print(
            f"Updated row with tiles {tile_numbers} status to {status} in '1d_pipeline' column."
        )
    if row_num == 0:
        print(
            f"No matching row found to update for field {field_name} with tiles {tile_numbers}."
        )
        raise ValueError(
            f"Field {field_name} with tiles {tile_numbers} not found in the database d."
        )
    if row_num > 1:
        print(
            f"Warning: Multiple ({row_num}) rows updated for field {field_name} with tiles {tile_numbers}."
        )
        raise ValueError(
            f"Multiple ({row_num}) rows updated for field {field_name} with tiles {tile_numbers}."
        )


def get_partial_tiles_for_1d_pipeline_run(band_number, conn):
    """
    Get a list of partial tiles that should be ready to be processed by the 1D pipeline
    In the database, this is when:
    partial_tile_1d_pipeline_band{band_number}.sbid is not NULL, number_sources is not NULL,
    and 1d_pipeline is NULL

    Args:
    band_number (int): The band number (1 or 2) to check.

    Returns:
    list: A list of tuples containing (field_ID, sbid, (tile1, tile2, tile3, tile4))
    that satisfy the conditions.
    """
    validate_band_number(band_number)
    print(f"""Fetching partial tiles ready for 1D pipeline run for band {band_number}
          from the database.""")
    query = f"""
        SELECT pt.observation, ob.sbid, pt.tile1, pt.tile2, pt.tile3, pt.tile4
        FROM possum.partial_tile_1d_pipeline_band{band_number} pt,
        possum.observation ob
        WHERE ob.sbid IS NOT NULL AND TRIM(ob.sbid) != ''
          AND ob.name = pt.observation
          AND pt.number_sources IS NOT NULL
          AND (pt."1d_pipeline" IS NULL or TRIM(pt."1d_pipeline") = '')
        ORDER BY id;
    """
    # added order by id to have consistent ordering for tests
    return execute_query(query, conn)


def get_observations_with_complete_partial_tiles(band_number, conn):
    """
    For each observation, check if all '1d_pipeline' is "Completed" and '1d_pipeline_validation' is empty
    Return rows of: (observation, sbid, all_complete)
    for which all_complete is True if all partial tiles for that observation and sbid
    have '1d_pipeline' marked as "Completed" and '1d_pipeline_validation' for the observation is NULL
    """
    sql = f"""
        SELECT pt.observation, ob1.sbid,
        CASE
            WHEN EXISTS (
                SELECT 1
                FROM possum.partial_tile_1d_pipeline_band{band_number} pt2
                WHERE pt2.observation = pt.observation
                AND (LOWER(pt2."1d_pipeline") != 'completed' OR pt2."1d_pipeline" IS NULL)
                ) THEN false
            WHEN (ob."1d_pipeline_validation" IS NULL OR TRIM(ob."1d_pipeline_validation") = '') AND LOWER(pt."1d_pipeline") = 'completed'
                THEN true
            ELSE
                false
        END AS all_complete
        FROM possum.partial_tile_1d_pipeline_band{band_number} pt, possum.observation_state_band{band_number} ob, possum.observation as ob1
        WHERE ob.name = pt.observation and ob1.name = ob.name
        GROUP BY pt.observation, ob1.sbid, ob."1d_pipeline_validation", pt."1d_pipeline";
    """
    return execute_query(sql, conn)


def get_observations_non_edge_rows(band_number, conn):
    """
    For each observation, check if all '1d_pipeline' is "Completed" and 1d_pipeline_validation' is empty,
    except if any partial tile type is an edge case (includes "crosses projection boundary").
    Return rows of: (observation, sbid, non_edge_complete)
    for which non_edge_complete is True if all partial tiles for that observation and sbid
    have '1d_pipeline' marked as "Completed" and '1d_pipeline_validation' for the observation is NULL
    disregarding those that have "crosses projection boundary" in its type.
    """
    sql = f"""
    SELECT
        ob.name AS observation,
        ob.sbid,
        CASE
            WHEN
                -- Observation-level validation must be empty
                (obs."1d_pipeline_validation" IS NULL
                OR TRIM(obs."1d_pipeline_validation") = '')

                -- And there must be NO NON-boundary-crossing tile that is not completed
                -- i.e. all non-boundary-crossing tiles must be completed
                -- important to test for IS NULL because != 'completed' does not catch NULLs
                AND NOT EXISTS (
                    SELECT 1
                    FROM possum.partial_tile_1d_pipeline_band{band_number} pt2
                    WHERE pt2.observation = ob.name
                    AND LOWER(pt2.type) NOT LIKE '%crosses projection boundary%'
                    AND (LOWER(pt2."1d_pipeline") != 'completed' OR pt2."1d_pipeline" IS NULL)
                )

                -- And there must be at least one relevant tile at all
                -- otherwise, all complete would be true for observations not yet in the partial_tile_1d_pipeline_band table
                AND EXISTS (
                    SELECT 1
                    FROM possum.partial_tile_1d_pipeline_band{band_number} pt3
                    WHERE pt3.observation = ob.name
                    AND LOWER(pt3.type) NOT LIKE '%crosses projection boundary%'
                )
            THEN true
            ELSE false
        END AS all_complete
    FROM possum.observation ob
    JOIN possum.observation_state_band{band_number} obs
        ON obs.name = ob.name
    ORDER BY ob.sbid;
    """
    return execute_query(sql, conn)


#### TEST METHODS ###


def get_3d_tile_data(tile_id, band_number, conn):
    """
    Utility method to get 3d tile information (useful for tests)
    Args:
    - tile_id: tile number
    - band_number: 1 or 2
    """
    if tile_id is None:
        sql = f"""SELECT tile, "3d_pipeline_val", "3d_val_link", "3d_pipeline_ingest", "3d_pipeline", "cube_state"
                FROM possum.tile_state_band{band_number}"""
        return execute_query(sql, conn)

    else:
        sql = f"""SELECT tile, "3d_pipeline_val", "3d_val_link", "3d_pipeline_ingest", "3d_pipeline", "cube_state"
                from possum.tile_state_band{band_number} WHERE tile = %s"""
        return execute_query(sql, conn, (tile_id,))


def get_1d_pipeline_validation_status(field_name, band_number, conn):
    """
    Handy method to get 1d_pipeline_validation status (useful for tests):
    """
    sql = f"""
        SELECT "1d_pipeline_validation" FROM possum.observation_state_band{band_number}
        WHERE name = %s;
        """
    return execute_query(sql, conn, (field_name,))


def get_single_sb_1d_pipeline_status(field_name, band_number, conn):
    """
    Handy method to get single_sb_1d_pipeline status (useful for tests):
    """
    sql = f"""
        SELECT single_sb_1d_pipeline FROM possum.observation_state_band{band_number}
        WHERE name = %s;
        """
    return execute_query(sql, conn, (field_name,))


def get_1d_pipeline_status(field_name, tilenumbers, band_number, conn):
    """
    Handy method to get 1d_pipeline status (useful for tests):
    """
    sql = f"""
        SELECT "1d_pipeline" FROM possum.partial_tile_1d_pipeline_band{band_number}
        WHERE observation = %s
        """
    # Check for NULLS in tile numbers and make sure the query says IS NULL and not = NULL so it works
    for i, tile in enumerate(tilenumbers, start=1):
        if tile is None or tile.strip() == "":  # If tile is None, use IS NULL
            sql += f" AND tile{i} IS NULL"
        else:  # Otherwise, use equality
            sql += f" AND tile{i} = '{tile}'"

    return execute_query(sql, conn, (field_name,))


def get_fields_ready_single_SB_pipeline(band_number, conn) -> Table:
    """
    Get fields that are ready for 1D Partial Tile pipeline processing:
    i.e. single_sb_1d_pipeline is NULL/empty and cube_state = 'COMPLETED'

    returns a table with columns ["name", "sbid"]
    """
    sql = f"""
    SELECT s.name, o.sbid FROM possum.observation_state_band{band_number} s, possum.observation o
    WHERE s.name = o.name
    AND
    (s."single_sb_1d_pipeline" IS NULL or s."single_sb_1d_pipeline" = '')
    AND UPPER(s."cube_state") = 'COMPLETED';
    """
    rows, colnames = execute_query(sql, conn, return_colnames=True)
    return rows_to_table(rows, colnames=colnames)


def get_full_table_single_SB_pipeline(band_number, conn, as_table=True) -> list | Table:
    """
    Get single_sb_1d_pipeline status as either raw rows or an astropy Table.
    """
    sql = f"""
        SELECT * FROM possum.observation_state_band{band_number}
    """
    rows, colnames = execute_query(sql, conn, return_colnames=True)

    if as_table:
        return rows_to_table(rows, colnames=colnames)
    return rows
