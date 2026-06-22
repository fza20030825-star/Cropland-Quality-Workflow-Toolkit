from pathlib import Path

from cropland_quality_update.tools import gb_to_sanpu_arcpy_ui as converter
from cropland_quality_update.tools import membership_arcpy_ui as membership_tool


def make_rule_set():
    return membership_tool.RuleSet(
        area_name="秦岭大巴山林农区",
        rule_path=Path("rules.xlsx"),
        weights={"耕层质地": 1.0, "质地构型": 1.0, "水资源条件": 1.0},
        concept_memberships={
            "耕层质地": {
                "壤土": 1.0,
                "粉（砂）质壤土": 0.9,
                "砂质壤土": 0.76,
                "黏壤土": 0.85,
                "砂土及壤质砂土": 0.47,
            },
            "质地构型": {
                "上松下紧型": 1.0,
                "海绵型": 0.9,
            },
            "水资源条件": {
                "充分满足": 1.0,
                "满足": 0.85,
                "基本满足": 0.7,
                "不满足": 0.4,
            },
        },
        numeric_rules={},
        grade_rules=[],
    )


def test_texture_aliases_normalize_to_rule_categories():
    rule_set = make_rule_set()
    assert converter.normalize_concept_value(rule_set, "耕层质地", "中壤") == "壤土"
    assert converter.normalize_concept_value(rule_set, "耕层质地", "轻壤") == "粉（砂）质壤土"
    assert converter.normalize_concept_value(rule_set, "耕层质地", "砂壤") == "砂质壤土"
    assert converter.normalize_concept_value(rule_set, "耕层质地", "粘壤土") == "黏壤土"
    assert converter.normalize_concept_value(rule_set, "耕层质地", "砂土") == "砂土及壤质砂土"


def test_type_and_water_aliases_normalize_to_rule_categories():
    rule_set = make_rule_set()
    assert converter.normalize_concept_value(rule_set, "质地构型", "上松下紧") == "上松下紧型"
    assert converter.normalize_concept_value(rule_set, "水资源条件", "充足") == "充分满足"
    assert converter.normalize_concept_value(rule_set, "水资源条件", "一般") == "基本满足"


def test_parse_numeric_value_accepts_units_and_commas():
    assert converter.parse_numeric_value("25cm") == 25
    assert converter.parse_numeric_value("1,234.5 mg/kg") == 1234.5
    assert converter.parse_numeric_value("") is None


def test_numeric_indicators_do_not_use_f_fields_as_raw_values():
    rule_set = membership_tool.RuleSet(
        area_name="秦岭大巴山林农区",
        rule_path=Path("rules.xlsx"),
        weights={"有机质": 1.0},
        concept_memberships={},
        numeric_rules={
            "有机质": membership_tool.NumericRule(
                indicator="有机质",
                function_code="UPPER_SATURATION",
                function_type="戒上型",
                a=0.002657,
                b=None,
                c=36.824713,
                lower=2,
                upper=36.8,
            )
        },
        grade_rules=[],
    )
    names = [converter.compact_key(name) for name in converter.candidate_field_names("有机质", rule_set)]
    assert "f有机质" not in names
