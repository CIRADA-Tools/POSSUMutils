"""
DEPRECATED since July 13, 2026: Direct database access has been replaced by REST API. The tests are now covered in possum_pipeline_control/test_rest_api.py

Test possum_pipeline_control: check_ingest_3Dpipeline.py
"""

from automation import database_queries as db_query
from automation.unit_tests._3dpipeline_base_test import _3DPipelineBaseTest
from possum_pipeline_control import check_ingest_3Dpipeline


class CheckIngest3dPipeline(_3DPipelineBaseTest):
    """
    Setup and tearDown is done in the _3DPipelineBaseTest class.
    """

    def test_get_tiles_for_ingest(self):
        """
        Based on the original code:
        tiles_to_run = [row['tile_id'] for row in tile_table if ( (row['3d_pipeline_val'] == 'Good') and (row['3d_pipeline_ingest'] == '') )]
        """
        tiles_to_run = check_ingest_3Dpipeline.get_tiles_for_ingest("1", self.conn)
        assert len(tiles_to_run) == 3
        assert tiles_to_run[0] == 1234
        assert tiles_to_run[1] == 1235
        assert tiles_to_run[2] == 6755

    def test_update_status(self):
        """
        Test updating ingest status
        """
        num_rows = check_ingest_3Dpipeline.update_status(
            tile_number=1235, band="943MHz", status="Ingested", conn=self.conn
        )
        assert num_rows == 1  # succesful update
        # Check if the value was updated
        results = db_query.get_3d_tile_data(1235, "1", self.conn)
        assert len(results) == 1
        assert results[0][3] == "Ingested"

        num_rows = check_ingest_3Dpipeline.update_status(
            tile_number=000, band="943MHz", status="Ingested", conn=self.conn
        )
        assert num_rows == 0  # tile not found
