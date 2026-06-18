"""ArcPy UI tool for updating score fields from calculated results by overlap area."""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, IntVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk

from cropland_quality_update.paths import resolve_paths
from cropland_quality_update.tools import membership_arcpy_ui as membership_tool
from cropland_quality_update.tools import vector_common_arcpy as vector_tool


arcpy = membership_tool.arcpy
ARCPY_IMPORT_ERROR = membership_tool.ARCPY_IMPORT_ERROR

VectorSource = membership_tool.VectorSource
RESULT_SCORE_FIELD = membership_tool.RESULT_SCORE_FIELD
RESULT_GRADE_FIELD = membership_tool.RESULT_GRADE_FIELD

NUMERIC_FIELD_TYPES = {"SmallInteger", "Integer", "Single", "Double"}
TEXT_FIELD_TYPES = {"String"}
FIELD_MATCH_MIN_PREFIX = 3
OUTPUT_TEXT_LENGTH = 255

OUTPUT_FIELD_NAMES = [
    "内部标识码",
    "地类号",
    "地类名称",
    "县名称",
    "县代码",
    "乡名称",
    "村名称",
    "实体面积",
    "实体长度",
    "实体类型",
    "平差面积",
    "地形部位",
    "耕层质地",
    "水资源条件",
    "排水能力",
    "海拔高度",
    "有机质",
    "有效土层厚度",
    "土壤容重",
    "速效钾",
    "有效磷",
    "质地构型",
    "酸碱度",
    "耕层厚度",
    "F地形部位",
    "F耕层质地",
    "F水资源条件",
    "F排水能力",
    "F海拔高度",
    "F有机质",
    "F有效土层厚度",
    "F土壤容重",
    "F速效钾",
    "F有效磷",
    "F质地构型",
    "F酸碱度",
    "F耕层厚度",
    RESULT_SCORE_FIELD,
    RESULT_GRADE_FIELD,
]

INDICATOR_FIELD_NAMES = [
    "地形部位",
    "耕层质地",
    "水资源条件",
    "排水能力",
    "海拔高度",
    "有机质",
    "有效土层厚度",
    "土壤容重",
    "速效钾",
    "有效磷",
    "质地构型",
    "酸碱度",
    "耕层厚度",
]
UPDATE_FIELD_NAMES = INDICATOR_FIELD_NAMES + [f"F{name}" for name in INDICATOR_FIELD_NAMES] + [RESULT_SCORE_FIELD, RESULT_GRADE_FIELD]
OPTIONAL_RESULT_FIELD_NAMES = {"海拔高度", "F海拔高度"}
TEXT_OUTPUT_FIELDS = {
    "内部标识码",
    "地类号",
    "地类名称",
    "县名称",
    "县代码",
    "乡名称",
    "村名称",
    "实体类型",
    "地形部位",
    "耕层质地",
    "水资源条件",
    "排水能力",
    "质地构型",
}
SHORT_OUTPUT_FIELDS = {RESULT_GRADE_FIELD}


@dataclass(frozen=True)
class FieldMatchCandidate:
    field_name: str
    field_type: str
    alias_name: str
    match_text: str
    score: tuple[int, int]


@dataclass(frozen=True)
class ScoreFieldBinding:
    canonical_name: str
    target_field: str | None
    target_type: str | None
    result_field: str
    result_type: str


@dataclass(frozen=True)
class SourceFieldBinding:
    canonical_name: str
    target_field: str
    target_type: str


@dataclass(frozen=True)
class FieldMatchProblem:
    canonical_name: str
    side: str
    message: str


@dataclass(frozen=True)
class UpdatePreflightReport:
    ok: bool
    target_source: VectorSource
    result_source: VectorSource
    target_feature_count: int
    result_feature_count: int
    projection_infos: list[object]
    projections_same: bool
    target_projection_source: VectorSource
    target_spatial_reference: object
    source_bindings: list[SourceFieldBinding]
    field_bindings: list[ScoreFieldBinding]
    problems: list[FieldMatchProblem]
    warnings: list[str]
    text: str


@dataclass(frozen=True)
class UpdateJob:
    job_id: str
    target_source: VectorSource
    result_source: VectorSource
    output_path: Path
    output_feature_name: str | None
    output_kind: str
    target_projection_source: VectorSource
    target_spatial_reference: object
    source_bindings: list[SourceFieldBinding]
    field_bindings: list[ScoreFieldBinding]
    validation_report: str
    created_at: str


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_for_file() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def require_runtime() -> None:
    missing = []
    if arcpy is None:
        missing.append(f"arcpy ({ARCPY_IMPORT_ERROR})")
    if missing:
        raise RuntimeError("缺少运行包：" + "；".join(missing))


def source_dataset_path(source: VectorSource) -> str:
    return membership_tool.source_dataset_path(source)


def source_label(source: VectorSource) -> str:
    return membership_tool.source_label(source)


def source_path_for_log(source: VectorSource) -> str:
    return source.display_name


def output_format(path: Path) -> str:
    return membership_tool.output_format(path)


def is_data_field(field) -> bool:
    return membership_tool.is_data_field(field)


def read_source_spatial_reference(source: VectorSource):
    return vector_tool.read_source_spatial_reference(source)


def spatial_reference_equal(a: object | None, b: object | None) -> bool:
    return vector_tool.spatial_reference_equal(a, b)


def describe_spatial_reference(sr: object | None) -> str:
    return vector_tool.describe_spatial_reference(sr)


def projection_text(sr: object | None) -> str:
    return vector_tool.projection_text(sr)


def make_vector_source(kind: str, source_path: Path, layer_name: str | None = None) -> VectorSource:
    return membership_tool.make_vector_source(kind, source_path, layer_name)


def is_gdb_path(path: Path) -> bool:
    return membership_tool.is_gdb_path(path)


def find_nearest_gdb_path(path: Path) -> Path | None:
    return membership_tool.find_nearest_gdb_path(path)


def list_gdb_polygon_layers(path: Path) -> list[VectorSource]:
    return membership_tool.list_gdb_polygon_layers(path)


def normalize_field_key(value: object) -> str:
    return str(value or "").strip().replace(" ", "").lower()


def logical_names_for_field(source: VectorSource, field) -> list[str]:
    names = [field.name]
    if field.aliasName and field.aliasName != field.name:
        names.append(field.aliasName)
    mapping = membership_tool.read_shp_field_mapping(source)
    for original, actual in mapping.items():
        if actual == field.name:
            names.append(original)
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        text = str(name).strip()
        key = normalize_field_key(text)
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def match_score_for_logical_name(canonical_name: str, logical_name: str) -> tuple[int, int] | None:
    canonical_key = normalize_field_key(canonical_name)
    logical_key = normalize_field_key(logical_name)
    if not canonical_key or not logical_key:
        return None
    if logical_key == canonical_key:
        return (0, -len(logical_key))
    if logical_key.startswith(canonical_key):
        return (1, -len(logical_key))
    if canonical_key.startswith(logical_key) and len(logical_key) >= FIELD_MATCH_MIN_PREFIX:
        return (2, -len(logical_key))
    return None


def field_match_candidates(source: VectorSource, canonical_name: str) -> list[FieldMatchCandidate]:
    candidates: dict[str, FieldMatchCandidate] = {}
    for field in arcpy.ListFields(source_dataset_path(source)):
        if not is_data_field(field):
            continue
        best_score: tuple[int, int] | None = None
        best_text = ""
        for logical_name in logical_names_for_field(source, field):
            score = match_score_for_logical_name(canonical_name, logical_name)
            if score is None:
                continue
            if best_score is None or score < best_score:
                best_score = score
                best_text = logical_name
        if best_score is None:
            continue
        current = candidates.get(field.name)
        candidate = FieldMatchCandidate(field.name, field.type, field.aliasName or field.name, best_text, best_score)
        if current is None or candidate.score < current.score:
            candidates[field.name] = candidate
    return sorted(candidates.values(), key=lambda item: item.score)


