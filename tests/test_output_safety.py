from pathlib import Path

from cropland_quality_update.tools.merge_common_arcpy_ui import output_dataset_path as merge_output_dataset_path
from cropland_quality_update.tools.membership_arcpy_ui import output_dataset_path as membership_output_dataset_path
from cropland_quality_update.tools.update_scores_arcpy_ui import output_dataset_path as update_output_dataset_path
from cropland_quality_update.tools.update_land_blocks_arcpy_ui import output_dataset_path as land_block_output_dataset_path
from cropland_quality_update.tools.area_balance_arcpy_ui import output_dataset_path as area_balance_output_dataset_path


def test_output_dataset_path_shp():
    path = Path(r"D:\work\input.shp")

    assert merge_output_dataset_path("shp", path, None) == path
    assert membership_output_dataset_path("shp", path, None) == path
    assert update_output_dataset_path("shp", path, None) == path
    assert land_block_output_dataset_path("shp", path, None) == path
    assert area_balance_output_dataset_path("shp", path, None) == path


def test_output_dataset_path_gdb_feature_class():
    gdb = Path(r"D:\work\county.gdb")

    assert merge_output_dataset_path("gdb", gdb, "new_layer") == gdb / "new_layer"
    assert membership_output_dataset_path("gdb", gdb, "new_layer") == gdb / "new_layer"
    assert update_output_dataset_path("gdb", gdb, "new_layer") == gdb / "new_layer"
    assert land_block_output_dataset_path("gdb", gdb, "new_layer") == gdb / "new_layer"
    assert area_balance_output_dataset_path("gdb", gdb, "new_layer") == gdb / "new_layer"
