"""
DEPRECATED since July 13, 2026: Direct database access has been replaced by REST API. The tests are now covered in possum_pipeline_control/test_rest_api.py

Test cirada_software: log_processing_status_1D_PartialTiles.py
"""

from automation import database_queries as db_query
from automation.unit_tests.partial_tile_1d_base_test import PartialTile1DBaseTest
from cirada_software import log_processing_status_1D_PartialTiles


class LogProcessingStatus1DPartialTilesTest(PartialTile1DBaseTest):
    """
    Setup and tearDown is done in the PartialTile1DBaseTest class.
    """

    def test_update_partial_tile_1d_pipeline(self):
        """Test update_partial_tile_1d_pipeline"""
        field_ID = "1748-64"
        tile_numbers = ["11791", "11792", "11852", "11853"]
        band = "943MHz"
        status = "Completed"
        log_processing_status_1D_PartialTiles.update_partial_tile_1d_pipeline(
            field_ID, tile_numbers, band, status, self.conn
        )
        new_status = db_query.get_1d_pipeline_status(
            "EMU_1748-64", tile_numbers, 1, self.conn
        )
        assert new_status[0][0] == "Completed"  # 1d_pipeline