def choose_field_match(source: VectorSource, canonical_name: str, side: str) -> tuple[FieldMatchCandidate | None, FieldMatchProblem | None]:
    candidates = field_match_candidates(source, canonical_name)
    if not candidates:
        return None, FieldMatchProblem(canonical_name, side, "没有找到可匹配字段")
    best = candidates[0]
    ties = [candidate for candidate in candidates if candidate.score == best.score]
    if len(ties) > 1:
        names = "、".join(f"{item.field_name}(按 {item.match_text})" for item in ties)
        return None, FieldMatchProblem(canonical_name, side, f"匹配到多个同等候选字段：{names}")
    return best, None


def canonical_update_field_names() -> list[str]:
    return list(UPDATE_FIELD_NAMES)


def validate_polygon_source(source: VectorSource, label: str) -> int:
    dataset = source_dataset_path(source)
    if not arcpy.Exists(dataset):
        raise RuntimeError(f"{label}不存在：{dataset}")
    desc = arcpy.Describe(dataset)
    if getattr(desc, "shapeType", "") != "Polygon":
        raise RuntimeError(f"{label}必须是面矢量。")
    return int(arcpy.management.GetCount(dataset)[0])


def build_source_field_bindings(target_source: VectorSource) -> tuple[list[SourceFieldBinding], list[str]]:
    bindings: list[SourceFieldBinding] = []
    warnings: list[str] = []
    for canonical_name in OUTPUT_FIELD_NAMES:
        match, problem = choose_field_match(target_source, canonical_name, "现有农田面")
        if problem:
            warnings.append(f"{canonical_name}：现有农田面未匹配到字段，输出将留空。")
            continue
        if match is None:
            continue
        bindings.append(SourceFieldBinding(canonical_name, match.field_name, match.field_type))
    return bindings, warnings


def build_field_bindings(
    target_source: VectorSource,
    result_source: VectorSource,
) -> tuple[list[ScoreFieldBinding], list[FieldMatchProblem], list[str]]:
    bindings: list[ScoreFieldBinding] = []
    problems: list[FieldMatchProblem] = []
    warnings: list[str] = []
    seen_result_fields: dict[str, str] = {}

    for canonical_name in canonical_update_field_names():
        target_match, target_problem = choose_field_match(target_source, canonical_name, "现有农田面")
        result_match, result_problem = choose_field_match(result_source, canonical_name, "第一步结果")
        if target_problem:
            warnings.append(f"{canonical_name}：现有农田面未匹配到旧字段，将在输出中创建全称字段并由第一步结果更新。")
        if result_problem:
            if canonical_name in OPTIONAL_RESULT_FIELD_NAMES:
                warnings.append(f"{canonical_name}：第一步结果未匹配到字段，输出将留空。")
            else:
                problems.append(result_problem)
        if result_match is None:
            continue

        if canonical_name in TEXT_OUTPUT_FIELDS:
            allowed_types = NUMERIC_FIELD_TYPES | TEXT_FIELD_TYPES
        else:
            allowed_types = NUMERIC_FIELD_TYPES
        if result_match.field_type not in allowed_types:
            problems.append(
                FieldMatchProblem(canonical_name, "第一步结果", f"字段 {result_match.field_name} 类型不符合要求，实际为 {result_match.field_type}")
            )
            continue

        if target_match and canonical_name not in TEXT_OUTPUT_FIELDS and target_match.field_type not in NUMERIC_FIELD_TYPES:
            warnings.append(f"{canonical_name} -> 现有农田旧字段 {target_match.field_name} 不是数值型；输出会重建全称字段。")

        previous = seen_result_fields.get(result_match.field_name)
        if previous and previous != canonical_name:
            problems.append(FieldMatchProblem(canonical_name, "第一步结果", f"字段 {result_match.field_name} 已匹配给 {previous}"))
            continue
        seen_result_fields[result_match.field_name] = canonical_name

        bindings.append(
            ScoreFieldBinding(
                canonical_name=canonical_name,
                target_field=target_match.field_name if target_match else None,
                target_type=target_match.field_type if target_match else None,
                result_field=result_match.field_name,
                result_type=result_match.field_type,
            )
        )
    return bindings, problems, warnings


def build_preflight_text(report: UpdatePreflightReport) -> str:
    lines: list[str] = []
    lines.append("第二步更新隶属度前审查报告")
    lines.append("=" * 60)
    lines.append(f"现有农田面：{source_label(report.target_source)}")
    lines.append(f"第一步结果：{source_label(report.result_source)}")
    lines.append(f"现有农田面要素数：{report.target_feature_count}")
    lines.append(f"第一步结果要素数：{report.result_feature_count}")
    lines.append("")
    lines.append("一、坐标系检查")
    lines.append(f"两个输入投影是否一致：{'是' if report.projections_same else '否'}")
    for info in report.projection_infos:
        lines.append(f"   - {source_label(info.source)}")
        lines.append(f"     {info.message}")
    lines.append(f"统一使用投影：{describe_spatial_reference(report.target_spatial_reference)}")
    lines.append(f"投影来源：{source_label(report.target_projection_source)}")
    lines.append("")
    lines.append("二、字段匹配")
    expected_count = len(canonical_update_field_names())
    lines.append(f"最终输出字段数：{len(OUTPUT_FIELD_NAMES)}")
    lines.append(f"应从第一步结果更新字段数：{expected_count}")
    lines.append(f"现有农田面可带入基础字段数：{len(report.source_bindings)}")
    lines.append(f"成功匹配字段数：{len(report.field_bindings)}")
    if report.source_bindings:
        lines.append("基础字段来源：")
        for binding in report.source_bindings:
            lines.append(f"   - {binding.canonical_name}: 现有农田 {binding.target_field}({binding.target_type})")
    if report.field_bindings:
        lines.append("更新字段来源：")
        for binding in report.field_bindings:
            lines.append(
                f"   - {binding.canonical_name}: 输出全称字段 <= 第一步结果 {binding.result_field}({binding.result_type})"
            )
    if report.problems:
        lines.append("")
        lines.append("三、需要修正的问题")
        for problem in report.problems:
            lines.append(f"   - {problem.canonical_name}；{problem.side}：{problem.message}")
    else:
        lines.append("")
        lines.append("三、需要修正的问题")
        lines.append("   无。")
    lines.append("")
    lines.append("四、提示")
    if report.warnings:
        lines.extend(f"   - {item}" for item in report.warnings)
    else:
        lines.append("   无。")
    lines.append("")
    lines.append("五、更新规则")
    lines.append("   对每个现有农田面，按第一步结果的重叠面积分组。")
    lines.append("   若未覆盖面积大于或等于任一第一步结果的重叠面积，则不更新。")
    lines.append("   否则使用重叠面积最大的第一步结果，更新各指标、F 隶属度、评价得分和质量等级。")
    lines.append("   输出只保留固定提交字段清单，字段名使用全称并按清单顺序保存。")
    lines.append("")
    lines.append("审查通过，可以继续提交更新任务。" if report.ok else "审查未通过，请先修正字段或数据后重新审查。")
    return "\n".join(lines)


def build_preflight_report(
    target_source: VectorSource,
    result_source: VectorSource,
    target_projection_source: VectorSource,
    target_spatial_reference: object,
) -> UpdatePreflightReport:
    require_runtime()
    target_count = validate_polygon_source(target_source, "现有农田面")
    result_count = validate_polygon_source(result_source, "第一步结果")
    projection_infos = [read_source_spatial_reference(target_source), read_source_spatial_reference(result_source)]
    projections_same = not any(info.spatial_reference is None for info in projection_infos) and spatial_reference_equal(
        projection_infos[0].spatial_reference,
        projection_infos[1].spatial_reference,
    )
    problems: list[FieldMatchProblem] = []
    if target_spatial_reference is None:
        problems.append(FieldMatchProblem("坐标系", "统一投影", "没有选择有效的统一坐标系"))
    for info in projection_infos:
        if info.spatial_reference is None:
            problems.append(FieldMatchProblem("坐标系", source_label(info.source), info.message))
    source_bindings, source_warnings = build_source_field_bindings(target_source)
    bindings, field_problems, warnings = build_field_bindings(target_source, result_source)
    warnings = source_warnings + warnings
    problems.extend(field_problems)
    expected_count = len([name for name in canonical_update_field_names() if name not in OPTIONAL_RESULT_FIELD_NAMES])
    matched_required_count = len([binding for binding in bindings if binding.canonical_name not in OPTIONAL_RESULT_FIELD_NAMES])
    ok = not problems and matched_required_count == expected_count
    draft = UpdatePreflightReport(
        ok=ok,
        target_source=target_source,
        result_source=result_source,
        target_feature_count=target_count,
        result_feature_count=result_count,
        projection_infos=projection_infos,
        projections_same=projections_same,
        target_projection_source=target_projection_source,
        target_spatial_reference=target_spatial_reference,
        source_bindings=source_bindings,
        field_bindings=bindings,
        problems=problems,
        warnings=warnings,
        text="",
    )
    return UpdatePreflightReport(
        ok=draft.ok,
        target_source=draft.target_source,
        result_source=draft.result_source,
        target_feature_count=draft.target_feature_count,
        result_feature_count=draft.result_feature_count,
        projection_infos=draft.projection_infos,
        projections_same=draft.projections_same,
        target_projection_source=draft.target_projection_source,
        target_spatial_reference=draft.target_spatial_reference,
        source_bindings=draft.source_bindings,
        field_bindings=draft.field_bindings,
        problems=draft.problems,
        warnings=draft.warnings,
        text=build_preflight_text(draft),
    )


