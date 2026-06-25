from types import SimpleNamespace

from cropland_quality_update.tools.recalculate_scores_arcpy_ui import required_raw_indicators


def test_required_raw_indicators_follow_rule_set():
    rule_set = SimpleNamespace(indicators=["地形部位", "有机质", "酸碱度"])

    assert required_raw_indicators(rule_set) == ["地形部位", "有机质", "酸碱度"]


def test_required_raw_indicators_omit_unused_altitude():
    rule_set = SimpleNamespace(indicators=["地形部位", "有机质"])

    assert "海拔高度" not in required_raw_indicators(rule_set)
