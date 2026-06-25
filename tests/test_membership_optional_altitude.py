from types import SimpleNamespace

from cropland_quality_update.tools.membership_arcpy_ui import FieldBinding, validation_bindings_for_area


def test_optional_unused_altitude_is_not_validated():
    bindings = {
        "地形部位": FieldBinding("地形部位", "地形部位", "String"),
        "海拔高度": FieldBinding("海拔高度", "海拔高度", "Double"),
    }
    rule_set = SimpleNamespace(indicators=["地形部位"])

    checked = validation_bindings_for_area(bindings, rule_set, {"海拔高度"})

    assert "地形部位" in checked
    assert "海拔高度" not in checked


def test_optional_altitude_is_validated_when_rule_uses_it():
    bindings = {
        "地形部位": FieldBinding("地形部位", "地形部位", "String"),
        "海拔高度": FieldBinding("海拔高度", "海拔高度", "Double"),
    }
    rule_set = SimpleNamespace(indicators=["地形部位", "海拔高度"])

    checked = validation_bindings_for_area(bindings, rule_set, {"海拔高度"})

    assert "地形部位" in checked
    assert "海拔高度" in checked