def build_blank_result_report(
    feature_class: str,
    check_fields: list[str],
    title: str,
    oid_filter: set[int] | None = None,
) -> tuple[bool, str, dict[str, int]]:
    oid_name = membership_tool.oid_field_name(feature_class)
    excluded = {oid_name.upper(), *(field.upper() for field in check_fields)}
    sample_fields = [field for field in membership_tool.first_data_field_names(feature_class) if field.upper() not in excluded]
    fields = [oid_name, *check_fields, *sample_fields]
    checked = 0
    missing_by_field: dict[str, int] = {field: 0 for field in check_fields}
    issue_lines: list[str] = []
    show_details = True
    detail_count = 0
    detail_limit = membership_tool.ISSUE_DETAIL_LIMIT

    with arcpy.da.SearchCursor(feature_class, fields) as cursor:
        for row in cursor:
            oid = int(row[0])
            if oid_filter is not None and oid not in oid_filter:
                continue
            checked += 1
            result_values = row[1 : 1 + len(check_fields)]
            sample_values = row[1 + len(check_fields) :]
            missing_fields = [field for field, value in zip(check_fields, result_values) if membership_tool.is_blank_value(value)]
            if not missing_fields:
                continue
            for field in missing_fields:
                missing_by_field[field] += 1
            detail_count += 1
            if show_details and detail_count <= detail_limit:
                values = (
                    "；".join(f"{name}={membership_tool.field_value_text(value)}" for name, value in zip(sample_fields, sample_values))
                    or "无可展示字段值"
                )
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


def values_equivalent_for_audit(actual, expected) -> bool:
    if membership_tool.is_blank_value(actual) or membership_tool.is_blank_value(expected):
        return False
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return abs(float(actual) - float(expected)) <= 1e-6
    return str(actual).strip() == str(expected).strip()


def build_update_match_report(
    output_fc: str,
    target_id_field: str,
    output_field_bindings: list[ScoreFieldBinding],
    decisions: dict[int, int],
    result_values: dict[int, tuple],
    title: str,
) -> tuple[bool, str, dict[str, int]]:
    output_fields = [binding.target_field for binding in output_field_bindings]
    fields_by_name = {field.name: field for field in arcpy.ListFields(output_fc)}
    excluded = {target_id_field.upper(), *(field.upper() for field in output_fields)}
    sample_fields = [field for field in membership_tool.first_data_field_names(output_fc) if field.upper() not in excluded]

    checked = 0
    issue_features = 0
    missing_result_rows = 0
    issue_by_field: dict[str, int] = {binding.target_field: 0 for binding in output_field_bindings}
    issue_lines: list[str] = []
    detail_limit = membership_tool.ISSUE_DETAIL_LIMIT

    with arcpy.da.SearchCursor(output_fc, [target_id_field, *output_fields, *sample_fields]) as cursor:
        for row in cursor:
            target_id = int(row[0])
            result_id = decisions.get(target_id)
            if result_id is None:
                continue
            checked += 1
            expected_values = result_values.get(result_id)
            actual_values = row[1 : 1 + len(output_fields)]
            sample_values = row[1 + len(output_fields) :]
            if expected_values is None:
                missing_result_rows += 1
                issue_features += 1
                if issue_features <= detail_limit:
                    issue_lines.append(f"   - OID={target_id}；找不到第一步结果 ID={result_id} 的值")
                continue

            field_issues: list[str] = []
            for binding, actual, raw_expected in zip(output_field_bindings, actual_values, expected_values):
                expected = coerce_value_for_field(raw_expected, fields_by_name[binding.target_field])
                if values_equivalent_for_audit(actual, expected):
                    continue
                issue_by_field[binding.target_field] += 1
                field_issues.append(
                    f"{binding.canonical_name}({binding.target_field}) 输出={membership_tool.field_value_text(actual)} "
                    f"应为={membership_tool.field_value_text(expected)}"
                )
            if not field_issues:
                continue
            issue_features += 1
            if issue_features <= detail_limit:
                sample_text = (
                    "；".join(f"{name}={membership_tool.field_value_text(value)}" for name, value in zip(sample_fields, sample_values))
                    or "无可展示字段值"
                )
                issue_lines.append(f"   - OID={target_id}；第一步结果 ID={result_id}；{sample_text}")
                for item in field_issues:
                    issue_lines.append(f"     * {item}")

    issue_total = sum(issue_by_field.values()) + missing_result_rows
    lines: list[str] = []
    lines.append(title)
    lines.append("=" * 60)
    lines.append(f"应更新要素数：{len(decisions)}")
    lines.append(f"实际检查要素数：{checked}")
    lines.append(f"问题要素数：{issue_features}")
    lines.append(f"问题字段/结果行总数：{issue_total}")
    lines.append("")
    if issue_total:
        lines.append("一、字段问题统计")
        if missing_result_rows:
            lines.append(f"   - 找不到第一步结果值：{missing_result_rows} 个要素")
        for field, count in issue_by_field.items():
            if count:
                lines.append(f"   - {field}：{count} 个要素输出值不一致或为空")
        lines.append("")
        lines.append("二、要素级样例")
        if issue_features > detail_limit:
            lines.append(f"问题要素过多（{issue_features} 个），只输出前 {detail_limit} 个样例。")
        lines.extend(issue_lines)
    else:
        lines.append("所有应更新要素的结果字段均与第一步结果一致。")
    stats = {
        "checked_features": checked,
        "issue_features": issue_features,
        "issue_values": issue_total,
        "missing_result_rows": missing_result_rows,
    }
    return issue_total == 0 and checked == len(decisions), "\n".join(lines), stats


def output_target_path(output_path: Path, output_feature_name: str | None, output_kind: str) -> str:
    return str(output_path / output_feature_name) if output_kind == "gdb" and output_feature_name else str(output_path)


def output_dataset_path(output_kind: str, output_path: Path, output_feature_name: str | None) -> Path:
    return output_path if output_kind == "shp" else output_path / str(output_feature_name)


def delete_output_dataset(output_path: Path, output_feature_name: str | None) -> None:
    membership_tool.delete_output_dataset(output_path, output_feature_name)


def output_field_type(name: str, source_type: str | None = None) -> str:
    if name in SHORT_OUTPUT_FIELDS:
        return "SHORT"
    if name in TEXT_OUTPUT_FIELDS:
        return "TEXT"
    return "DOUBLE"


def add_ordered_output_fields(output_fc: str, source_types: dict[str, str]) -> dict[str, str]:
    field_map: dict[str, str] = {}
    existing = {field.name.upper() for field in arcpy.ListFields(output_fc)}
    for name in OUTPUT_FIELD_NAMES:
        field_type = output_field_type(name, source_types.get(name))
        if field_type == "TEXT":
            arcpy.management.AddField(output_fc, name, "TEXT", field_length=OUTPUT_TEXT_LENGTH, field_alias=name)
        else:
            arcpy.management.AddField(output_fc, name, field_type, field_alias=name)
        if name.upper() in existing:
            raise RuntimeError(f"输出字段创建后出现重名：{name}")
        existing.add(name.upper())
        field_map[name] = name
    return field_map


