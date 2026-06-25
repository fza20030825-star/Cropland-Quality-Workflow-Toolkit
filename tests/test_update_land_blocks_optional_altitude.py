from cropland_quality_update.tools.update_land_blocks_arcpy_ui import required_evaluation_field_names
from cropland_quality_update.tools.update_scores_arcpy_ui import OPTIONAL_RESULT_FIELD_NAMES, UPDATE_FIELD_NAMES


def test_third_step_required_evaluation_fields_exclude_optional_altitude():
    required = required_evaluation_field_names()

    assert "海拔高度" not in required
    assert "F海拔高度" not in required
    assert set(required) == set(UPDATE_FIELD_NAMES) - OPTIONAL_RESULT_FIELD_NAMES
