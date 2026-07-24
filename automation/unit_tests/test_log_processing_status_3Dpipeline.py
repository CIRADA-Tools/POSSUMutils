"""
DEPRECATED since July 13, 2026: Direct database access has been replaced by REST API. The tests are now covered in possum_pipeline_control/test_rest_api.py

Test cirada_software: log_processing_status_3Dpipeline.py
"""

from automation import database_queries as db_query
from automation.unit_tests._3dpipeline_base_test import _3DPipelineBaseTest


class LogProcessingStatus3DPipelineTest(_3DPipelineBaseTest):
    """
    Setup and tearDown is done in the 3DPipelineBaseTest class.
    """

    def test_update_status_spreadsheet(self):
        """
        Test update_status_spreadsheet
        """
        tile_number = 1240
        band_number = "1"
        status = "WaitingForValidation"
        row_num = db_query.update_3d_pipeline_table(
            tile_number, band_number, status, "3d_pipeline_val", self.conn
        )
        assert row_num == 1
        result = db_query.get_3d_tile_data(tile_number, band_number, self.conn)
        assert result[0][1] == "WaitingForValidation"  # 3d_pipeline

    def test_update_tile_database(self):
        """
        Test update_tile_database
        """
        tile_number = 1240
        band_number = "1"
        status = "WaitingForValidation"
        # Test with legit link
        validation_link = "https://ws-uv.canfar.net/arc/files/projects/CIRADA/polarimetry/pipeline_runs/943MHz/tile1240/PSM_943MHz_20asec_1353+0823_5258_p3d_v1_validation.html"
        rows_updated = db_query.update_3d_pipeline_table(
            tile_number, band_number, status, "3d_pipeline_val", self.conn
        )
        db_query.update_3d_pipeline_table(
            tile_number, band_number, validation_link, "3d_val_link", self.conn
        )
        assert rows_updated == 1
        result = db_query.get_3d_tile_data(tile_number, band_number, self.conn)
        assert result[0][1] == "WaitingForValidation"  # 1d_pipeline_validation
        assert result[0][2] == validation_link  # 1d_val_link

        # Test with non-link link
        validation_link = "MultipleHTMLFiles"
        rows_updated = db_query.update_3d_pipeline_table(
            tile_number, band_number, status, "3d_pipeline_val", self.conn
        )
        db_query.update_3d_pipeline_table(
            tile_number, band_number, validation_link, "3d_val_link", self.conn
        )
        assert rows_updated == 1
        result = db_query.get_3d_tile_data(tile_number, band_number, self.conn)
        assert result[0][2] == validation_link  # 1d_val_link