def output_data_field_names(feature_class: str) -> list[str]:
    return [field.name for field in arcpy.ListFields(feature_class) if is_data_field(field)]


def audit_output_schema(feature_class: str) -> None:
    actual = output_data_field_names(feature_class)
    expected = list(OUTPUT_FIELD_NAMES)
    if actual == expected:
        return
    missing = [name for name in expected if name not in actual]
    extra = [name for name in actual if name not in expected]
    misplaced = [
        f"{index + 1}. 应为 {expected_name}，实际为 {actual[index] if index < len(actual) else '缺失'}"
        for index, expected_name in enumerate(expected)
        if index >= len(actual) or actual[index] != expected_name
    ]
    parts = ["输出字段结构不符合固定清单，已中断。"]
    if missing:
        parts.append("缺少字段：" + "、".join(missing))
    if extra:
        parts.append("多余字段：" + "、".join(extra))
    if misplaced:
        parts.append("字段顺序不一致：" + "；".join(misplaced[:20]))
        if len(misplaced) > 20:
            parts.append(f"另有 {len(misplaced) - 20} 处顺序差异。")
    raise RuntimeError("\n".join(parts))


def create_target_output(job: UpdateJob, temp_dir: Path, logger: logging.Logger) -> tuple[str, dict[str, str]]:
    delete_output_dataset(job.output_path, job.output_feature_name)
    source_dataset = source_dataset_path(job.target_source)
    if job.output_kind != "gdb":
        raise RuntimeError("第二步需要保留完整中文字段名和固定字段顺序，请输出到 GDB 面要素类。")
    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    if not job.output_path.exists():
        arcpy.management.CreateFileGDB(str(job.output_path.parent), job.output_path.name)
    if not job.output_feature_name:
        raise RuntimeError("输出到 GDB 时必须指定面要素类名称。")
    source_info = read_source_spatial_reference(job.target_source)
    if source_info.spatial_reference is None:
        raise RuntimeError(f"现有农田面缺少可识别投影：{source_label(job.target_source)}")

    source_types = {binding.canonical_name: binding.target_type for binding in job.source_bindings}
    for binding in job.field_bindings:
        source_types[binding.canonical_name] = binding.result_type
    arcpy.management.CreateFeatureclass(
        str(job.output_path),
        job.output_feature_name,
        "POLYGON",
        spatial_reference=job.target_spatial_reference,
    )
    output_fc = str(job.output_path / job.output_feature_name)
    output_field_map = add_ordered_output_fields(output_fc, source_types)
    audit_output_schema(output_fc)

    source_read_fields = [binding.target_field for binding in job.source_bindings]
    insert_fields = ["SHAPE@"] + [output_field_map[binding.canonical_name] for binding in job.source_bindings]
    source_output_types = {field.name: field for field in arcpy.ListFields(output_fc)}

    copied = 0
    if source_read_fields:
        cursor_fields = ["SHAPE@", *source_read_fields]
    else:
        cursor_fields = ["SHAPE@"]
    if spatial_reference_equal(source_info.spatial_reference, job.target_spatial_reference):
        logger.info("现有农田面投影一致，直接写入输出几何和基础字段。")
        with arcpy.da.InsertCursor(output_fc, insert_fields) as insert_cursor:
            with arcpy.da.SearchCursor(source_dataset, cursor_fields) as search_cursor:
                for row in search_cursor:
                    values = [
                        coerce_value_for_field(value, source_output_types[output_field_map[binding.canonical_name]])
                        for value, binding in zip(row[1:], job.source_bindings)
                    ]
                    insert_cursor.insertRow([row[0], *values])
                    copied += 1
    else:
        temp_gdb = create_temp_gdb(temp_dir, f"project_for_output_{uuid.uuid4().hex[:6]}")
        projected_fc = str(temp_gdb / "projected")
        try:
            logger.info("现有农田面重投影后写入输出。")
            arcpy.management.Project(source_dataset, projected_fc, job.target_spatial_reference)
            with arcpy.da.InsertCursor(output_fc, insert_fields) as insert_cursor:
                with arcpy.da.SearchCursor(projected_fc, cursor_fields) as search_cursor:
                    for row in search_cursor:
                        values = [
                            coerce_value_for_field(value, source_output_types[output_field_map[binding.canonical_name]])
                            for value, binding in zip(row[1:], job.source_bindings)
                        ]
                        insert_cursor.insertRow([row[0], *values])
                        copied += 1
        finally:
            shutil.rmtree(temp_gdb, ignore_errors=True)
    logger.info("输出初始图层创建完成：%s；写入要素数：%s", output_fc, copied)
    return output_fc, output_field_map


def unique_field_name(feature_class: str, base_name: str) -> str:
    workspace = str(Path(feature_class).parent)
    existing = {field.name.upper() for field in arcpy.ListFields(feature_class)}
    candidate = arcpy.ValidateFieldName(base_name, workspace)
    base_candidate = candidate
    suffix = 1
    while candidate.upper() in existing:
        suffix_text = str(suffix)
        candidate = arcpy.ValidateFieldName(f"{base_candidate[: max(1, 64 - len(suffix_text) - 1)]}_{suffix_text}", workspace)
        suffix += 1
    return candidate


def add_oid_copy_field(feature_class: str, base_name: str) -> str:
    id_field = unique_field_name(feature_class, base_name)
    arcpy.management.AddField(feature_class, id_field, "LONG")
    oid_name = arcpy.Describe(feature_class).OIDFieldName
    arcpy.management.CalculateField(feature_class, id_field, f"!{oid_name}!", "PYTHON3")
    return id_field


def create_temp_gdb(temp_dir: Path, name: str) -> Path:
    gdb_path = temp_dir / f"{name}.gdb"
    if gdb_path.exists():
        shutil.rmtree(gdb_path, ignore_errors=True)
    arcpy.management.CreateFileGDB(str(temp_dir), gdb_path.name)
    return gdb_path


def copy_feature_to_temp(source_fc: str, temp_dir: Path, gdb_name: str, feature_name: str) -> str:
    gdb_path = create_temp_gdb(temp_dir, gdb_name)
    out_fc = str(gdb_path / feature_name)
    arcpy.conversion.ExportFeatures(source_fc, out_fc)
    return out_fc


def prepare_result_for_analysis(job: UpdateJob, temp_dir: Path, logger: logging.Logger) -> str:
    source_dataset = source_dataset_path(job.result_source)
    gdb_path = create_temp_gdb(temp_dir, "result_analysis")
    out_fc = str(gdb_path / "result_analysis")
    info = read_source_spatial_reference(job.result_source)
    if info.spatial_reference is None:
        raise RuntimeError(f"第一步结果缺少可识别投影：{source_label(job.result_source)}")
    if spatial_reference_equal(info.spatial_reference, job.target_spatial_reference):
        logger.info("第一步结果投影一致，复制到临时分析图层。")
        arcpy.conversion.ExportFeatures(source_dataset, out_fc)
    else:
        logger.info("第一步结果重投影到临时分析图层。")
        arcpy.management.Project(source_dataset, out_fc, job.target_spatial_reference)
    return out_fc


def field_name_case_insensitive(feature_class: str, wanted: str) -> str:
    wanted_key = wanted.upper()
    for field in arcpy.ListFields(feature_class):
        if field.name.upper() == wanted_key:
            return field.name
    raise RuntimeError(f"字段没有保留到分析结果中：{wanted}")


def build_target_area_map(target_analysis_fc: str, target_id_field: str) -> dict[int, float]:
    totals: dict[int, float] = {}
    with arcpy.da.SearchCursor(target_analysis_fc, [target_id_field, "SHAPE@AREA"]) as cursor:
        for target_id, area in cursor:
            if target_id is None:
                continue
            totals[int(target_id)] = float(area or 0.0)
    return totals


