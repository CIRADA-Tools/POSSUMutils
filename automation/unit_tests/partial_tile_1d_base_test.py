"""
DEPRECATED since July 13, 2026: Direct database access has been replaced by REST API. The tests are now covered in possum_pipeline_control/test_rest_api.py

Base class for setting up Partial Tile 1D test cases. This is to reduce repetitions across test cases
because the setup is all the same.
"""

import csv
import unittest
from abc import ABC

from automation import database_queries as db_query
from automation import insert_database_script as db


class PartialTile1DBaseTest(unittest.TestCase, ABC):
    "Setup the tables for partial_tile 1D pipeline cases"

    def setUp(self):
        self.conn = db_query.get_database_connection(test=True)
        queries = []
        queries.append(db.drop_test_schema())
        queries.append(db.create_test_schema())
        queries.append(db.create_observation_test_table())
        queries.extend(db.create_observation_state_tables())
        queries.extend(db.create_partial_tile_pipeline_tables())
        # insert partial tile data
        # Columns: observation, sbid, tile1, tile2, tile3, tile4, type, number_sources, 1d_pipeline
        partial_tile_csv = (
            "automation/unit_tests/csv/partial_tile_1d_pipeline_band1.csv"
        )
        # Open and stream rows
        with open(partial_tile_csv, newline="", encoding="utf-8") as csvfile:
            reader = csv.reader(csvfile)
            # Skip header
            next(reader)
            for row in reader:
                # observation, sbid, tile1, tile2, tile3, tile4, type, number_sources, 1d_pipeline
                queries.append(db.insert_row_into_partial_tile_table(row, "1"))
                # execute in one transaction
        with self.conn:
            for query in queries:
                db_query.execute_query(query[0], self.conn, query[1], True)
        # insert observation data: name, sbid, 1d_pipeline_validation, single_sb_1d_pipeline
        insert_observation_state_csv_data(self.conn)

    def tearDown(self):
        if self.conn:
            queries = []
            queries.extend(db.drop_test_tables())
            queries.append(db.drop_test_schema())
            # using with statement to auto commit and rollback if there's exception
            with self.conn:
                for query in queries:
                    db_query.execute_query(query[0], self.conn, query[1], True)
            self.conn.close()
            self.conn = None


def insert_observation_state_csv_data(conn):
    """
    Insert observation state data from CSV file
    """
    observation_state_csv = "automation/unit_tests/csv/observation_state_band1.csv"
    # Open and stream rows
    with conn:
        with open(observation_state_csv, newline="", encoding="utf-8") as csvfile:
            reader = csv.reader(csvfile)
            # Skip header
            next(reader)
            for row in reader:
                # name, sbid into observation table
                db.insert_observation_row(row[0], row[1], conn)
                # name, band, 1d_pipeline_validation, single_sb_1d_pipeline, cube_state into observation_state_band1 table
                db.insert_observation_state_data(
                    row[0], "1", row[2], row[3], row[4], conn
                )
