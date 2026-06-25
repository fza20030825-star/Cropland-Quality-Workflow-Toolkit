from cropland_quality_update.tools.update_scores_arcpy_ui import OPTIONAL_RESULT_FIELD_NAMES, UPDATE_FIELD_NAMES, required_update_field_names, values_equivalent_for_audit


def test_optional_altitude_blank_values_match():
    assert values_equivalent_for_audit(None, "", "海拔高度")
    assert values_equivalent_for_audit("", None, "F海拔高度")


def test_optional_altitude_still_rejects_partial_blank():
    assert not values_equivalent_for_audit(None, 120.0, "海拔高度")
    assert not values_equivalent_for_audit("", 0.85, "F海拔高度")


def test_non_optional_blank_values_do_not_match():
    assert not values_equivalent_for_audit(None, "", "有机质")


def test_required_update_fields_exclude_optional_altitude():
    required = required_update_field_names()

    assert "海拔高度" not in required
    assert "F海拔高度" not in required
    assert set(required) == set(UPDATE_FIELD_NAMES) - OPTIONAL_RESULT_FIELD_NAMES