def build_result_value_map(result_analysis_fc: str, result_id_field: str, result_fields: list[str]) -> dict[int, tuple]:
    values: dict[int, tuple] = {}
    with arcpy.da.SearchCursor(result_analysis_fc, [result_id_field, *result_fields]) as cursor:
        for row in cursor:
            result_id = row[0]
            if result_id is None:
                continue
            values[int(result_id)] = tuple(row[1:])
    return values


def intersect_overlap_areas(
    target_analysis_fc: str,
    result_analysis_fc: str,
    target_id_field: str,
    result_id_field: str,
    temp_dir: Path,
    logger: logging.Logger,
) -> dict[int, dict[int, float]]:
    gdb_path = create_temp_gdb(temp_dir, "overlap_intersect")
    intersect_fc = str(gdb_path / "overlap_intersect")
    logger.info("开始计算现有农田面与第一步结果的重叠面积。")
    arcpy.analysis.PairwiseIntersect([target_analysis_fc, result_analysis_fc], intersect_fc, "ALL")
    count = int(arcpy.management.GetCount(intersect_fc)[0])
    logger.info("重叠分析结果要素数：%s", count)
    if count <= 0:
        return {}
    target_field = field_name_case_insensitive(intersect_fc, target_id_field)
    result_field = field_name_case_insensitive(intersect_fc, result_id_field)
    overlaps: dict[int, dict[int, float]] = {}
    with arcpy.da.SearchCursor(intersect_fc, [target_field, result_field, "SHAPE@AREA"]) as cursor:
        for target_id, result_id, area in cursor:
            if target_id is None or result_id is None:
                continue
            target_key = int(target_id)
            result_key = int(result_id)
            overlaps.setdefault(target_key, {})
            overlaps[target_key][result_key] = overlaps[target_key].get(result_key, 0.0) + float(area or 0.0)
    return overlaps


def decide_update_sources(
    total_areas: dict[int, float],
    overlaps: dict[int, dict[int, float]],
) -> tuple[dict[int, int], dict[str, int]]:
    decisions: dict[int, int] = {}
    stats = {
        "total": len(total_areas),
        "updated": 0,
        "no_overlap": 0,
        "uncovered_dominates": 0,
        "result_tie": 0,
        "zero_area": 0,
    }
    for target_id, total_area in total_areas.items():
        if total_area <= 0:
            stats["zero_area"] += 1
            continue
        by_result = overlaps.get(target_id, {})
        if not by_result:
            stats["no_overlap"] += 1
            continue
        result_id, max_area = max(by_result.items(), key=lambda item: item[1])
        covered_area = sum(max(0.0, area) for area in by_result.values())
        uncovered_area = max(0.0, total_area - covered_area)
        epsilon = max(total_area * 1e-9, 1e-9)
        if max_area <= uncovered_area + epsilon:
            stats["uncovered_dominates"] += 1
            continue
        tied_results = [candidate_id for candidate_id, area in by_result.items() if abs(area - max_area) <= epsilon]
        if len(tied_results) > 1:
            stats["result_tie"] += 1
            continue
        decisions[target_id] = result_id
        stats["updated"] += 1
    return decisions, stats


def coerce_value_for_field(value, field) -> object:
    if value is None:
        return None
    if field.type in {"SmallInteger", "Integer"}:
        try:
            return int(float(value))
        except Exception:
            return None
    if field.type in {"Single", "Double"}:
        try:
            return float(value)
        except Exception:
            return None
    if field.type == "String":
        text = str(value)
        if field.length:
            text = text[: int(field.length)]
        return text
    return value


def update_output_fields(
    output_fc: str,
    target_id_field: str,
    output_field_bindings: list[ScoreFieldBinding],
    decisions: dict[int, int],
    result_values: dict[int, tuple],
    logger: logging.Logger,
) -> int:
    output_fields = [binding.target_field for binding in output_field_bindings]
    fields_by_name = {field.name: field for field in arcpy.ListFields(output_fc)}
    updated = 0
    with arcpy.da.UpdateCursor(output_fc, [target_id_field, *output_fields]) as cursor:
        for row in cursor:
            target_id = int(row[0])
            result_id = decisions.get(target_id)
            if result_id is None:
                continue
            values = result_values.get(result_id)
            if values is None:
                logger.warning("找不到第一步结果值，跳过现有农田 OID=%s，结果 ID=%s", target_id, result_id)
                continue
            converted = [
                coerce_value_for_field(value, fields_by_name[binding.target_field])
                for value, binding in zip(values, output_field_bindings)
            ]
            row[1:] = converted
            cursor.updateRow(row)
            updated += 1
    return updated


def calculate_update(job: UpdateJob, temp_dir: Path, logger: logging.Logger) -> tuple[str, dict[str, int]]:
    require_runtime()
    arcpy.env.overwriteOutput = True
    output_fc, output_field_map = create_target_output(job, temp_dir, logger)
    output_id_field = add_oid_copy_field(output_fc, "CQ_TARGET_OID")

    result_stats: dict[str, int] | None = None
    try:
        output_bindings: list[ScoreFieldBinding] = []
        output_fields_by_name = {field.name: field for field in arcpy.ListFields(output_fc)}
        for binding in job.field_bindings:
            output_field = output_field_map.get(binding.canonical_name)
            if output_field is None:
                raise RuntimeError(f"输出结果中找不到固定字段：{binding.canonical_name}")
            output_bindings.append(
                ScoreFieldBinding(
                    canonical_name=binding.canonical_name,
                    target_field=output_field,
                    target_type=output_fields_by_name[output_field].type,
                    result_field=binding.result_field,
                    result_type=binding.result_type,
                )
            )
        target_analysis_fc = copy_feature_to_temp(output_fc, temp_dir, "target_analysis", "target_analysis")
        target_id_field = field_name_case_insensitive(target_analysis_fc, output_id_field)
        result_analysis_fc = prepare_result_for_analysis(job, temp_dir, logger)
        result_id_field = add_oid_copy_field(result_analysis_fc, "CQ_RESULT_OID")

        total_areas = build_target_area_map(target_analysis_fc, target_id_field)
        result_fields = [field_name_case_insensitive(result_analysis_fc, binding.result_field) for binding in job.field_bindings]
        result_values = build_result_value_map(result_analysis_fc, result_id_field, result_fields)
        overlaps = intersect_overlap_areas(target_analysis_fc, result_analysis_fc, target_id_field, result_id_field, temp_dir, logger)
        decisions, stats = decide_update_sources(total_areas, overlaps)
        updated_count = update_output_fields(output_fc, output_id_field, output_bindings, decisions, result_values, logger)
        stats["updated"] = updated_count
        ok, audit_text, audit_stats = build_update_match_report(
            output_fc,
            output_id_field,
            output_bindings,
            decisions,
            result_values,
            "第二步隶属度更新后结果一致性审计",
        )
        logger.info("结果完整性审计：\n%s", audit_text)
        stats["audit_checked_features"] = audit_stats["checked_features"]
        stats["audit_issue_values"] = audit_stats["issue_values"]
        stats["audit_issue_features"] = audit_stats["issue_features"]
        stats["audit_missing_result_rows"] = audit_stats["missing_result_rows"]
        if not ok:
            raise RuntimeError(f"更新后仍有应更新要素的结果字段不一致或为空：{audit_stats['issue_values']} 个。详情见日志。")
        logger.info("更新统计：%s", stats)
        result_stats = stats
    finally:
        try:
            arcpy.management.DeleteField(output_fc, output_id_field)
            logger.info("已删除输出副本临时 ID 字段：%s", output_id_field)
        except Exception as exc:
            logger.warning("输出副本临时 ID 字段删除失败：%s", exc)
    audit_output_schema(output_fc)
    logger.info("输出字段结构审计通过。")
    logger.info("输出完成：%s", output_fc)
    if result_stats is None:
        raise RuntimeError("更新统计未生成，任务状态异常。")
    return output_fc, result_stats


def setup_job_logger(logs_dir: Path, job_id: str) -> tuple[logging.Logger, Path]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"update_scores_{timestamp_for_file()}_{job_id}.log"
    logger = logging.getLogger(f"update_scores_arcpy.{job_id}")
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


