"""
DEPRECATED since July 13, 2026: Direct database access has been replaced by REST API. The tests are now covered in possum_pipeline_control/test_rest_api.py

Base class for setting up 3D Pipeline test cases. This is to reduce repetitions across test cases
because the setup is all the same.
"""

import csv
import unittest
from abc import ABC
from datetime import datetime

from automation import database_queries as db_query
from automation import insert_database_script as db


class _3DPipelineBaseTest(unittest.TestCase, ABC):
    def setUp(self):
        self.conn = db_query.get_database_connection(True)
        queries = []
        queries.append(db.drop_test_schema())
        queries.append(db.create_test_schema())
        queries.extend(db.create_tile_state_tables())
        with self.conn:
            for query in queries:
                db_query.execute_query(query[0], self.conn, query[1], True)

        # Columns: tile_id, 3d_pipeline, 3d_pipeline_val, 3d_pipeline_ingest, cube_state
        _3d_data = "automation/unit_tests/csv/tile_state_band1.csv"
        # Open and stream rows
        with open(_3d_data, newline="", encoding="utf-8") as csvfile:
            reader = csv.reader(csvfile)
            # Skip header
            next(reader)
            with self.conn:
                for row in reader:
                    # tile_id, 3d_pipeline, 3d_pipeline_val, 3d_pipeline_ingest
                    timestamp = None
                    if isinstance(row[1], datetime):
                        # the spreadsheet has mixed values (state and timestamp)
                        # we're going to separate them
                        timestamp = row[1]
                    _3d_val = row[2]
                    db.insert_3d_pipeline_test_data(
                        row[0], timestamp, _3d_val, row[3], row[4], row[5], self.conn
                    )

    def tearDown(self):
        sql = []
        sql.extend(db.drop_test_tables())
        sql.append(db.drop_test_schema())
        # using with statement to auto commit and rollback if there's exception
        with self.conn:
            for query in sql:
                db_query.execute_query(query[0], self.conn, query[1], True)

        self.conn.close()
