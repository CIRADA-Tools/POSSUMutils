"""
DEPRECATED since July 13, 2026: Direct database access has been replaced by REST API. The tests are now covered in possum_pipeline_control/test_rest_api.py

Test possum_pipeline_control: check_status_and_launch_1Dpipeline_PartialTiles.py
"""

from automation import database_queries as db_query
from automation.unit_tests.partial_tile_1d_base_test import PartialTile1DBaseTest
from possum_pipeline_control import (
    check_status_and_launch_1Dpipeline_PartialTiles as check_status,
)


class CheckStatusAndLaunch1DPipelinePartialTiles(PartialTile1DBaseTest):
    """
    Setup and tearDown is done in the PartialTile1DBaseTest class.
    """

    def test_get_tiles_for_pipeline_run(self):
        """
        Test get_tiles_for_pipeline_run
        """
        (
            field_IDs,
            tile1,
            tile2,
            tile3,
            tile4,
            SBids,
            fields_to_validate,
            field_to_validate_boundaryissues,
        ) = check_status.get_tiles_for_pipeline_run(self.conn, band_number=1)
        assert field_IDs == [
            "EMU_1748-64",
            "EMU_1759-69",
            "EMU_1759-69",
            "EMU_1759-69",
            "EMU_1759-69",
            "EMU_1136-64",
            "EMU_1136-64",
            "EMU_1136-62",
        ], f"found {field_IDs}"
        assert tile1 == [
            "11791",
            "11726",
            "11791",
            "11726",
            "11791",
            "11487",
            "11487",
            "11490",
        ], f"found {tile1}"
        assert tile2 == [
            "11792",
            "11727",
            "11852",
            "11727",
            "11792",
            "11565",
            "11488",
            "11491",
        ], f"found {tile2}"
        assert tile3 == ["11852", "", "", "11791", "11852", "", "", "11492"], (
            f"found {tile3}"
        )
        assert tile4 == ["11853", "", "", "11792", "11853", "", "", "11493"], (
            f"found {tile4}"
        )
        assert SBids == [
            "54926",
            "46955",
            "46955",
            "46955",
            "46955",
            "46948",
            "46948",
            "46946",
        ], f"found {SBids}"
        assert fields_to_validate == [
            ("EMU_1136-61", "46950"),
            ("EMU_0626-09B", "61077"),
        ], f"found {fields_to_validate}"
        assert field_to_validate_boundaryissues == {("EMU_1136-62", "46946")}, (
            f"found {field_to_validate_boundaryissues}"
        )

    def test_update_1d_pipeline_validation_status(self):
        """Testing - update the status of the '1d_pipeline_validation' column to "Running"
        regardless of tile number
        """
        field = "EMU_1748-64"
        band_number = 1
        status = "Running"
        row_num = db_query.update_1d_pipeline_table(
            field, band_number, status, "1d_pipeline_validation", self.conn
        )
        assert row_num == 1
        # Make sure the update works
        results = db_query.get_1d_pipeline_validation_status(
            field, band_number, self.conn
        )
        new_status = results[0][0]  # 1d_pipeline_validation
        assert new_status == status

    def test_update_partial_tile_1d_pipeline_status(self):
        """Testing - update the status of '1d_pipeline' column in partial tile table."""
        field = "EMU_1748-64"
        tilenumbers = ["11791", "11792", "11852", "11853"]
        band_number = "1"
        previous_status = db_query.get_1d_pipeline_status(
            field, tilenumbers, band_number, self.conn
        )
        db_query.update_partial_tile_1d_pipeline_status(
            field, tilenumbers, band_number, "Running", self.conn
        )
        new_status = db_query.get_1d_pipeline_status(
            field, tilenumbers, band_number, self.conn
        )
        assert previous_status[0][0] != new_status[0][0]
        assert new_status[0][0] == "Running"  # 1d_pipeline

        # Test with None in tiles
        field = "EMU_1136-60"
        tilenumbers = ["12345", None, None, None]
        band_number = "1"
        previous_status = db_query.get_1d_pipeline_status(
            field, tilenumbers, band_number, self.conn
        )
        db_query.update_partial_tile_1d_pipeline_status(
            field, tilenumbers, band_number, "Running", self.conn
        )
        new_status = db_query.get_1d_pipeline_status(
            field, tilenumbers, band_number, self.conn
        )
        assert previous_status[0][0] != new_status[0][0]
        assert new_status[0][0] == "Running"  # 1d_pipeline

        # Test with empty tile number
        field = "EMU_1136-60"
        tilenumbers = ["12345", "", "", ""]
        band_number = "1"
        previous_status = db_query.get_1d_pipeline_status(
            field, tilenumbers, band_number, self.conn
        )
        db_query.update_partial_tile_1d_pipeline_status(
            field, tilenumbers, band_number, "Completed", self.conn
        )
        new_status = db_query.get_1d_pipeline_status(
            field, tilenumbers, band_number, self.conn
        )
        assert previous_status[0][0] != new_status[0][0]
        assert new_status[0][0] == "Completed"  # 1d_pipeline