class UpdateWorker(threading.Thread):
    def __init__(self, job_queue: queue.Queue, event_queue: queue.Queue, logs_dir: Path, process_dir: Path):
        super().__init__(daemon=True)
        self.job_queue = job_queue
        self.event_queue = event_queue
        self.logs_dir = logs_dir
        self.process_dir = process_dir
        self.history_path = logs_dir / "update_scores_history.jsonl"

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

    def process_job(self, job: UpdateJob) -> None:
        logger, log_path = setup_job_logger(self.logs_dir, job.job_id)
        temp_dir = self.process_dir / f"update_scores_{timestamp_for_file()}_{job.job_id}"
        output_target = output_target_path(job.output_path, job.output_feature_name, job.output_kind)
        record = {
            "job_id": job.job_id,
            "created_at": job.created_at,
            "started_at": now_text(),
            "ended_at": None,
            "status": "running",
            "target_source": source_path_for_log(job.target_source),
            "result_source": source_path_for_log(job.result_source),
            "output_path": output_target,
            "target_projection_source": source_path_for_log(job.target_projection_source),
            "target_projection": describe_spatial_reference(job.target_spatial_reference),
            "source_bindings": [binding.__dict__ for binding in job.source_bindings],
            "field_bindings": [binding.__dict__ for binding in job.field_bindings],
            "validation_report": job.validation_report,
            "log_path": str(log_path),
            "error": None,
            "stats": None,
        }
        self.send("job_started", {"job_id": job.job_id, "message": "开始更新隶属度字段", "log_path": str(log_path)})
        try:
            require_runtime()
            temp_dir.mkdir(parents=True, exist_ok=True)
            logger.info("任务开始：%s", job.job_id)
            logger.info("现有农田面：%s", source_path_for_log(job.target_source))
            logger.info("第一步结果：%s", source_path_for_log(job.result_source))
            logger.info("输出目标：%s", output_target)
            logger.info("统一投影：%s", describe_spatial_reference(job.target_spatial_reference))
            logger.info("字段绑定数量：%s", len(job.field_bindings))
            if job.validation_report:
                logger.info("提交前审查报告：\n%s", job.validation_report)
            output_fc, stats = calculate_update(job, temp_dir, logger)
            record.update({"status": "success", "stats": stats})
            self.send(
                "job_done",
                {
                    "job_id": job.job_id,
                    "message": f"更新完成：{output_fc}；更新要素 {stats.get('updated', 0)} 个",
                    "output_path": output_fc,
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
                    "message": f"更新失败：{exc}",
                    "log_path": str(log_path),
                },
            )
        finally:
            record["ended_at"] = now_text()
            append_history(self.history_path, record)
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info("过程目录已清理：%s", temp_dir)
            close_job_logger(logger)


