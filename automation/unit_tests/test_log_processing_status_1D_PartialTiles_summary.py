"""
DEPRECATED since July 13, 2026: Direct database access has been replaced by REST API. The tests are now covered in possum_pipeline_control/test_rest_api.py

Test cirada_software: log_processing_status_1D_PartialTiles_summary.py
"""

from automation import database_queries as db_query
from automation.unit_tests.partial_tile_1d_base_test import PartialTile1DBaseTest
from cirada_software import log_processing_status_1D_PartialTiles_summary
from possum_pipeline_control import util


class LogProcessingStatus1DPartialTilesSummaryTest(PartialTile1DBaseTest):
    """
    Setup and tearDown is done in the PartialTile1DBaseTest class.
    """

    def test_update_1d_database(self):
        """
        Test update_1d_database
        """
        field_ID = "1136-62"
        SBid = "46946"
        band = "943MHz"
        status = "Failed"
        # has boundary issues
        boundary_issues = (
            log_processing_status_1D_PartialTiles_summary.update_1d_database(
                field_ID, SBid, band, status, self.conn
            )
        )
        assert boundary_issues is True
        # check that the status was updated
        full_field_name = util.get_full_field_name(field_ID, band)
        _1d_pipeline_validation = db_query.get_1d_pipeline_validation_status(
            field_name=full_field_name, band_number="1", conn=self.conn
        )[0][0]
        assert _1d_pipeline_validation == "Failed"  # 1d_pipeline_validation

        # no boundary issues
        field_ID = "1136-61"
        SBid = "46950"
        status = "Completed"
        boundary_issues = (
            log_processing_status_1D_PartialTiles_summary.update_1d_database(
                field_ID, SBid, band, status, self.conn
            )
        )
        assert boundary_issues is False
        full_field_name = util.get_full_field_name(field_ID, band)
        _1d_pipeline_validation = db_query.get_1d_pipeline_validation_status(
            field_name=full_field_name, band_number="1", conn=self.conn
        )[0][0]
        assert _1d_pipeline_validation == "Completed"  # 1d_pipeline_validation

        # row doesn't exist
        field_ID = "1136-1000"
        boundary_issues = (
            log_processing_status_1D_PartialTiles_summary.update_1d_database(
                field_ID, SBid, band, status, self.conn
            )
        )
        assert boundary_issues is False

    def test_update_status_spreadsheet(self):
        """
        Test update_status_spreadsheet
        """
        field_ID = "1136-62"
        band = "943MHz"
        status = "Failed"
        full_field_name = util.get_full_field_name(field_ID, band)
        band_number = util.get_band_number(band)
        row_num = db_query.update_1d_pipeline_table(
            full_field_name, band_number, status, "single_sb_1d_pipeline", self.conn
        )
        # Verify successful
        assert row_num == 1
        status = db_query.get_single_sb_1d_pipeline_status(
            full_field_name, "1", self.conn
        )[0][0]
        assert status == "Failed"  # single_sb_1d_pipeline
