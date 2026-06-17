"""ArcPy UI tool for calculating cropland quality memberships and grades."""

from __future__ import annotations

import json
import logging
import math
import queue
import shutil
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, IntVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk

try:
    import arcpy
except ImportError as exc:  # pragma: no cover - shown to user at runtime
    arcpy = None
    ARCPY_IMPORT_ERROR = exc
else:
    ARCPY_IMPORT_ERROR = None

try:
    from openpyxl import load_workbook
except ImportError as exc:  # pragma: no cover - shown to user at runtime
    load_workbook = None
    OPENPYXL_IMPORT_ERROR = exc
else:
    OPENPYXL_IMPORT_ERROR = None

from cropland_quality_update.paths import resolve_paths


DEFAULT_AREA_NAME = "秦岭大巴山林农区"
ISSUE_DETAIL_LIMIT = 50
SAMPLE_FIELD_VALUE_LIMIT = 10
GEOMETRY_FIELD_NAMES = {"shape", "shape_length", "shape_area", "shape_leng"}
SKIP_FIELD_TYPES = {"OID", "Geometry", "Blob", "Raster", "GUID", "GlobalID", "XML"}
TEXT_FIELD_TYPES = {"String"}
NUMERIC_FIELD_TYPES = {"SmallInteger", "Integer", "Single", "Double"}
RESULT_SCORE_FIELD = "评价得分"
RESULT_GRADE_FIELD = "质量等级"


@dataclass(frozen=True)
class VectorSource:
    kind: str  # shp | gdb
    source_path: Path
    layer_name: str | None
    display_name: str


@dataclass(frozen=True)
class NumericRule:
    indicator: str
    function_code: str
    function_type: str
    a: float
    b: float | None
    c: float | None
    lower: float
    upper: float


@dataclass(frozen=True)
class GradeRule:
    grade_value: int
    grade_name: str
    lower: float | None
    upper: float | None


@dataclass(frozen=True)
class RuleSet:
    area_name: str
    rule_path: Path
    weights: dict[str, float]
    concept_memberships: dict[str, dict[str, float]]
    numeric_rules: dict[str, NumericRule]
    grade_rules: list[GradeRule]

    @property
    def indicators(self) -> list[str]:
        return list(self.weights.keys())

    @property
    def concept_indicators(self) -> set[str]:
        return set(self.concept_memberships.keys())

    @property
    def numeric_indicators(self) -> set[str]:
        return set(self.numeric_rules.keys())


@dataclass(frozen=True)
class FieldBinding:
    indicator: str
    source_field: str
    field_type: str


@dataclass(frozen=True)
class FeatureSample:
    oid: int
    field_values: list[tuple[str, str]]


@dataclass(frozen=True)
class MissingValueIssue:
    indicator: str
    field_name: str
    count: int
    samples: list[FeatureSample]


@dataclass(frozen=True)
class InvalidCategoryIssue:
    indicator: str
    field_name: str
    invalid_value: str
    count: int
    samples: list[FeatureSample]


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    source: VectorSource
    rule_set: RuleSet
    feature_count: int
    field_bindings: dict[str, FieldBinding]
    missing_fields: list[str]
    type_errors: list[str]
    missing_value_issues: list[MissingValueIssue]
    invalid_category_issues: list[InvalidCategoryIssue]
    text: str


@dataclass(frozen=True)
class CalculationJob:
    job_id: str
    source: VectorSource
    output_path: Path
    output_feature_name: str | None
    output_kind: str
    rule_set: RuleSet
    field_bindings: dict[str, FieldBinding]
    created_at: str
    validation_report: str


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_for_file() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def require_runtime() -> None:
    missing = []
    if arcpy is None:
        missing.append(f"arcpy ({ARCPY_IMPORT_ERROR})")
    if load_workbook is None:
        missing.append(f"openpyxl ({OPENPYXL_IMPORT_ERROR})")
    if missing:
        raise RuntimeError("缺少运行包：" + "；".join(missing))


def make_vector_source(kind: str, source_path: Path, layer_name: str | None = None) -> VectorSource:
    source_path = source_path.resolve()
    if kind == "shp":
        shp_path = source_path if source_path.suffix.lower() == ".shp" else source_path.with_suffix(".shp")
        return VectorSource("shp", shp_path, None, str(shp_path))
    if kind == "gdb":
        if not layer_name:
            raise ValueError("gdb source requires layer_name")
        return VectorSource("gdb", source_path, layer_name, f"{source_path}\\{layer_name}")
    raise ValueError(f"unsupported source kind: {kind}")


def source_label(source: VectorSource) -> str:
    return source.display_name


def source_dataset_path(source: VectorSource) -> str:
    if source.kind == "gdb":
        return str(source.source_path / str(source.layer_name))
    return str(source.source_path)


def is_gdb_path(path: Path) -> bool:
    return path.suffix.lower() == ".gdb" and path.is_dir()


def find_nearest_gdb_path(path: Path) -> Path | None:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if candidate.suffix.lower() == ".gdb" and candidate.is_dir():
            return candidate
    return None


def list_gdb_polygon_layers(gdb_path: Path) -> list[VectorSource]:
    require_runtime()
    previous_workspace = arcpy.env.workspace
    arcpy.env.workspace = str(gdb_path)
    try:
        sources: list[VectorSource] = []
        for feature_class in arcpy.ListFeatureClasses(feature_type="Polygon") or []:
            sources.append(make_vector_source("gdb", gdb_path, feature_class))
        for dataset in arcpy.ListDatasets(feature_type="Feature") or []:
            for feature_class in arcpy.ListFeatureClasses(feature_dataset=dataset, feature_type="Polygon") or []:
                sources.append(make_vector_source("gdb", gdb_path, f"{dataset}\\{feature_class}"))
        return sources
    finally:
        arcpy.env.workspace = previous_workspace


def output_format(path: Path) -> str:
    return "shp" if path.suffix.lower() == ".shp" else "gdb"


def output_dataset_path(output_kind: str, output_path: Path, output_feature_name: str | None) -> Path:
    return output_path if output_kind == "shp" else output_path / str(output_feature_name)


def sidecar_paths(shp_path: Path) -> list[Path]:
    base = shp_path.with_suffix("")
    return [base.with_suffix(ext) for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".shp.xml", ".fields.json")]


def delete_output_dataset(output_path: Path, output_feature_name: str | None = None) -> None:
    require_runtime()
    if output_format(output_path) == "shp":
        for path in sidecar_paths(output_path):
            if path.exists():
                path.unlink()
        return
    if output_feature_name:
        feature_class = str(output_path / output_feature_name)
        if arcpy.Exists(feature_class):
            arcpy.management.Delete(feature_class)
        return
    if output_path.exists():
        shutil.rmtree(output_path)