class ScoreUpdateApp:
    def __init__(
        self,
        root: Tk,
        *,
        embedded: bool = False,
        shared_status_text: Text | None = None,
        on_job_done: Callable[[dict], None] | None = None,
    ):
        self.root = root
        self.embedded = embedded
        self.shared_status_text = shared_status_text
        self.on_job_done = on_job_done
        self.paths = resolve_paths(Path.cwd())
        self.logs_dir = self.paths.outputs_dir / "logs"
        self.process_dir = self.paths.outputs_dir / "process_files"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.process_dir.mkdir(parents=True, exist_ok=True)

        self.target_path_var = StringVar()
        self.target_layer_var = StringVar()
        self.target_source: VectorSource | None = None
        self.target_gdb_sources: list[VectorSource] = []

        self.result_path_var = StringVar()
        self.result_layer_var = StringVar()
        self.result_source: VectorSource | None = None
        self.result_gdb_sources: list[VectorSource] = []

        self.reference_mode = IntVar(value=0)
        self.reference_extra_var = StringVar()
        self.target_spatial_reference = None
        self.target_projection_source: VectorSource | None = None

        self.output_gdb_var = StringVar()
        self.output_feature_var = StringVar(value="Step2_更新隶属度")

        self.last_report: UpdatePreflightReport | None = None
        self.last_report_key: tuple | None = None

        self.job_queue: queue.Queue = queue.Queue()
        self.event_queue: queue.Queue = queue.Queue()
        self.worker = UpdateWorker(self.job_queue, self.event_queue, self.logs_dir, self.process_dir)
        self.worker.start()

        if not self.embedded:
            self.root.title("第二步：更新隶属度（ArcPy）")
            self.root.geometry("1160x820")
            self.root.minsize(1160, 760)
        self.build_ui()
        self.root.after(200, self.poll_worker_events)

    def build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill=BOTH, expand=True)

        input_frame = ttk.LabelFrame(container, text="1. 输入数据")
        input_frame.pack(fill="x", pady=5)

        self.build_source_rows(input_frame, "现有农田面", self.target_path_var, self.target_layer_var, True)
        self.build_source_rows(input_frame, "第一步结果", self.result_path_var, self.result_layer_var, False)

        projection_frame = ttk.LabelFrame(container, text="2. 坐标系统一")
        projection_frame.pack(fill="x", pady=5)
        projection_row = ttk.Frame(projection_frame)
        projection_row.pack(fill="x", padx=5, pady=4)
        ttk.Radiobutton(projection_row, text="使用现有农田面投影", variable=self.reference_mode, value=0, command=self.update_target_projection).pack(side=LEFT)
        ttk.Radiobutton(projection_row, text="使用第一步结果投影", variable=self.reference_mode, value=1, command=self.update_target_projection).pack(side=LEFT, padx=8)
        ttk.Radiobutton(projection_row, text="使用外部 shp 投影", variable=self.reference_mode, value=2, command=self.update_target_projection).pack(side=LEFT, padx=8)
        ttk.Button(projection_row, text="选择外部 shp", command=self.choose_extra_reference).pack(side=LEFT, padx=5)
        ttk.Entry(projection_row, textvariable=self.reference_extra_var).pack(side=LEFT, fill="x", expand=True, padx=5)

        projection_table = ttk.Frame(projection_frame)
        projection_table.pack(fill="x", padx=5, pady=4)
        self.projection_tree = ttk.Treeview(projection_table, columns=("source", "projection"), show="headings", height=3)
        self.projection_tree.heading("source", text="数据源")
        self.projection_tree.heading("projection", text="投影")
        self.projection_tree.column("source", width=1300, anchor="w", stretch=False)
        self.projection_tree.column("projection", width=800, anchor="w", stretch=False)
        projection_y = ttk.Scrollbar(projection_table, orient="vertical", command=self.projection_tree.yview)
        projection_x = ttk.Scrollbar(projection_table, orient="horizontal", command=self.projection_tree.xview)
        self.projection_tree.configure(yscrollcommand=projection_y.set, xscrollcommand=projection_x.set)
        self.projection_tree.grid(row=0, column=0, sticky="nsew")
        projection_y.grid(row=0, column=1, sticky="ns")
        projection_x.grid(row=1, column=0, sticky="ew")
        projection_table.columnconfigure(0, weight=1)

        projection_text_frame = ttk.Frame(projection_frame)
        projection_text_frame.pack(fill="x", padx=5, pady=4)
        self.projection_text = Text(projection_text_frame, height=4, wrap="none")
        projection_text_y = ttk.Scrollbar(projection_text_frame, orient="vertical", command=self.projection_text.yview)
        projection_text_x = ttk.Scrollbar(projection_text_frame, orient="horizontal", command=self.projection_text.xview)
        self.projection_text.configure(yscrollcommand=projection_text_y.set, xscrollcommand=projection_text_x.set)
        self.projection_text.grid(row=0, column=0, sticky="nsew")
        projection_text_y.grid(row=0, column=1, sticky="ns")
        projection_text_x.grid(row=1, column=0, sticky="ew")
        projection_text_frame.columnconfigure(0, weight=1)
        self.projection_text.insert(END, "选择输入数据后，这里会显示统一投影。")

        output_frame = ttk.LabelFrame(container, text="3. 输出位置")
        output_frame.pack(fill="x", pady=5)
        kind_row = ttk.Frame(output_frame)
        kind_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(kind_row, text="第二步固定输出为 GDB 面要素类，以保留完整中文字段名和字段顺序。").pack(side=LEFT)

        gdb_row = ttk.Frame(output_frame)
        gdb_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(gdb_row, text="GDB").pack(side=LEFT)
        ttk.Entry(gdb_row, textvariable=self.output_gdb_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(gdb_row, text="选择已有 gdb", command=self.choose_existing_output_gdb).pack(side=LEFT, padx=3)
        ttk.Button(gdb_row, text="新建 gdb", command=self.choose_output_gdb).pack(side=LEFT, padx=3)
        ttk.Label(gdb_row, text="面要素类名").pack(side=LEFT, padx=5)
        ttk.Entry(gdb_row, textvariable=self.output_feature_var, width=24).pack(side=LEFT)

        report_frame = ttk.LabelFrame(container, text="4. 审查报告、日志和历史记录")
        report_frame.pack(fill="both", expand=True, pady=5)
        action_row = ttk.Frame(report_frame)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="开始审查", command=self.validate_current_inputs).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="提交更新任务", command=self.submit_job).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="查看历史记录", command=self.show_history).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="打开日志文件夹", command=self.open_logs_folder).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="删除日志", command=self.delete_logs).pack(side=LEFT, padx=5, pady=4)
        if self.shared_status_text is None:
            self.status_text = Text(report_frame, height=22, wrap="word")
            self.status_text.pack(fill="both", expand=True, padx=5, pady=5)
        else:
            self.status_text = self.shared_status_text
            ttk.Label(report_frame, text="运行详细信息显示在窗口底部“详细信息”区域。").pack(anchor="w", padx=5, pady=5)
        self.log_status("第二步工具已启动。请先选择现有农田面和第一步结果。")

    def build_source_rows(self, parent, label: str, path_var: StringVar, layer_var: StringVar, is_target: bool) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=5, pady=4)
        ttk.Label(row, text=label, width=10).pack(side=LEFT)
        ttk.Entry(row, textvariable=path_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(row, text="选择 shp", command=self.choose_target_shp if is_target else self.choose_result_shp).pack(side=LEFT, padx=3)
        ttk.Button(row, text="选择 gdb", command=self.choose_target_gdb if is_target else self.choose_result_gdb).pack(side=LEFT, padx=3)

        layer_row = ttk.Frame(parent)
        layer_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(layer_row, text=f"{label}图层", width=10).pack(side=LEFT)
        combo = ttk.Combobox(layer_row, textvariable=layer_var, state="readonly", width=96)
        combo.pack(side=LEFT, fill="x", expand=True, padx=5)
        combo.bind("<<ComboboxSelected>>", self.update_target_source_from_layer if is_target else self.update_result_source_from_layer)
        if is_target:
            self.target_layer_combo = combo
        else:
            self.result_layer_combo = combo

    def choose_target_shp(self) -> None:
        self.choose_shp(is_target=True)

    def choose_result_shp(self) -> None:
        self.choose_shp(is_target=False)

    def choose_shp(self, is_target: bool) -> None:
        path = filedialog.askopenfilename(title="选择 shp", filetypes=[("Shapefile", "*.shp")])
        if not path:
            return
        source = make_vector_source("shp", Path(path))
        if is_target:
            self.target_source = source
            self.target_path_var.set(str(source.source_path))
            self.target_layer_var.set("")
            self.target_gdb_sources = []
            self.target_layer_combo["values"] = []
            self.log_status(f"已选择现有农田 Shapefile：{source_label(source)}")
        else:
            self.result_source = source
            self.result_path_var.set(str(source.source_path))
            self.result_layer_var.set("")
            self.result_gdb_sources = []
            self.result_layer_combo["values"] = []
            self.log_status(f"已选择第一步结果 Shapefile：{source_label(source)}")
        self.last_report = None
        self.update_target_projection()

    def choose_target_gdb(self) -> None:
        self.choose_gdb(is_target=True)

    def choose_result_gdb(self) -> None:
        self.choose_gdb(is_target=False)

    def choose_gdb(self, is_target: bool) -> None:
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
        if is_target:
            self.target_gdb_sources = sources
            self.target_path_var.set(str(gdb_path))
            self.target_layer_combo["values"] = [source.layer_name for source in sources]
            self.target_layer_var.set(str(sources[0].layer_name))
            self.target_source = sources[0]
            self.log_status(f"已选择现有农田 GDB：{gdb_path}，面图层数量 {len(sources)}。")
        else:
            self.result_gdb_sources = sources
            self.result_path_var.set(str(gdb_path))
            self.result_layer_combo["values"] = [source.layer_name for source in sources]
            self.result_layer_var.set(str(sources[0].layer_name))
            self.result_source = sources[0]
            self.log_status(f"已选择第一步结果 GDB：{gdb_path}，面图层数量 {len(sources)}。")
        self.last_report = None
        self.update_target_projection()

    def update_target_source_from_layer(self, _event=None) -> None:
        layer_name = self.target_layer_var.get()
        for source in self.target_gdb_sources:
            if source.layer_name == layer_name:
                self.target_source = source
                self.last_report = None
                self.log_status(f"已选择现有农田 GDB 面图层：{source_label(source)}")
                self.update_target_projection()
                return

    def update_result_source_from_layer(self, _event=None) -> None:
        layer_name = self.result_layer_var.get()
        for source in self.result_gdb_sources:
            if source.layer_name == layer_name:
                self.result_source = source
                self.last_report = None
                self.log_status(f"已选择第一步结果 GDB 面图层：{source_label(source)}")
                self.update_target_projection()
                return

    def choose_extra_reference(self) -> None:
        path = filedialog.askopenfilename(title="选择投影参考 shp", filetypes=[("Shapefile", "*.shp")])
        if path:
            self.reference_extra_var.set(path)
            self.reference_mode.set(2)
            self.update_target_projection()

    def update_target_projection(self) -> None:
        if hasattr(self, "projection_tree"):
            for item in self.projection_tree.get_children():
                self.projection_tree.delete(item)
        if hasattr(self, "projection_text"):
            self.projection_text.delete("1.0", END)
        self.target_projection_source = None
        self.target_spatial_reference = None

        sources = [source for source in (self.target_source, self.result_source) if source is not None]
        if hasattr(self, "projection_tree"):
            for source in sources:
                try:
                    info = read_source_spatial_reference(source)
                    message = info.message
                except Exception as exc:
                    message = f"投影读取失败：{exc}"
                self.projection_tree.insert("", END, values=(source_label(source), message))

        reference_source = None
        if self.reference_mode.get() == 0:
            reference_source = self.target_source
        elif self.reference_mode.get() == 1:
            reference_source = self.result_source
        else:
            value = self.reference_extra_var.get().strip()
            if value:
                reference_source = make_vector_source("shp", Path(value))
        self.target_projection_source = reference_source
        if reference_source is None:
            if hasattr(self, "projection_text"):
                self.projection_text.insert(END, "尚未选择有效的投影来源。")
            return
        try:
            info = read_source_spatial_reference(reference_source)
            self.target_spatial_reference = info.spatial_reference
            if hasattr(self, "projection_text"):
                self.projection_text.insert(END, f"投影来源：{source_label(reference_source)}\n\n{projection_text(info.spatial_reference)}")
        except Exception as exc:
            if hasattr(self, "projection_text"):
                self.projection_text.insert(END, f"投影读取失败：{exc}")

    def choose_output_gdb(self) -> None:
        path = filedialog.asksaveasfilename(title="选择或新建 FileGDB", defaultextension=".gdb", filetypes=[("File Geodatabase", "*.gdb")])
        if path:
            if not path.lower().endswith(".gdb"):
                path += ".gdb"
            self.output_gdb_var.set(path)

    def choose_existing_output_gdb(self) -> None:
        path = filedialog.askdirectory(title="选择已有 .gdb 文件夹")
        if not path:
            return
        gdb_path = find_nearest_gdb_path(Path(path))
        if gdb_path is None or not is_gdb_path(gdb_path):
            messagebox.showwarning("提示", "请选择一个已有的 .gdb 文件夹。")
            return
        self.output_gdb_var.set(str(gdb_path))
        self.log_status(f"已选择已有 GDB 输出库：{gdb_path}")

    def report_key(self) -> tuple:
        return (
            self.target_source,
            self.result_source,
            self.reference_mode.get(),
            self.reference_extra_var.get(),
            source_label(self.target_projection_source) if self.target_projection_source else "",
        )

    def validate_current_inputs(self) -> None:
        if self.target_source is None:
            messagebox.showwarning("提示", "请先选择现有农田面。")
            return
        if self.result_source is None:
            messagebox.showwarning("提示", "请先选择第一步结果。")
            return
        self.update_target_projection()
        if self.target_projection_source is None or self.target_spatial_reference is None:
            messagebox.showwarning("提示", "请先选择有效的统一投影。")
            return
        try:
            self.log_status("开始审查坐标和字段匹配。")
            report = build_preflight_report(
                self.target_source,
                self.result_source,
                self.target_projection_source,
                self.target_spatial_reference,
            )
        except Exception as exc:
            messagebox.showerror("审查失败", str(exc))
            return
        self.last_report = report
        self.last_report_key = self.report_key()
        self.show_report(report, ask_continue=False)
        self.log_status("审查通过，可以提交更新任务。" if report.ok else "审查未通过，已在报告中列出问题。")

    def show_report(self, report: UpdatePreflightReport, ask_continue: bool) -> bool:
        window = Toplevel(self.root)
        window.title("第二步更新隶属度前审查报告")
        window.geometry("1020x700")
        text_frame = ttk.Frame(window)
        text_frame.pack(fill=BOTH, expand=True, padx=8, pady=8)
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        text = Text(text_frame, wrap="word")
        text_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=text_scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        text_scroll.grid(row=0, column=1, sticky="ns")
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
            ttk.Button(button_row, text="确认无误，继续更新", command=confirm).pack(side=LEFT, padx=5)
            ttk.Button(button_row, text="取消", command=cancel).pack(side=LEFT, padx=5)
            window.transient(self.root)
            window.grab_set()
            self.root.wait_window(window)
            return result["confirmed"]
        ttk.Button(button_row, text="关闭", command=window.destroy).pack(side=LEFT, padx=5)
        return False

    def output_settings(self) -> tuple[str, Path, str | None] | None:
        output_kind = "gdb"
        gdb_text = self.output_gdb_var.get().strip()
        feature_name = self.output_feature_var.get().strip()
        if not gdb_text or not feature_name:
            messagebox.showwarning("提示", "请选择 GDB 并填写面要素类名。")
            return None
        output_path = Path(gdb_text if gdb_text.lower().endswith(".gdb") else f"{gdb_text}.gdb")
        output_feature_name = membership_tool.validate_gdb_feature_name(feature_name, output_path)
        if output_feature_name != feature_name:
            self.output_feature_var.set(output_feature_name)
            self.log_status(f"输出要素类名已按 FileGDB 规则修正为：{output_feature_name}")
        return output_kind, output_path, output_feature_name

    def submit_job(self) -> None:
        if self.target_source is None or self.result_source is None:
            messagebox.showwarning("提示", "请先选择现有农田面和第一步结果。")
            return
        output = self.output_settings()
        if output is None:
            return
        output_kind, output_path, output_feature_name = output
        output_dataset = output_dataset_path(output_kind, output_path, output_feature_name)
        for label, source in (("现有农田面", self.target_source), ("第一步结果", self.result_source)):
            input_dataset = Path(source_dataset_path(source)).resolve()
            if output_dataset.resolve() == input_dataset:
                messagebox.showerror("输出错误", f"输出结果不能覆盖{label}输入数据，请换一个输出名称。")
                return
            if output_kind == "gdb" and source.kind == "gdb" and output_path.resolve() == source.source_path.resolve():
                self.log_status(f"输出将保存到{label}所在 GDB 的新图层：{output_feature_name}")
        try:
            report = self.last_report
            if report is None or self.last_report_key != self.report_key():
                self.log_status("当前输入没有最新审查报告，正在重新审查。")
                self.validate_current_inputs()
                report = self.last_report
            if report is None:
                return
            if not report.ok:
                self.show_report(report, ask_continue=False)
                messagebox.showerror("审查未通过", "输入数据存在问题，不能更新。请先按报告修正。")
                return
        except Exception as exc:
            messagebox.showerror("提交失败", str(exc))
            return
        if not self.show_report(report, ask_continue=True):
            self.log_status("用户取消更新任务，未提交。")
            return
        job = UpdateJob(
            job_id=uuid.uuid4().hex[:8],
            target_source=self.target_source,
            result_source=self.result_source,
            output_path=output_path,
            output_feature_name=output_feature_name,
            output_kind=output_kind,
            target_projection_source=report.target_projection_source,
            target_spatial_reference=report.target_spatial_reference,
            source_bindings=report.source_bindings,
            field_bindings=report.field_bindings,
            validation_report=report.text,
            created_at=now_text(),
        )
        self.job_queue.put(job)
        self.log_status(f"已提交任务 {job.job_id}，后台执行中。")

    def poll_worker_events(self) -> None:
        try:
            while True:
                event_type, payload = self.event_queue.get_nowait()
                self.log_status(f"[{payload.get('job_id')}] {payload.get('message')}")
                if event_type == "job_done" and self.on_job_done:
                    self.on_job_done(payload)
                if event_type in {"job_done", "job_failed"} and payload.get("log_path"):
                    self.log_status(f"日志：{payload['log_path']}")
        except queue.Empty:
            pass
        self.root.after(200, self.poll_worker_events)

    def log_status(self, message: str) -> None:
        self.status_text.insert(END, f"{now_text()}  {message}\n")
        self.status_text.see(END)

    def show_history(self) -> None:
        history_path = self.logs_dir / "update_scores_history.jsonl"
        if not history_path.exists():
            messagebox.showinfo("历史记录", "暂无历史记录。")
            return
        window = Toplevel(self.root)
        window.title("第二步更新隶属度历史记录")
        window.geometry("980x620")
        text_frame = ttk.Frame(window)
        text_frame.pack(fill=BOTH, expand=True, padx=8, pady=8)
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        text = Text(text_frame, wrap="word")
        text_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=text_scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        text_scroll.grid(row=0, column=1, sticky="ns")
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
                f"现有农田：{record.get('target_source')}\n"
                f"第一步结果：{record.get('result_source')}\n"
                f"输出：{record.get('output_path')}\n"
                f"统计：{record.get('stats')}\n"
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
        if not messagebox.askyesno("确认删除", "确定删除第二步更新隶属度日志和历史记录吗？"):
            return
        deleted = 0
        for path in self.logs_dir.glob("update_scores_*.log"):
            path.unlink()
            deleted += 1
        history_path = self.logs_dir / "update_scores_history.jsonl"
        if history_path.exists():
            history_path.unlink()
            deleted += 1
        self.log_status(f"已删除 {deleted} 个第二步更新隶属度日志/历史文件。")

    def reset_inputs(self) -> None:
        self.target_path_var.set("")
        self.target_layer_var.set("")
        self.target_source = None
        self.target_gdb_sources = []
        self.result_path_var.set("")
        self.result_layer_var.set("")
        self.result_source = None
        self.result_gdb_sources = []
        self.reference_mode.set(0)
        self.reference_extra_var.set("")
        self.target_spatial_reference = None
        self.target_projection_source = None
        self.output_gdb_var.set("")
        self.output_feature_var.set("Step2_更新隶属度")
        self.last_report = None
        self.last_report_key = None
        for combo_name in ("target_layer_combo", "result_layer_combo"):
            combo = getattr(self, combo_name, None)
            if combo is not None:
                combo["values"] = []
        if hasattr(self, "projection_tree"):
            for item in self.projection_tree.get_children():
                self.projection_tree.delete(item)
        if hasattr(self, "projection_text"):
            self.projection_text.delete("1.0", END)
            self.projection_text.insert(END, "选择输入数据后，这里会显示统一投影。")
        self.log_status("第二步输入和参数已恢复为启动默认值。")


def main() -> int:
    try:
        require_runtime()
    except Exception as exc:
        try:
            root = Tk()
            root.withdraw()
            messagebox.showerror("缺少运行环境", str(exc))
            root.destroy()
        except Exception:
            print(f"缺少运行环境：{exc}")
        return 1
    root = Tk()
    ScoreUpdateApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
