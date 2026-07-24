"""
DEPRECATED since July 13, 2026: Direct database access has been replaced by REST API. The tests are now covered in possum_pipeline_control/test_rest_api.py

Test possum_pipeline_control: check_status_and_launch_3Dpipeline_v2.py
"""

import csv

from automation import database_queries as db_query
from automation import insert_database_script as db
from automation.unit_tests import partial_tile_1d_base_test
from automation.unit_tests._3dpipeline_base_test import _3DPipelineBaseTest


class CheckStatusAndLaunch3DPipelinev2Test(_3DPipelineBaseTest):
    """
    Test check_status_and_launch_3Dpipeline_v2.py
    """

    def setUp(self):
        super().setUp()
        self.conn = db_query.get_database_connection(test=True)
        # Create associated_tile table
        db.create_associated_tile_test_table(self.conn)

        # Columns: observation name, tile id, row id
        _data = "automation/unit_tests/csv/associated_tile.csv"
        # Insert associated_tile data
        with open(_data, newline="", encoding="utf-8") as csvfile:
            reader = csv.reader(csvfile)
            # Skip header
            next(reader)
            for row in reader:
                db.insert_associated_tile_data(row[0], row[1], self.conn)
        # create and populate observation_state_band1 table
        queries = []
        queries.append(db.create_observation_test_table())
        queries.extend(db.create_observation_state_tables())
        with self.conn:
            for query in queries:
                db_query.execute_query(query[0], self.conn, query[1], True)
        partial_tile_1d_base_test.insert_observation_state_csv_data(self.conn)

    def test_update_status(self):
        "Test update_status"
        tile_number = 1239
        row_num = db_query.update_3d_pipeline_table(
            tile_number, "1", "Running", "3d_pipeline_val", self.conn
        )
        assert row_num == 1

        tile_number = "1240"
        row_num = db_query.update_3d_pipeline_table(
            tile_number, "1", "Running", "3d_pipeline_val", self.conn
        )
        assert row_num == 1

        # check that the update works
        results = db_query.get_3d_tile_data("1239", "1", self.conn)
        assert results[0][1] == "Running"  # 3d_pipeline_val

        results = db_query.get_3d_tile_data(1240, "1", self.conn)
        assert results[0][1] == "Running"  # 3d_pipeline_val

    def test_get_tiles_for_pipeline_run(self):
        """
        Test get_tiles_for_pipeline_run
        """
        # to check the database at this point
        # results = db_query.get_3d_tile_data(None, '1', self.conn)
        # print("tile", "3d_pipeline_val", "3d_val_link", "3d_pipeline_ingest", "3d_pipeline", "cube_state")
        # for row in results:
        #     print(row)

        tile_numbers = db_query.get_tiles_for_pipeline_run(self.conn, band_number=1)
        # checks for cube_state completed, mfs_state completed, and nothing in "3d_pipeline_val"
        assert tile_numbers == [(1239,), (6753,)], f"Got {tile_numbers}"