def is_data_field(field) -> bool:
    if field.type in SKIP_FIELD_TYPES:
        return False
    if field.name.lower() in GEOMETRY_FIELD_NAMES:
        return False
    return True


def field_value_text(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > 80:
        return text[:77] + "..."
    return text


def is_blank_value(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def normalize_category(value) -> str:
    return str(value).strip()


def oid_field_name(dataset_path: str) -> str:
    return arcpy.Describe(dataset_path).OIDFieldName


def first_data_field_names(dataset_path: str, limit: int = SAMPLE_FIELD_VALUE_LIMIT) -> list[str]:
    names: list[str] = []
    for field in arcpy.ListFields(dataset_path):
        if is_data_field(field):
            names.append(field.name)
        if len(names) >= limit:
            break
    return names


def fetch_field_values_by_oid(dataset_path: str, oid_value: int, field_names: list[str]) -> list[tuple[str, str]]:
    if oid_value is None or oid_value < 0 or not field_names:
        return []
    oid_name = oid_field_name(dataset_path)
    where = f"{arcpy.AddFieldDelimiters(dataset_path, oid_name)} = {int(oid_value)}"
    with arcpy.da.SearchCursor(dataset_path, field_names, where_clause=where) as cursor:
        for row in cursor:
            return [(name, field_value_text(value)) for name, value in zip(field_names, row)]
    return []


def make_issue_samples(dataset_path: str, oids: list[int]) -> list[FeatureSample]:
    sample_fields = first_data_field_names(dataset_path)
    return [FeatureSample(oid, fetch_field_values_by_oid(dataset_path, oid, sample_fields)) for oid in oids]


def read_shp_field_mapping(source: VectorSource) -> dict[str, str]:
    if source.kind != "shp":
        return {}
    mapping_path = source.source_path.with_suffix(".fields.json")
    if not mapping_path.exists():
        return {}
    try:
        raw = json.loads(mapping_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(original): str(actual) for original, actual in raw.items()}


def find_rule_file(rules_dir: Path, area_name: str) -> Path:
    candidates = [
        path
        for path in sorted(rules_dir.glob("*.xlsx"))
        if not path.name.startswith("~$") and path.stem.split("_", 1)[0] == area_name
    ]
    if not candidates:
        raise RuntimeError(f"没有找到国标二级区对应规则文件：{area_name}_*.xlsx")
    if len(candidates) > 1:
        names = "；".join(path.name for path in candidates)
        raise RuntimeError(f"找到多个规则文件，请保留一个：{names}")
    return candidates[0]


def list_rule_area_options(rules_dir: Path) -> list[str]:
    area_names: list[str] = []
    seen: set[str] = set()
    for path in sorted(rules_dir.glob("*.xlsx")):
        if path.name.startswith("~$") or "_" not in path.stem:
            continue
        area_name = path.stem.split("_", 1)[0].strip()
        if not area_name or area_name in seen:
            continue
        seen.add(area_name)
        area_names.append(area_name)
    return area_names


def sheet_rows(ws) -> list[dict[str, object]]:
    headers = [str(cell).strip() if cell is not None else "" for cell in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    rows: list[dict[str, object]] = []
    for raw in ws.iter_rows(min_row=2, values_only=True):
        if not any(value is not None and str(value).strip() != "" for value in raw):
            continue
        rows.append({header: raw[index] if index < len(raw) else None for index, header in enumerate(headers) if header})
    return rows


def as_float(value, field_name: str) -> float:
    if value is None or str(value).strip() == "":
        raise RuntimeError(f"规则表字段为空：{field_name}")
    return float(value)


def as_optional_float(value) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def load_rule_set(rules_dir: Path, area_name: str) -> RuleSet:
    require_runtime()
    rule_path = find_rule_file(rules_dir, area_name)
    wb = load_workbook(rule_path, data_only=True, read_only=True)
    for sheet_name in ("指标权重", "概念指标隶属度", "数值指标隶属函数", "等级划分指数"):
        if sheet_name not in wb.sheetnames:
            raise RuntimeError(f"规则文件缺少 sheet：{sheet_name}")

    weights: dict[str, float] = {}
    for row in sheet_rows(wb["指标权重"]):
        indicator = str(row.get("指标名称", "")).strip()
        if indicator:
            weights[indicator] = as_float(row.get("指标权重"), f"{indicator}.指标权重")
    if not weights:
        raise RuntimeError("规则表“指标权重”中没有读取到任何指标。")

    concept_memberships: dict[str, dict[str, float]] = {}
    for row in sheet_rows(wb["概念指标隶属度"]):
        indicator = str(row.get("指标名称", "")).strip()
        category = normalize_category(row.get("类别值", ""))
        if indicator and category:
            concept_memberships.setdefault(indicator, {})[category] = as_float(row.get("隶属度"), f"{indicator}.{category}.隶属度")

    numeric_rules: dict[str, NumericRule] = {}
    for row in sheet_rows(wb["数值指标隶属函数"]):
        indicator = str(row.get("指标名称", "")).strip()
        if not indicator:
            continue
        numeric_rules[indicator] = NumericRule(
            indicator=indicator,
            function_code=str(row.get("函数代码", "")).strip(),
            function_type=str(row.get("函数类型", "")).strip(),
            a=as_float(row.get("a"), f"{indicator}.a"),
            b=as_optional_float(row.get("b")),
            c=as_optional_float(row.get("c")),
            lower=as_float(row.get("下限"), f"{indicator}.下限"),
            upper=as_float(row.get("上限"), f"{indicator}.上限"),
        )

    grade_rules: list[GradeRule] = []
    for row in sheet_rows(wb["等级划分指数"]):
        grade_value = row.get("等级值")
        if grade_value is None:
            continue
        grade_rules.append(
            GradeRule(
                grade_value=int(grade_value),
                grade_name=str(row.get("等级名称", "")).strip(),
                lower=as_optional_float(row.get("下限")),
                upper=as_optional_float(row.get("上限")),
            )
        )
    grade_rules.sort(key=lambda item: item.grade_value)

    missing_rules = set(weights) - set(concept_memberships) - set(numeric_rules)
    extra_rules = (set(concept_memberships) | set(numeric_rules)) - set(weights)
    if missing_rules:
        raise RuntimeError("规则表中这些指标没有隶属度规则：" + "、".join(sorted(missing_rules)))
    if extra_rules:
        raise RuntimeError("规则表中这些隶属度规则没有对应权重：" + "、".join(sorted(extra_rules)))
    return RuleSet(area_name, rule_path, weights, concept_memberships, numeric_rules, grade_rules)


def bind_indicator_fields(source: VectorSource, rule_set: RuleSet) -> tuple[dict[str, FieldBinding], list[str]]:
    dataset = source_dataset_path(source)
    fields = {field.name: field for field in arcpy.ListFields(dataset) if is_data_field(field)}
    shp_mapping = read_shp_field_mapping(source)
    bindings: dict[str, FieldBinding] = {}
    missing: list[str] = []
    for indicator in rule_set.indicators:
        source_field = indicator if indicator in fields else shp_mapping.get(indicator, "")
        if source_field and source_field in fields:
            bindings[indicator] = FieldBinding(indicator, source_field, fields[source_field].type)
        else:
            missing.append(indicator)
    return bindings, missing


def collect_validation_issues(
    source: VectorSource,
    rule_set: RuleSet,
    bindings: dict[str, FieldBinding],
) -> tuple[list[str], list[MissingValueIssue], list[InvalidCategoryIssue]]:
    dataset = source_dataset_path(source)
    oid_name = oid_field_name(dataset)
    type_errors: list[str] = []
    missing_oids: dict[str, list[int]] = {indicator: [] for indicator in bindings}
    invalid_categories: dict[tuple[str, str], list[int]] = {}

    for indicator in rule_set.concept_indicators:
        binding = bindings.get(indicator)
        if binding and binding.field_type not in TEXT_FIELD_TYPES:
            type_errors.append(f"类别指标字段必须为文本型：{indicator} -> {binding.source_field}（实际 {binding.field_type}）")
    for indicator in rule_set.numeric_indicators:
        binding = bindings.get(indicator)
        if binding and binding.field_type not in NUMERIC_FIELD_TYPES:
            type_errors.append(f"数值指标字段必须为数值型：{indicator} -> {binding.source_field}（实际 {binding.field_type}）")

    cursor_fields = [oid_name] + [binding.source_field for binding in bindings.values()]
    binding_by_cursor_index = list(bindings.values())
    allowed_categories = {
        indicator: set(values.keys()) for indicator, values in rule_set.concept_memberships.items()
    }
    with arcpy.da.SearchCursor(dataset, cursor_fields) as cursor:
        for row in cursor:
            oid = int(row[0])
            for value, binding in zip(row[1:], binding_by_cursor_index):
                if is_blank_value(value):
                    missing_oids[binding.indicator].append(oid)
                    continue
                if binding.indicator in allowed_categories:
                    text = normalize_category(value)
                    if text not in allowed_categories[binding.indicator]:
                        invalid_categories.setdefault((binding.indicator, text), []).append(oid)

    missing_total = sum(len(oids) for oids in missing_oids.values())
    invalid_total = sum(len(oids) for oids in invalid_categories.values())
    show_missing_details = missing_total < ISSUE_DETAIL_LIMIT
    show_invalid_details = invalid_total < ISSUE_DETAIL_LIMIT

    missing_issues: list[MissingValueIssue] = []
    for indicator, oids in missing_oids.items():
        if not oids:
            continue
        binding = bindings[indicator]
        samples = make_issue_samples(dataset, oids) if show_missing_details else []
        missing_issues.append(MissingValueIssue(indicator, binding.source_field, len(oids), samples))

    invalid_issues: list[InvalidCategoryIssue] = []
    for (indicator, invalid_value), oids in sorted(invalid_categories.items()):
        binding = bindings[indicator]
        samples = make_issue_samples(dataset, oids) if show_invalid_details else []
        invalid_issues.append(InvalidCategoryIssue(indicator, binding.source_field, invalid_value, len(oids), samples))

    return type_errors, missing_issues, invalid_issues


def format_samples(samples: list[FeatureSample]) -> list[str]:
    lines: list[str] = []
    for index, sample in enumerate(samples, start=1):
        values = "；".join(f"{name}={value}" for name, value in sample.field_values) or "无可展示字段值"
        lines.append(f"       {index}. OID={sample.oid}；{values}")
    return lines


def build_validation_text(report: ValidationReport) -> str:
    lines: list[str] = []
    lines.append("隶属度计算前检查报告")
    lines.append("=" * 60)
    lines.append(f"输入数据：{source_label(report.source)}")
    lines.append(f"国标二级区：{report.rule_set.area_name}")
    lines.append(f"规则文件：{report.rule_set.rule_path}")
    lines.append(f"面要素数：{report.feature_count}")
    lines.append(f"规则指标数：{len(report.rule_set.weights)}")
    lines.append("")
    lines.append("一、字段匹配")
    if report.missing_fields:
        lines.append(f"缺少以下规则要求的 {len(report.rule_set.weights)} 个指标字段，已打回：")
        for name in report.missing_fields:
            lines.append(f"   - {name}")
    else:
        lines.append(f"规则要求的 {len(report.rule_set.weights)} 个指标字段均已找到。")
        for indicator in report.rule_set.indicators:
            binding = report.field_bindings[indicator]
            lines.append(f"   - {indicator} -> {binding.source_field}（{binding.field_type}）")
    lines.append("")
    lines.append("二、字段类型检查")
    if report.type_errors:
        lines.append("发现字段类型不符合规则，已打回：")
        lines.extend(f"   - {item}" for item in report.type_errors)
    else:
        lines.append("字段类型检查通过。")
    lines.append("")
    lines.append("三、空值检查")
    missing_total = sum(issue.count for issue in report.missing_value_issues)
    if missing_total:
        lines.append(f"发现空值 {missing_total} 个，已打回。")
        if missing_total >= ISSUE_DETAIL_LIMIT:
            lines.append(f"空值过多（总体空值 {missing_total} 个），不展开字段值。")
        for issue in report.missing_value_issues:
            lines.append(f"   - 指标：{issue.indicator}；字段：{issue.field_name}；空值要素数：{issue.count}")
            lines.extend(format_samples(issue.samples))
    else:
        lines.append("未发现规则指标字段空值。")
    lines.append("")
    lines.append("四、类别值检查")
    invalid_total = sum(issue.count for issue in report.invalid_category_issues)
    if invalid_total:
        lines.append(f"发现类别值不在规则表中 {invalid_total} 个，已打回。")
        if invalid_total >= ISSUE_DETAIL_LIMIT:
            lines.append(f"类别值错误过多（总体错误 {invalid_total} 个），不展开字段值。")
        for issue in report.invalid_category_issues:
            lines.append(
                f"   - 指标：{issue.indicator}；字段：{issue.field_name}；非法类别值：{issue.invalid_value}；要素数：{issue.count}"
            )
            allowed = "、".join(sorted(report.rule_set.concept_memberships[issue.indicator]))
            lines.append(f"     允许值：{allowed}")
            lines.extend(format_samples(issue.samples))
    else:
        lines.append("类别字段取值均能在规则表中匹配。")
    lines.append("")
    if report.ok:
        lines.append("检查通过，可以继续计算隶属度、评价得分和质量等级。")
    else:
        lines.append("检查未通过，请修正数据后重新检查。")
    return "\n".join(lines)


def validate_source(source: VectorSource, rule_set: RuleSet) -> ValidationReport:
    require_runtime()
    dataset = source_dataset_path(source)
    if not arcpy.Exists(dataset):
        raise RuntimeError(f"输入数据不存在：{dataset}")
    desc = arcpy.Describe(dataset)
    if getattr(desc, "shapeType", "") != "Polygon":
        raise RuntimeError("输入数据必须是面矢量。")
    feature_count = int(arcpy.management.GetCount(dataset)[0])
    bindings, missing_fields = bind_indicator_fields(source, rule_set)
    type_errors: list[str] = []
    missing_issues: list[MissingValueIssue] = []
    invalid_issues: list[InvalidCategoryIssue] = []
    if not missing_fields:
        type_errors, missing_issues, invalid_issues = collect_validation_issues(source, rule_set, bindings)
    ok = not missing_fields and not type_errors and not missing_issues and not invalid_issues
    draft = ValidationReport(
        ok=ok,
        source=source,
        rule_set=rule_set,
        feature_count=feature_count,
        field_bindings=bindings,
        missing_fields=missing_fields,
        type_errors=type_errors,
        missing_value_issues=missing_issues,
        invalid_category_issues=invalid_issues,
        text="",
    )
    return ValidationReport(
        ok=draft.ok,
        source=draft.source,
        rule_set=draft.rule_set,
        feature_count=draft.feature_count,
        field_bindings=draft.field_bindings,
        missing_fields=draft.missing_fields,
        type_errors=draft.type_errors,
        missing_value_issues=draft.missing_value_issues,
        invalid_category_issues=draft.invalid_category_issues,
        text=build_validation_text(draft),
    )


def membership_for_numeric(rule: NumericRule, value: float) -> float:
    u = float(value)
    code = rule.function_code.upper()
    if code == "NEG_LINEAR_CLAMP":
        if u <= rule.lower:
            return 1.0
        if u >= rule.upper:
            return 0.0
        if rule.b is None:
            raise RuntimeError(f"{rule.indicator} 负直线型缺少 b 值。")
        return max(0.0, min(1.0, rule.b - rule.a * u))
    if code == "UPPER_SATURATION":
        if u <= rule.lower:
            return 0.0
        if u >= rule.upper:
            return 1.0
        if rule.c is None:
            raise RuntimeError(f"{rule.indicator} 戒上型缺少 c 值。")
        return max(0.0, min(1.0, 1.0 / (1.0 + rule.a * (u - rule.c) ** 2)))
    if code == "PEAK":
        if u <= rule.lower or u >= rule.upper:
            return 0.0
        if rule.c is None:
            raise RuntimeError(f"{rule.indicator} 峰型缺少 c 值。")
        return max(0.0, min(1.0, 1.0 / (1.0 + rule.a * (u - rule.c) ** 2)))
    raise RuntimeError(f"不支持的函数代码：{rule.function_code}")


def grade_for_score(score: float, grade_rules: list[GradeRule]) -> int:
    for rule in grade_rules:
        lower_ok = True if rule.lower is None else score >= rule.lower
        upper_ok = True if rule.upper is None else score < rule.upper
        if lower_ok and upper_ok:
            return rule.grade_value
    return grade_rules[-1].grade_value


def output_workspace(output_path: Path, output_feature_name: str | None) -> str:
    if output_format(output_path) == "shp":
        return str(output_path.parent)
    return str(output_path)


def validated_field_name(name: str, workspace: str, existing_upper: set[str]) -> str:
    candidate = arcpy.ValidateFieldName(name, workspace)
    base = candidate
    suffix = 1
    while candidate.upper() in existing_upper:
        suffix_text = str(suffix)
        candidate = arcpy.ValidateFieldName(f"{base[: max(1, 64 - len(suffix_text))]}{suffix_text}", workspace)
        suffix += 1
    existing_upper.add(candidate.upper())
    return candidate


def ensure_result_fields(feature_class: str, output_kind: str, rule_set: RuleSet) -> dict[str, str]:
    workspace = str(Path(feature_class).parent) if output_kind == "shp" else str(Path(feature_class).parent)
    existing = {field.name.upper(): field.name for field in arcpy.ListFields(feature_class)}
    existing_upper = set(existing)
    result_map: dict[str, str] = {}
    for indicator in rule_set.indicators:
        target = f"F{indicator}"
        actual = existing.get(target.upper())
        if actual is None:
            actual = validated_field_name(target, workspace, existing_upper)
            arcpy.management.AddField(feature_class, actual, "DOUBLE", field_alias=target)
            existing[actual.upper()] = actual
        result_map[indicator] = actual
    for result_name in (RESULT_SCORE_FIELD, RESULT_GRADE_FIELD):
        actual = existing.get(result_name.upper())
        if actual is None:
            actual = validated_field_name(result_name, workspace, existing_upper)
            field_type = "SHORT" if result_name == RESULT_GRADE_FIELD else "DOUBLE"
            arcpy.management.AddField(feature_class, actual, field_type, field_alias=result_name)
            existing[actual.upper()] = actual
        result_map[result_name] = actual
    return result_map


def copy_source_to_output(job: CalculationJob) -> str:
    delete_output_dataset(job.output_path, job.output_feature_name)
    source_dataset = source_dataset_path(job.source)
    if job.output_kind == "shp":
        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        arcpy.conversion.ExportFeatures(source_dataset, str(job.output_path))
        return str(job.output_path)
    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    if not job.output_path.exists():
        arcpy.management.CreateFileGDB(str(job.output_path.parent), job.output_path.name)
    if not job.output_feature_name:
        raise RuntimeError("输出到 GDB 时必须指定面要素类名称。")
    out_fc = str(job.output_path / job.output_feature_name)
    arcpy.conversion.ExportFeatures(source_dataset, out_fc)
    return out_fc


def copied_source_field_map(source_dataset: str, output_fc: str) -> dict[str, str]:
    source_fields = [field.name for field in arcpy.ListFields(source_dataset) if is_data_field(field)]
    output_fields = [field.name for field in arcpy.ListFields(output_fc) if is_data_field(field)]
    if len(output_fields) < len(source_fields):
        raise RuntimeError(
            f"输出字段数量少于输入字段数量，无法建立计算字段映射：输入 {len(source_fields)} 个，输出 {len(output_fields)} 个。"
        )
    return {source_name: output_fields[index] for index, source_name in enumerate(source_fields)}


def result_original_names(rule_set: RuleSet) -> set[str]:
    return {f"F{indicator}" for indicator in rule_set.indicators} | {RESULT_SCORE_FIELD, RESULT_GRADE_FIELD}


def source_result_field_names(source: VectorSource, rule_set: RuleSet) -> set[str]:
    names = set(result_original_names(rule_set))
    mapping = read_shp_field_mapping(source)
    for original in result_original_names(rule_set):
        actual = mapping.get(original)
        if actual:
            names.add(actual)
    return names


def delete_existing_result_fields(
    output_fc: str,
    source: VectorSource,
    rule_set: RuleSet,
    source_output_map: dict[str, str],
    logger: logging.Logger,
) -> None:
    originals = result_original_names(rule_set)
    source_result_names = source_result_field_names(source, rule_set)
    delete_names: set[str] = set()
    for source_name, output_name in source_output_map.items():
        if source_name in source_result_names:
            delete_names.add(output_name)
    for field in arcpy.ListFields(output_fc):
        if not is_data_field(field):
            continue
        alias = field.aliasName or field.name
        if field.name in originals or alias in originals or field.name in source_result_names:
            delete_names.add(field.name)
    if delete_names:
        logger.info("删除输出副本中已有计算字段：%s", sorted(delete_names))
        arcpy.management.DeleteField(output_fc, sorted(delete_names))


def bound_output_field_map(source_output_map: dict[str, str], input_bindings: dict[str, FieldBinding]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for binding in input_bindings.values():
        actual = source_output_map.get(binding.source_field)
        if actual is None:
            raise RuntimeError(f"无法在输出中找到输入字段：{binding.source_field}")
        mapping[binding.source_field] = actual
    return mapping


def copy_field_mapping_if_needed(job: CalculationJob, source_output_map: dict[str, str], result_map: dict[str, str]) -> None:
    if job.output_kind != "shp":
        return
    original_mapping = read_shp_field_mapping(job.source)
    mapping = dict(original_mapping)
    for field in arcpy.ListFields(source_dataset_path(job.source)):
        if is_data_field(field):
            mapping.setdefault(field.name, source_output_map.get(field.name, field.name))
    for indicator, actual in result_map.items():
        if indicator in {RESULT_SCORE_FIELD, RESULT_GRADE_FIELD}:
            mapping[indicator] = actual
        else:
            mapping[f"F{indicator}"] = actual
    job.output_path.with_suffix(".fields.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


def build_blank_result_report(
    feature_class: str,
    check_fields: list[str],
    title: str,
    oid_filter: set[int] | None = None,
) -> tuple[bool, str, dict[str, int]]:
    oid_name = oid_field_name(feature_class)
    excluded = {oid_name.upper(), *(field.upper() for field in check_fields)}
    sample_fields = [field for field in first_data_field_names(feature_class) if field.upper() not in excluded]
    fields = [oid_name, *check_fields, *sample_fields]
    checked = 0
    missing_by_field: dict[str, int] = {field: 0 for field in check_fields}
    issue_lines: list[str] = []
    show_details = True
    detail_count = 0
    detail_limit = ISSUE_DETAIL_LIMIT

    with arcpy.da.SearchCursor(feature_class, fields) as cursor:
        for row in cursor:
            oid = int(row[0])
            if oid_filter is not None and oid not in oid_filter:
                continue
            checked += 1
            result_values = row[1 : 1 + len(check_fields)]
            sample_values = row[1 + len(check_fields) :]
            missing_fields = [field for field, value in zip(check_fields, result_values) if is_blank_value(value)]
            if not missing_fields:
                continue
            for field in missing_fields:
                missing_by_field[field] += 1
            detail_count += 1
            if show_details and detail_count <= detail_limit:
                values = "；".join(f"{name}={field_value_text(value)}" for name, value in zip(sample_fields, sample_values)) or "无可展示字段值"
                issue_lines.append(f"   - OID={oid}；缺失字段：{'、'.join(missing_fields)}；{values}")
            elif detail_count > detail_limit:
                show_details = False

    missing_total = sum(missing_by_field.values())
    lines: list[str] = []
    lines.append(title)
    lines.append("=" * 60)
    lines.append(f"检查要素数：{checked}")
    lines.append(f"检查字段数：{len(check_fields)}")
    lines.append(f"缺失值总数：{missing_total}")
    lines.append("")
    if missing_total:
        lines.append("一、字段缺失统计")
        for field, count in missing_by_field.items():
            if count:
                lines.append(f"   - {field}：{count} 个要素缺失")
        lines.append("")
        lines.append("二、要素级样例")
        if detail_count > detail_limit:
            lines.append(f"缺失要素过多（{detail_count} 个），只输出字段汇总，不展开全部要素。")
            lines.extend(issue_lines)
        else:
            lines.extend(issue_lines)
    else:
        lines.append("未发现结果字段空值。")
    stats = {"checked_features": checked, "missing_values": missing_total, "issue_features": detail_count}
    return missing_total == 0, "\n".join(lines), stats


def calculate_output(job: CalculationJob, logger: logging.Logger) -> tuple[str, int, dict[str, int]]:
    require_runtime()
    arcpy.env.overwriteOutput = True
    output_fc = copy_source_to_output(job)
    copied_field_map = copied_source_field_map(source_dataset_path(job.source), output_fc)
    delete_existing_result_fields(output_fc, job.source, job.rule_set, copied_field_map, logger)
    source_output_map = bound_output_field_map(copied_field_map, job.field_bindings)
    result_map = ensure_result_fields(output_fc, job.output_kind, job.rule_set)

    read_fields = [source_output_map[binding.source_field] for binding in job.field_bindings.values()]
    indicators = list(job.field_bindings.keys())
    update_fields = read_fields + [result_map[indicator] for indicator in indicators] + [
        result_map[RESULT_SCORE_FIELD],
        result_map[RESULT_GRADE_FIELD],
    ]
    calculated = 0
    with arcpy.da.UpdateCursor(output_fc, update_fields) as cursor:
        for row in cursor:
            input_values = row[: len(indicators)]
            memberships: list[float] = []
            score = 0.0
            for indicator, value in zip(indicators, input_values):
                if indicator in job.rule_set.concept_memberships:
                    membership = job.rule_set.concept_memberships[indicator][normalize_category(value)]
                else:
                    membership = membership_for_numeric(job.rule_set.numeric_rules[indicator], float(value))
                memberships.append(membership)
                score += membership * job.rule_set.weights[indicator]
            grade = grade_for_score(score, job.rule_set.grade_rules)
            row[len(indicators) :] = memberships + [score, grade]
            cursor.updateRow(row)
            calculated += 1
    copy_field_mapping_if_needed(job, source_output_map, result_map)
    check_fields = [result_map[indicator] for indicator in indicators] + [
        result_map[RESULT_SCORE_FIELD],
        result_map[RESULT_GRADE_FIELD],
    ]
    ok, audit_text, audit_stats = build_blank_result_report(output_fc, check_fields, "隶属度计算后结果完整性审计")
    logger.info("结果完整性审计：\n%s", audit_text)
    if not ok:
        raise RuntimeError(f"计算后仍有结果字段空值：{audit_stats['missing_values']} 个。详情见日志。")
    logger.info("输出完成：%s；计算要素数：%s", output_fc, calculated)
    return output_fc, calculated, audit_stats


def setup_job_logger(logs_dir: Path, job_id: str) -> tuple[logging.Logger, Path]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"membership_{timestamp_for_file()}_{job_id}.log"
    logger = logging.getLogger(f"membership_arcpy.{job_id}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger, log_path


def close_job_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def append_history(history_path: Path, record: dict) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


class CalculationWorker(threading.Thread):
    def __init__(self, job_queue: queue.Queue, event_queue: queue.Queue, logs_dir: Path, process_dir: Path):
        super().__init__(daemon=True)
        self.job_queue = job_queue
        self.event_queue = event_queue
        self.logs_dir = logs_dir
        self.process_dir = process_dir
        self.history_path = logs_dir / "membership_history.jsonl"

    def send(self, event_type: str, payload: dict) -> None:
        self.event_queue.put((event_type, payload))

    def run(self) -> None:
        while True:
            job = self.job_queue.get()
            if job is None:
                self.job_queue.task_done()
                break
            self.process_job(job)
            self.job_queue.task_done()

    def process_job(self, job: CalculationJob) -> None:
        logger, log_path = setup_job_logger(self.logs_dir, job.job_id)
        temp_dir = self.process_dir / f"membership_{timestamp_for_file()}_{job.job_id}"
        output_target = str(job.output_path / job.output_feature_name) if job.output_kind == "gdb" else str(job.output_path)
        record = {
            "job_id": job.job_id,
            "created_at": job.created_at,
            "started_at": now_text(),
            "ended_at": None,
            "status": "running",
            "input_source": source_label(job.source),
            "output_path": output_target,
            "area_name": job.rule_set.area_name,
            "rule_path": str(job.rule_set.rule_path),
            "validation_report": job.validation_report,
            "log_path": str(log_path),
            "error": None,
            "calculated_count": None,
            "audit_stats": None,
        }
        self.send("job_started", {"job_id": job.job_id, "message": "开始计算隶属度和质量等级", "log_path": str(log_path)})
        try:
            require_runtime()
            temp_dir.mkdir(parents=True, exist_ok=True)
            logger.info("任务开始：%s", job.job_id)
            logger.info("输入源：%s", source_label(job.source))
            logger.info("输出目标：%s", output_target)
            logger.info("规则文件：%s", job.rule_set.rule_path)
            logger.info("检查报告：\n%s", job.validation_report)
            output_fc, count, audit_stats = calculate_output(job, logger)
            record.update({"status": "success", "calculated_count": count, "output_path": output_fc, "audit_stats": audit_stats})
            self.send(
                "job_done",
                {
                    "job_id": job.job_id,
                    "message": f"计算完成：{output_fc}；要素数 {count}",
                    "log_path": str(log_path),
                },
            )
        except Exception as exc:
            error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            logger.error(error_text)
            record.update({"status": "failed", "error": str(exc)})
            self.send(
                "job_failed",
                {
                    "job_id": job.job_id,
                    "message": f"计算失败：{exc}",
                    "log_path": str(log_path),
                },
            )
        finally:
            record["ended_at"] = now_text()
            append_history(self.history_path, record)
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info("过程目录已清理：%s", temp_dir)
            close_job_logger(logger)


class MembershipApp:
    def __init__(self, root: Tk):
        self.root = root
        self.paths = resolve_paths(Path.cwd())
        self.rules_dir = self.paths.data_dir / "rules"
        self.logs_dir = self.paths.outputs_dir / "logs"
        self.process_dir = self.paths.outputs_dir / "process_files"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.process_dir.mkdir(parents=True, exist_ok=True)
        self.area_options = self.load_area_options()

        self.area_var = StringVar(value=self.area_options[0] if self.area_options else DEFAULT_AREA_NAME)
        self.input_path_var = StringVar()
        self.input_layer_var = StringVar()
        self.output_kind_var = IntVar(value=1)
        self.output_shp_var = StringVar()
        self.output_gdb_var = StringVar()
        self.output_feature_var = StringVar(value="耕地质量评价结果")
        self.input_source: VectorSource | None = None
        self.available_gdb_sources: list[VectorSource] = []
        self.last_report: ValidationReport | None = None
        self.last_source: VectorSource | None = None

        self.job_queue: queue.Queue = queue.Queue()
        self.event_queue: queue.Queue = queue.Queue()
        self.worker = CalculationWorker(self.job_queue, self.event_queue, self.logs_dir, self.process_dir)
        self.worker.start()

        self.root.title("耕地质量评价隶属度和等级计算工具（ArcPy）")
        self.root.geometry("1120x760")
        self.build_ui()
        self.root.after(200, self.poll_worker_events)

    def load_area_options(self) -> list[str]:
        options = list_rule_area_options(self.rules_dir)
        if DEFAULT_AREA_NAME in options:
            options.remove(DEFAULT_AREA_NAME)
            options.insert(0, DEFAULT_AREA_NAME)
        return options

    def refresh_area_options(self) -> None:
        self.area_options = self.load_area_options()
        self.area_combo["values"] = self.area_options
        if not self.area_options:
            self.area_var.set("")
            self.log_status(f"没有在规则目录中找到规则文件：{self.rules_dir}")
            messagebox.showwarning("提示", f"没有在规则目录中找到 *_机器读取规则.xlsx：\n{self.rules_dir}")
            return
        if self.area_var.get() not in self.area_options:
            self.area_var.set(self.area_options[0])
        self.last_report = None
        self.log_status(f"已刷新国标二级区规则列表：{len(self.area_options)} 个。")

    def build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill=BOTH, expand=True)

        input_frame = ttk.LabelFrame(container, text="1. 输入数据和规则")
        input_frame.pack(fill="x", pady=5)
        area_row = ttk.Frame(input_frame)
        area_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(area_row, text="国标二级区").pack(side=LEFT)
        self.area_combo = ttk.Combobox(area_row, textvariable=self.area_var, state="readonly", values=self.area_options, width=36)
        self.area_combo.pack(side=LEFT, padx=5)
        ttk.Button(area_row, text="检查规则文件", command=self.check_rule_file).pack(side=LEFT, padx=5)
        ttk.Button(area_row, text="刷新规则列表", command=self.refresh_area_options).pack(side=LEFT, padx=5)

        input_row = ttk.Frame(input_frame)
        input_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(input_row, text="输入面矢量").pack(side=LEFT)
        ttk.Entry(input_row, textvariable=self.input_path_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(input_row, text="选择 shp", command=self.choose_input_shp).pack(side=LEFT, padx=3)
        ttk.Button(input_row, text="选择 gdb", command=self.choose_input_gdb).pack(side=LEFT, padx=3)

        layer_row = ttk.Frame(input_frame)
        layer_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(layer_row, text="GDB 面图层").pack(side=LEFT)
        self.layer_combo = ttk.Combobox(layer_row, textvariable=self.input_layer_var, state="readonly", width=88)
        self.layer_combo.pack(side=LEFT, fill="x", expand=True, padx=5)
        self.layer_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_input_source_from_layer())
        ttk.Button(layer_row, text="开始检查", command=self.validate_current_input).pack(side=LEFT, padx=5)

        output_frame = ttk.LabelFrame(container, text="2. 输出位置")
        output_frame.pack(fill="x", pady=5)
        kind_row = ttk.Frame(output_frame)
        kind_row.pack(fill="x", padx=5, pady=2)
        ttk.Radiobutton(kind_row, text="输出 Shapefile（字段名受限制）", variable=self.output_kind_var, value=0).pack(side=LEFT)
        ttk.Radiobutton(kind_row, text="输出 GDB 中的面要素类（推荐）", variable=self.output_kind_var, value=1).pack(side=LEFT, padx=12)

        shp_row = ttk.Frame(output_frame)
        shp_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(shp_row, text="Shapefile").pack(side=LEFT)
        ttk.Entry(shp_row, textvariable=self.output_shp_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(shp_row, text="选择 shp 输出", command=self.choose_output_shp).pack(side=LEFT)

        gdb_row = ttk.Frame(output_frame)
        gdb_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(gdb_row, text="GDB").pack(side=LEFT)
        ttk.Entry(gdb_row, textvariable=self.output_gdb_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(gdb_row, text="选择已有 gdb", command=self.choose_existing_output_gdb).pack(side=LEFT, padx=3)
        ttk.Button(gdb_row, text="新建 gdb", command=self.choose_output_gdb).pack(side=LEFT, padx=3)
        ttk.Label(gdb_row, text="面要素类名").pack(side=LEFT, padx=5)
        ttk.Entry(gdb_row, textvariable=self.output_feature_var, width=24).pack(side=LEFT)
        ttk.Button(gdb_row, text="提交计算任务", command=self.submit_job).pack(side=LEFT, padx=8)

        report_frame = ttk.LabelFrame(container, text="3. 检查报告、日志和历史记录")
        report_frame.pack(fill="both", expand=True, pady=5)
        action_row = ttk.Frame(report_frame)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="查看历史记录", command=self.show_history).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="打开日志文件夹", command=self.open_logs_folder).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="删除日志", command=self.delete_logs).pack(side=LEFT, padx=5, pady=4)
        self.status_text = Text(report_frame, height=22, wrap="word")
        self.status_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.log_status("工具已启动。请先选择输入面矢量并执行检查。")

    def check_rule_file(self) -> None:
        if not self.area_var.get().strip():
            messagebox.showwarning("提示", "请先选择国标二级区。")
            return
        try:
            rule_set = load_rule_set(self.rules_dir, self.area_var.get())
        except Exception as exc:
            messagebox.showerror("规则文件错误", str(exc))
            return
        messagebox.showinfo("规则文件", f"已找到并读取规则文件：\n{rule_set.rule_path}\n\n指标数：{len(rule_set.weights)}")
        self.log_status(f"规则文件检查通过：{rule_set.rule_path}")

    def choose_input_shp(self) -> None:
        path = filedialog.askopenfilename(title="选择输入 shp", filetypes=[("Shapefile", "*.shp")])
        if not path:
            return
        source = make_vector_source("shp", Path(path))
        self.input_source = source
        self.input_path_var.set(str(source.source_path))
        self.input_layer_var.set("")
        self.available_gdb_sources = []
        self.layer_combo["values"] = []
        self.last_report = None
        self.log_status(f"已选择输入 Shapefile：{source_label(source)}")

    def choose_input_gdb(self) -> None:
        path = filedialog.askdirectory(title="选择输入 .gdb 文件夹")
        if not path:
            return
        gdb_path = find_nearest_gdb_path(Path(path))
        if gdb_path is None or not is_gdb_path(gdb_path):
            messagebox.showwarning("提示", "请选择一个 .gdb 文件夹。")
            return
        try:
            sources = list_gdb_polygon_layers(gdb_path)
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        if not sources:
            messagebox.showwarning("提示", "这个 gdb 中没有识别到面图层。")
            return
        self.available_gdb_sources = sources
        self.input_path_var.set(str(gdb_path))
        self.layer_combo["values"] = [source.layer_name for source in sources]
        self.input_layer_var.set(str(sources[0].layer_name))
        self.input_source = sources[0]
        self.last_report = None
        self.log_status(f"已选择输入 GDB：{gdb_path}，面图层数量 {len(sources)}。")

    def update_input_source_from_layer(self) -> None:
        layer_name = self.input_layer_var.get()
        for source in self.available_gdb_sources:
            if source.layer_name == layer_name:
                self.input_source = source
                self.last_report = None
                self.log_status(f"已选择 GDB 面图层：{source_label(source)}")
                return

    def choose_output_shp(self) -> None:
        path = filedialog.asksaveasfilename(title="选择 Shapefile 输出", defaultextension=".shp", filetypes=[("Shapefile", "*.shp")])
        if path:
            self.output_kind_var.set(0)
            self.output_shp_var.set(path)

    def choose_output_gdb(self) -> None:
        path = filedialog.asksaveasfilename(title="选择或新建 FileGDB", defaultextension=".gdb", filetypes=[("File Geodatabase", "*.gdb")])
        if path:
            if not path.lower().endswith(".gdb"):
                path += ".gdb"
            self.output_kind_var.set(1)
            self.output_gdb_var.set(path)

    def choose_existing_output_gdb(self) -> None:
        path = filedialog.askdirectory(title="选择已有 .gdb 文件夹")
        if not path:
            return
        gdb_path = find_nearest_gdb_path(Path(path))
        if gdb_path is None or not is_gdb_path(gdb_path):
            messagebox.showwarning("提示", "请选择一个已有的 .gdb 文件夹。")
            return
        self.output_kind_var.set(1)
        self.output_gdb_var.set(str(gdb_path))
        self.log_status(f"已选择已有 GDB 输出库：{gdb_path}")

    def validate_current_input(self) -> None:
        if self.input_source is None:
            messagebox.showwarning("提示", "请先选择输入面矢量。")
            return
        if not self.area_var.get().strip():
            messagebox.showwarning("提示", "请先选择国标二级区。")
            return
        try:
            self.log_status("开始检查输入字段、空值、类别值和数值字段类型。")
            rule_set = load_rule_set(self.rules_dir, self.area_var.get())
            report = validate_source(self.input_source, rule_set)
        except Exception as exc:
            messagebox.showerror("检查失败", str(exc))
            return
        self.last_report = report
        self.last_source = self.input_source
        self.show_report(report, ask_continue=False)
        if report.ok:
            self.log_status("检查通过，可以提交计算任务。")
        else:
            self.log_status("检查未通过，已在报告中列出问题。")

    def show_report(self, report: ValidationReport, ask_continue: bool) -> bool:
        window = Toplevel(self.root)
        window.title("隶属度计算前检查报告")
        window.geometry("980x680")
        text = Text(window, wrap="word")
        text.pack(fill=BOTH, expand=True, padx=8, pady=8)
        text.insert(END, report.text)
        text.configure(state="disabled")
        result = {"confirmed": False}

        button_row = ttk.Frame(window)
        button_row.pack(fill="x", padx=8, pady=8)

        def confirm() -> None:
            result["confirmed"] = True
            window.destroy()

        def cancel() -> None:
            result["confirmed"] = False
            window.destroy()

        if ask_continue:
            ttk.Button(button_row, text="确认无误，继续计算", command=confirm).pack(side=LEFT, padx=5)
            ttk.Button(button_row, text="取消", command=cancel).pack(side=LEFT, padx=5)
            window.transient(self.root)
            window.grab_set()
            self.root.wait_window(window)
            return result["confirmed"]
        ttk.Button(button_row, text="关闭", command=window.destroy).pack(side=LEFT, padx=5)
        return False

    def submit_job(self) -> None:
        if self.input_source is None:
            messagebox.showwarning("提示", "请先选择输入面矢量。")
            return
        if not self.area_var.get().strip():
            messagebox.showwarning("提示", "请先选择国标二级区。")
            return
        output_kind = "shp" if self.output_kind_var.get() == 0 else "gdb"
        if output_kind == "shp":
            output_text = self.output_shp_var.get().strip()
            if not output_text:
                messagebox.showwarning("提示", "请选择 Shapefile 输出位置。")
                return
            output_path = Path(output_text)
            if output_path.suffix.lower() != ".shp":
                output_path = output_path.with_suffix(".shp")
            output_feature_name = None
        else:
            gdb_text = self.output_gdb_var.get().strip()
            feature_name = self.output_feature_var.get().strip()
            if not gdb_text or not feature_name:
                messagebox.showwarning("提示", "请选择 GDB 并填写面要素类名。")
                return
            output_path = Path(gdb_text if gdb_text.lower().endswith(".gdb") else f"{gdb_text}.gdb")
            output_feature_name = arcpy.ValidateTableName(feature_name, str(output_path.parent))
        source_dataset = Path(source_dataset_path(self.input_source)).resolve()
        output_dataset = output_dataset_path(output_kind, output_path, output_feature_name)
        if output_dataset.resolve() == source_dataset:
            messagebox.showerror("输出错误", "输出结果不能覆盖输入数据。")
            return
        try:
            report = self.last_report
            if report is None or self.last_source != self.input_source or report.rule_set.area_name != self.area_var.get():
                self.log_status("当前输入没有最新检查报告，正在重新检查。")
                rule_set = load_rule_set(self.rules_dir, self.area_var.get())
                report = validate_source(self.input_source, rule_set)
                self.last_report = report
                self.last_source = self.input_source
            if not report.ok:
                self.show_report(report, ask_continue=False)
                messagebox.showerror("检查未通过", "输入数据存在问题，不能计算。请先按报告修正。")
                return
        except Exception as exc:
            messagebox.showerror("提交失败", str(exc))
            return
        if not self.show_report(report, ask_continue=True):
            self.log_status("用户取消计算任务，未提交。")
            return
        job = CalculationJob(
            job_id=uuid.uuid4().hex[:8],
            source=self.input_source,
            output_path=output_path,
            output_feature_name=output_feature_name,
            output_kind=output_kind,
            rule_set=report.rule_set,
            field_bindings=report.field_bindings,
            created_at=now_text(),
            validation_report=report.text,
        )
        self.job_queue.put(job)
        self.log_status(f"已提交任务 {job.job_id}，后台执行中。")

    def poll_worker_events(self) -> None:
        try:
            while True:
                event_type, payload = self.event_queue.get_nowait()
                self.log_status(f"[{payload.get('job_id')}] {payload.get('message')}")
                if event_type in {"job_done", "job_failed"} and payload.get("log_path"):
                    self.log_status(f"日志：{payload['log_path']}")
        except queue.Empty:
            pass
        self.root.after(200, self.poll_worker_events)

    def log_status(self, message: str) -> None:
        self.status_text.insert(END, f"{now_text()}  {message}\n")
        self.status_text.see(END)

    def show_history(self) -> None:
        history_path = self.logs_dir / "membership_history.jsonl"
        if not history_path.exists():
            messagebox.showinfo("历史记录", "暂无历史记录。")
            return
        window = Toplevel(self.root)
        window.title("隶属度计算历史记录")
        window.geometry("980x620")
        text = Text(window, wrap="word")
        text.pack(fill=BOTH, expand=True, padx=8, pady=8)
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                text.insert(END, line + "\n")
                continue
            text.insert(
                END,
                f"任务 {record.get('job_id')} | {record.get('status')} | {record.get('created_at')}\n"
                f"输入：{record.get('input_source')}\n"
                f"输出：{record.get('output_path')}\n"
                f"规则：{record.get('rule_path')}\n"
                f"日志：{record.get('log_path')}\n"
                f"错误：{record.get('error') or ''}\n\n",
            )
        text.configure(state="disabled")

    def open_logs_folder(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            import os

            os.startfile(self.logs_dir)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def delete_logs(self) -> None:
        if not messagebox.askyesno("确认删除", "确定删除隶属度计算日志和历史记录吗？"):
            return
        deleted = 0
        for path in self.logs_dir.glob("membership_*.log"):
            path.unlink()
            deleted += 1
        history_path = self.logs_dir / "membership_history.jsonl"
        if history_path.exists():
            history_path.unlink()
            deleted += 1
        self.log_status(f"已删除 {deleted} 个隶属度计算日志/历史文件。")


def main() -> int:
    try:
        require_runtime()
    except Exception as exc:
        root = Tk()
        root.withdraw()
        messagebox.showerror("缺少运行环境", str(exc))
        return 1
    root = Tk()
    MembershipApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
