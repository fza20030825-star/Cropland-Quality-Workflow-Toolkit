"""ArcPy UI tool for transferring third-step results to latest land blocks."""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, IntVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk

from cropland_quality_update.paths import resolve_paths
from cropland_quality_update.tools import membership_arcpy_ui as membership_tool
from cropland_quality_update.tools import merge_common_arcpy_ui as merge_tool
from cropland_quality_update.tools import update_scores_arcpy_ui as score_tool


arcpy = score_tool.arcpy
ARCPY_IMPORT_ERROR = score_tool.ARCPY_IMPORT_ERROR

VectorSource = score_tool.VectorSource
RESULT_SCORE_FIELD = score_tool.RESULT_SCORE_FIELD
RESULT_GRADE_FIELD = score_tool.RESULT_GRADE_FIELD

OUTPUT_FIELD_NAMES = score_tool.OUTPUT_FIELD_NAMES
EVALUATION_FIELD_NAMES = score_tool.UPDATE_FIELD_NAMES

COUNTY_NAME_FIELD = "县名称"
COUNTY_CODE_FIELD = "县代码"
TOWNSHIP_NAME_FIELD = "乡名称"
AREA_FIELD = "实体面积"
LENGTH_FIELD = "实体长度"
ENTITY_TYPE_FIELD = "实体类型"
BALANCED_AREA_FIELD = "平差面积"
LAND_CLASS_FIELD = "地类号"
ADMIN_NAME_SOURCE_FIELD = "行政区名称"
NEAREST_ALL = 0
NEAREST_SAME_LAND_CLASS = 1
CROPLAND_LAND_CLASS_CODES = {"0101", "0102", "0103"}

LAND_BLOCK_FIELD_SPECS = {
    "内部标识码": "标识码",
    LAND_CLASS_FIELD: "地类编码",
    "地类名称": "地类名称",
    "村名称": "坐落单位名称",
}


@dataclass(frozen=True)
class FieldBinding:
    output_field: str
    source_field: str
    source_type: str
    source_label: str


@dataclass(frozen=True)
class FieldProblem:
    field_name: str
    side: str
    message: str


@dataclass(frozen=True)
class TransferPreflightReport:
    ok: bool
    result_source: VectorSource
    block_source: VectorSource
    admin_source: VectorSource
    result_feature_count: int
    block_feature_count: int
    block_cropland_count: int
    admin_feature_count: int
    projection_infos: list[object]
    projections_same: bool
    target_projection_source: VectorSource
    target_spatial_reference: object
    block_bindings: list[FieldBinding]
    result_bindings: list[FieldBinding]
    admin_binding: FieldBinding | None
    nearest_mode: int
    problems: list[FieldProblem]
    warnings: list[str]
    text: str


@dataclass(frozen=True)
class TransferJob:
    job_id: str
    result_source: VectorSource
    block_source: VectorSource
    admin_source: VectorSource
    output_path: Path
    output_feature_name: str | None
    output_kind: str
    target_projection_source: VectorSource
    target_spatial_reference: object
    block_bindings: list[FieldBinding]
    result_bindings: list[FieldBinding]
    admin_binding: FieldBinding
    nearest_mode: int
    validation_report: str
    created_at: str


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_for_file() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def require_runtime() -> None:
    if arcpy is None:
        raise RuntimeError(f"缺少运行包：arcpy ({ARCPY_IMPORT_ERROR})")


def source_dataset_path(source: VectorSource) -> str:
    return score_tool.source_dataset_path(source)


def source_label(source: VectorSource) -> str:
    return score_tool.source_label(source)


def source_path_for_log(source: VectorSource) -> str:
    return source.display_name


def read_source_spatial_reference(source: VectorSource):
    return score_tool.read_source_spatial_reference(source)


def spatial_reference_equal(a: object | None, b: object | None) -> bool:
    return score_tool.spatial_reference_equal(a, b)


def describe_spatial_reference(sr: object | None) -> str:
    return score_tool.describe_spatial_reference(sr)


def projection_text(sr: object | None) -> str:
    return score_tool.projection_text(sr)


def make_vector_source(kind: str, source_path: Path, layer_name: str | None = None) -> VectorSource:
    return score_tool.make_vector_source(kind, source_path, layer_name)


def is_gdb_path(path: Path) -> bool:
    return score_tool.is_gdb_path(path)


def find_nearest_gdb_path(path: Path) -> Path | None:
    return score_tool.find_nearest_gdb_path(path)


def list_gdb_polygon_layers(path: Path) -> list[VectorSource]:
    return score_tool.list_gdb_polygon_layers(path)


def output_target_path(output_path: Path, output_feature_name: str | None, output_kind: str) -> str:
    return str(output_path / output_feature_name) if output_kind == "gdb" and output_feature_name else str(output_path)


def output_dataset_path(output_kind: str, output_path: Path, output_feature_name: str | None) -> Path:
    return output_path if output_kind == "shp" else output_path / str(output_feature_name)


def delete_output_dataset(output_path: Path, output_feature_name: str | None) -> None:
    score_tool.delete_output_dataset(output_path, output_feature_name)


def validate_polygon_source(source: VectorSource, label: str) -> int:
    return score_tool.validate_polygon_source(source, label)


def nearest_mode_text(mode: int) -> str:
    if mode == NEAREST_SAME_LAND_CLASS:
        return "最近相同地类号的有值第三步结果"
    return "最近的有值第三步结果（不限地类号）"


def normalize_land_class_code(value: object) -> str:
    if membership_tool.is_blank_value(value):
        return ""
    text = str(value).strip()
    if text.isdigit():
        return text.zfill(4) if len(text) < 4 else text
    try:
        number = float(text)
    except Exception:
        return text
    if number.is_integer():
        return str(int(number)).zfill(4)
    return text


def is_cropland_code(value: object) -> bool:
    return normalize_land_class_code(value) in CROPLAND_LAND_CLASS_CODES


def choose_field_match(source: VectorSource, wanted_name: str, side: str):
    return score_tool.choose_field_match(source, wanted_name, side)


def build_land_block_bindings(block_source: VectorSource) -> tuple[list[FieldBinding], list[FieldProblem]]:
    bindings: list[FieldBinding] = []
    problems: list[FieldProblem] = []
    for output_name, source_name in LAND_BLOCK_FIELD_SPECS.items():
        match, problem = choose_field_match(block_source, source_name, "最新地类图斑")
        if problem or match is None:
            problems.append(FieldProblem(output_name, "最新地类图斑", f"缺少来源字段：{source_name}"))
            continue
        bindings.append(FieldBinding(output_name, match.field_name, match.field_type, source_name))
    return bindings, problems


def count_cropland_features(block_source: VectorSource, land_class_field: str) -> int:
    count = 0
    with arcpy.da.SearchCursor(source_dataset_path(block_source), [land_class_field]) as cursor:
        for (value,) in cursor:
            if is_cropland_code(value):
                count += 1
    return count


def filter_cropland_blocks(feature_class: str, land_class_field: str, logger: logging.Logger) -> tuple[int, int]:
    kept = 0
    removed = 0
    with arcpy.da.UpdateCursor(feature_class, [land_class_field]) as cursor:
        for (value,) in cursor:
            if is_cropland_code(value):
                kept += 1
            else:
                cursor.deleteRow()
                removed += 1
    logger.info("最新地类图斑农田筛选完成：保留 %s 个，剔除 %s 个。", kept, removed)
    return kept, removed


def build_result_bindings(result_source: VectorSource) -> tuple[list[FieldBinding], list[FieldProblem]]:
    bindings: list[FieldBinding] = []
    problems: list[FieldProblem] = []
    needed = [LAND_CLASS_FIELD, COUNTY_NAME_FIELD, COUNTY_CODE_FIELD, *EVALUATION_FIELD_NAMES]
    seen: set[str] = set()
    for output_name in needed:
        if output_name in seen:
            continue
        seen.add(output_name)
        match, problem = choose_field_match(result_source, output_name, "第三步结果")
        if problem or match is None:
            problems.append(FieldProblem(output_name, "第三步结果", "缺少固定结果字段"))
            continue
        bindings.append(FieldBinding(output_name, match.field_name, match.field_type, output_name))
    return bindings, problems


def build_admin_binding(admin_source: VectorSource) -> tuple[FieldBinding | None, list[FieldProblem]]:
    match, problem = choose_field_match(admin_source, ADMIN_NAME_SOURCE_FIELD, "最新行政区")
    if problem or match is None:
        return None, [FieldProblem(TOWNSHIP_NAME_FIELD, "最新行政区", f"缺少来源字段：{ADMIN_NAME_SOURCE_FIELD}")]
    return FieldBinding(TOWNSHIP_NAME_FIELD, match.field_name, match.field_type, ADMIN_NAME_SOURCE_FIELD), []


def audit_third_result_schema(result_source: VectorSource) -> list[FieldProblem]:
    try:
        score_tool.audit_output_schema(source_dataset_path(result_source))
    except Exception as exc:
        return [FieldProblem("第三步结果字段结构", "第三步结果", str(exc))]
    return []


def build_preflight_text(report: TransferPreflightReport) -> str:
    lines: list[str] = []
    lines.append("第四步更新前审查报告")
    lines.append("=" * 60)
    lines.append(f"第三步结果：{source_label(report.result_source)}")
    lines.append(f"最新地类图斑：{source_label(report.block_source)}")
    lines.append(f"最新行政区：{source_label(report.admin_source)}")
    lines.append(f"第三步结果要素数：{report.result_feature_count}")
    lines.append(f"最新地类图斑要素数：{report.block_feature_count}")
    lines.append(f"其中将参与处理的耕地图斑数（地类编码=0101/0102/0103）：{report.block_cropland_count}")
    lines.append(f"最新行政区要素数：{report.admin_feature_count}")
    lines.append("")
    lines.append("一、坐标系检查")
    lines.append(f"三个输入投影是否一致：{'是' if report.projections_same else '否'}")
    for info in report.projection_infos:
        lines.append(f"   - {source_label(info.source)}")
        lines.append(f"     {info.message}")
    lines.append(f"统一使用投影：{describe_spatial_reference(report.target_spatial_reference)}")
    lines.append(f"投影来源：{source_label(report.target_projection_source)}")
    lines.append("")
    lines.append("二、字段和赋值规则")
    lines.append(f"最终输出字段数：{len(OUTPUT_FIELD_NAMES)}，字段名和顺序与第三步结果一致。")
    lines.append("最新地类图斑直接带入字段：")
    for binding in report.block_bindings:
        lines.append(f"   - {binding.output_field} <= {binding.source_label} / {binding.source_field}({binding.source_type})")
    lines.append("第三步结果字段：")
    for binding in report.result_bindings:
        lines.append(f"   - {binding.output_field} <= {binding.source_field}({binding.source_type})")
    if report.admin_binding:
        lines.append(f"乡名称来源：最新行政区 {report.admin_binding.source_field}({report.admin_binding.source_type})，按最大叠置面积赋值。")
    lines.append("县名称、县代码：读取第三步结果同名字段中出现次数最多的非空值，并赋给全部输出要素。")
    lines.append("最新地类图斑：只保留地类编码为 0101、0102、0103 的耕地图斑，其余地类不进入输出。")
    lines.append("实体面积、实体长度：由输出几何重新计算，单位分别为公顷和 km；实体类型固定为“面”；平差面积留空。")
    lines.append(f"耕评字段：先按第三步结果最大有值叠置面积赋值；无值区占优时，使用{nearest_mode_text(report.nearest_mode)}。")
    lines.append("")
    lines.append("三、需要修正的问题")
    if report.problems:
        for problem in report.problems:
            lines.append(f"   - {problem.field_name}；{problem.side}：{problem.message}")
    else:
        lines.append("   无。")
    lines.append("")
    lines.append("四、提示")
    if report.warnings:
        lines.extend(f"   - {item}" for item in report.warnings)
    else:
        lines.append("   无。")
    lines.append("")
    lines.append("审查通过，可以继续提交更新任务。" if report.ok else "审查未通过，请先修正字段或数据后重新审查。")
    return "\n".join(lines)


def build_preflight_report(
    result_source: VectorSource,
    block_source: VectorSource,
    admin_source: VectorSource,
    target_projection_source: VectorSource,
    target_spatial_reference: object,
    nearest_mode: int,
) -> TransferPreflightReport:
    require_runtime()
    result_count = validate_polygon_source(result_source, "第三步结果")
    block_count = validate_polygon_source(block_source, "最新地类图斑")
    admin_count = validate_polygon_source(admin_source, "最新行政区")
    projection_infos = [
        read_source_spatial_reference(result_source),
        read_source_spatial_reference(block_source),
        read_source_spatial_reference(admin_source),
    ]
    projections_same = not any(info.spatial_reference is None for info in projection_infos)
    if projections_same:
        base_sr = projection_infos[0].spatial_reference
        projections_same = all(spatial_reference_equal(base_sr, info.spatial_reference) for info in projection_infos[1:])

    problems: list[FieldProblem] = []
    warnings: list[str] = []
    if target_spatial_reference is None:
        problems.append(FieldProblem("坐标系", "统一投影", "没有选择有效的统一坐标系"))
    for info in projection_infos:
        if info.spatial_reference is None:
            problems.append(FieldProblem("坐标系", source_label(info.source), info.message))

    problems.extend(audit_third_result_schema(result_source))
    block_bindings, block_problems = build_land_block_bindings(block_source)
    result_bindings, result_problems = build_result_bindings(result_source)
    admin_binding, admin_problems = build_admin_binding(admin_source)
    problems.extend(block_problems)
    problems.extend(result_problems)
    problems.extend(admin_problems)
    block_cropland_count = 0
    if not block_problems:
        try:
            block_cropland_count = count_cropland_features(block_source, binding_field(block_bindings, LAND_CLASS_FIELD))
        except Exception as exc:
            problems.append(FieldProblem(LAND_CLASS_FIELD, "最新地类图斑", f"耕地图斑筛选统计失败：{exc}"))
    if not block_problems and block_cropland_count <= 0:
        problems.append(FieldProblem(LAND_CLASS_FIELD, "最新地类图斑", "没有找到地类编码为 0101、0102 或 0103 的耕地图斑"))
    if nearest_mode == NEAREST_SAME_LAND_CLASS:
        warnings.append("选择“最近相同地类号”时，如果某个地类号在第三步结果中没有任何有值要素，对应输出要素会被打回。")

    expected_result_fields = len({LAND_CLASS_FIELD, COUNTY_NAME_FIELD, COUNTY_CODE_FIELD, *EVALUATION_FIELD_NAMES})
    ok = (
        not problems
        and len(block_bindings) == len(LAND_BLOCK_FIELD_SPECS)
        and len(result_bindings) == expected_result_fields
        and admin_binding is not None
    )
    draft = TransferPreflightReport(
        ok=ok,
        result_source=result_source,
        block_source=block_source,
        admin_source=admin_source,
        result_feature_count=result_count,
        block_feature_count=block_count,
        block_cropland_count=block_cropland_count,
        admin_feature_count=admin_count,
        projection_infos=projection_infos,
        projections_same=projections_same,
        target_projection_source=target_projection_source,
        target_spatial_reference=target_spatial_reference,
        block_bindings=block_bindings,
        result_bindings=result_bindings,
        admin_binding=admin_binding,
        nearest_mode=nearest_mode,
        problems=problems,
        warnings=warnings,
        text="",
    )
    return TransferPreflightReport(
        ok=draft.ok,
        result_source=draft.result_source,
        block_source=draft.block_source,
        admin_source=draft.admin_source,
        result_feature_count=draft.result_feature_count,
        block_feature_count=draft.block_feature_count,
        block_cropland_count=draft.block_cropland_count,
        admin_feature_count=draft.admin_feature_count,
        projection_infos=draft.projection_infos,
        projections_same=draft.projections_same,
        target_projection_source=draft.target_projection_source,
        target_spatial_reference=draft.target_spatial_reference,
        block_bindings=draft.block_bindings,
        result_bindings=draft.result_bindings,
        admin_binding=draft.admin_binding,
        nearest_mode=draft.nearest_mode,
        problems=draft.problems,
        warnings=draft.warnings,
        text=build_preflight_text(draft),
    )


def prepare_source_for_analysis(
    source: VectorSource,
    target_spatial_reference: object,
    temp_dir: Path,
    gdb_name: str,
    feature_name: str,
    logger: logging.Logger,
) -> str:
    source_dataset = source_dataset_path(source)
    gdb_path = score_tool.create_temp_gdb(temp_dir, gdb_name)
    out_fc = str(gdb_path / feature_name)
    info = read_source_spatial_reference(source)
    if info.spatial_reference is None:
        raise RuntimeError(f"{source_label(source)} 缺少可识别投影。")
    if spatial_reference_equal(info.spatial_reference, target_spatial_reference):
        logger.info("%s 投影一致，复制到临时分析图层。", source_label(source))
        arcpy.conversion.ExportFeatures(source_dataset, out_fc)
    else:
        logger.info("%s 重投影到临时分析图层。", source_label(source))
        arcpy.management.Project(source_dataset, out_fc, target_spatial_reference)
    return out_fc


def geometry_area_hectares(geometry) -> float | None:
    if geometry is None:
        return None
    try:
        return float(geometry.getArea("GEODESIC", "HECTARES"))
    except Exception:
        try:
            return float(geometry.area or 0.0) / 10000.0
        except Exception:
            return None


def geometry_length_km(geometry) -> float | None:
    if geometry is None:
        return None
    try:
        return float(geometry.getLength("GEODESIC", "KILOMETERS"))
    except Exception:
        try:
            return float(geometry.length or 0.0) / 1000.0
        except Exception:
            return None


def create_land_block_output(job: TransferJob, temp_dir: Path, logger: logging.Logger) -> tuple[str, dict[str, str]]:
    if job.output_kind != "gdb":
        raise RuntimeError("第四步需要保留完整中文字段名和固定字段顺序，请输出到 GDB 面要素类。")
    if not job.output_feature_name:
        raise RuntimeError("输出到 GDB 时必须指定面要素类名称。")
    delete_output_dataset(job.output_path, job.output_feature_name)
    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    if not job.output_path.exists():
        arcpy.management.CreateFileGDB(str(job.output_path.parent), job.output_path.name)

    arcpy.management.CreateFeatureclass(
        str(job.output_path),
        job.output_feature_name,
        "POLYGON",
        spatial_reference=job.target_spatial_reference,
    )
    output_fc = str(job.output_path / job.output_feature_name)
    source_types = {binding.output_field: binding.source_type for binding in job.block_bindings}
    source_types.update({binding.output_field: binding.source_type for binding in job.result_bindings})
    output_field_map = score_tool.add_ordered_output_fields(output_fc, source_types)
    score_tool.audit_output_schema(output_fc)

    block_analysis_fc = prepare_source_for_analysis(
        job.block_source,
        job.target_spatial_reference,
        temp_dir,
        "block_for_output",
        "block_for_output",
        logger,
    )
    land_class_source_field = score_tool.field_name_case_insensitive(block_analysis_fc, binding_field(job.block_bindings, LAND_CLASS_FIELD))
    kept_count, _removed_count = filter_cropland_blocks(block_analysis_fc, land_class_source_field, logger)
    if kept_count <= 0:
        raise RuntimeError("最新地类图斑中没有地类编码为 0101、0102 或 0103 的耕地图斑，已中断。")
    block_fields = [binding.source_field for binding in job.block_bindings]
    insert_fields = [
        "SHAPE@",
        *[output_field_map[binding.output_field] for binding in job.block_bindings],
        output_field_map[AREA_FIELD],
        output_field_map[LENGTH_FIELD],
        output_field_map[ENTITY_TYPE_FIELD],
        output_field_map[BALANCED_AREA_FIELD],
    ]
    source_output_types = {field.name: field for field in arcpy.ListFields(output_fc)}
    copied = 0
    with arcpy.da.InsertCursor(output_fc, insert_fields) as insert_cursor:
        with arcpy.da.SearchCursor(block_analysis_fc, ["SHAPE@", *block_fields]) as search_cursor:
            for row in search_cursor:
                geometry = row[0]
                copied_values = [
                    score_tool.coerce_value_for_field(value, source_output_types[output_field_map[binding.output_field]])
                    for value, binding in zip(row[1:], job.block_bindings)
                ]
                insert_cursor.insertRow(
                    [
                        geometry,
                        *copied_values,
                        geometry_area_hectares(geometry),
                        geometry_length_km(geometry),
                        "面",
                        None,
                    ]
                )
                copied += 1
    logger.info("输出初始图层创建完成：%s；写入最新耕地图斑要素数：%s", output_fc, copied)
    return output_fc, output_field_map


def binding_field(bindings: list[FieldBinding], output_name: str) -> str:
    for binding in bindings:
        if binding.output_field == output_name:
            return binding.source_field
    raise RuntimeError(f"字段绑定不存在：{output_name}")


def most_common_nonblank_value(feature_class: str, field_name: str) -> tuple[object, int, int]:
    counts: Counter[str] = Counter()
    values: dict[str, object] = {}
    total_nonblank = 0
    with arcpy.da.SearchCursor(feature_class, [field_name]) as cursor:
        for (value,) in cursor:
            if membership_tool.is_blank_value(value):
                continue
            key = str(value).strip()
            counts[key] += 1
            values.setdefault(key, value)
            total_nonblank += 1
    if not counts:
        raise RuntimeError(f"第三步结果字段 {field_name} 没有非空值，无法为输出统一赋值。")
    key, count = counts.most_common(1)[0]
    return values[key], count, total_nonblank


def build_valid_result_ids(feature_class: str, id_field: str, evaluation_fields: list[str]) -> set[int]:
    valid: set[int] = set()
    with arcpy.da.SearchCursor(feature_class, [id_field, *evaluation_fields]) as cursor:
        for row in cursor:
            result_id = row[0]
            if result_id is None:
                continue
            if all(not membership_tool.is_blank_value(value) for value in row[1:]):
                valid.add(int(result_id))
    return valid


def build_single_field_value_map(feature_class: str, id_field: str, value_field: str) -> dict[int, object]:
    values: dict[int, object] = {}
    with arcpy.da.SearchCursor(feature_class, [id_field, value_field]) as cursor:
        for row_id, value in cursor:
            if row_id is not None:
                values[int(row_id)] = value
    return values


def decide_quality_sources(
    total_areas: dict[int, float],
    overlaps: dict[int, dict[int, float]],
    valid_result_ids: set[int],
) -> tuple[dict[int, int], set[int], dict[str, int]]:
    decisions: dict[int, int] = {}
    unresolved: set[int] = set()
    stats = {
        "total": len(total_areas),
        "overlay_decisions": 0,
        "nearest_needed": 0,
        "no_valid_overlap": 0,
        "no_data_dominates": 0,
        "result_tie": 0,
        "zero_area": 0,
    }
    for target_id, total_area in total_areas.items():
        if total_area <= 0:
            stats["zero_area"] += 1
            unresolved.add(target_id)
            continue
        valid_overlaps = {
            result_id: area
            for result_id, area in overlaps.get(target_id, {}).items()
            if result_id in valid_result_ids and area > 0
        }
        if not valid_overlaps:
            stats["no_valid_overlap"] += 1
            unresolved.add(target_id)
            continue
        result_id, max_area = max(valid_overlaps.items(), key=lambda item: item[1])
        valid_area = sum(max(0.0, area) for area in valid_overlaps.values())
        no_data_area = max(0.0, total_area - valid_area)
        epsilon = max(total_area * 1e-9, 1e-9)
        if max_area <= no_data_area + epsilon:
            stats["no_data_dominates"] += 1
            unresolved.add(target_id)
            continue
        tied_results = [candidate_id for candidate_id, area in valid_overlaps.items() if abs(area - max_area) <= epsilon]
        if len(tied_results) > 1:
            stats["result_tie"] += 1
            unresolved.add(target_id)
            continue
        decisions[target_id] = result_id
        stats["overlay_decisions"] += 1
    stats["nearest_needed"] = len(unresolved)
    return decisions, unresolved, stats


def create_id_subset(source_fc: str, id_field: str, keep_ids: set[int], temp_dir: Path, gdb_name: str, feature_name: str) -> str:
    out_fc = score_tool.copy_feature_to_temp(source_fc, temp_dir, gdb_name, feature_name)
    with arcpy.da.UpdateCursor(out_fc, [id_field]) as cursor:
        for row in cursor:
            row_id = row[0]
            if row_id is None or int(row_id) not in keep_ids:
                cursor.deleteRow()
    return out_fc


def field_info(feature_class: str, field_name: str):
    wanted = field_name.upper()
    for field in arcpy.ListFields(feature_class):
        if field.name.upper() == wanted:
            return field
    raise RuntimeError(f"字段不存在：{field_name}")


def sql_literal(value: object, field_type: str) -> str:
    if value is None:
        return "NULL"
    if field_type == "String":
        return "'" + str(value).replace("'", "''") + "'"
    try:
        return str(float(value))
    except Exception:
        return "'" + str(value).replace("'", "''") + "'"


def sql_equals(feature_class: str, field_name: str, value: object) -> str:
    field = field_info(feature_class, field_name)
    delimited = arcpy.AddFieldDelimiters(feature_class, field.name)
    if value is None:
        return f"{delimited} IS NULL"
    return f"{delimited} = {sql_literal(value, field.type)}"


def make_feature_layer(feature_class: str, name: str, where_clause: str | None = None) -> str:
    layer_name = f"{name}_{uuid.uuid4().hex[:8]}"
    arcpy.management.MakeFeatureLayer(feature_class, layer_name, where_clause or "")
    return layer_name


def generate_nearest_decisions(
    input_fc: str,
    near_fc: str,
    input_id_field: str,
    near_id_field: str,
    temp_dir: Path,
    table_name: str,
    logger: logging.Logger,
) -> dict[int, int]:
    in_count = int(arcpy.management.GetCount(input_fc)[0])
    near_count = int(arcpy.management.GetCount(near_fc)[0])
    if in_count <= 0 or near_count <= 0:
        return {}
    in_oid = membership_tool.oid_field_name(input_fc)
    near_oid = membership_tool.oid_field_name(near_fc)
    input_oid_to_id = {
        int(row[0]): int(row[1])
        for row in arcpy.da.SearchCursor(input_fc, [in_oid, input_id_field])
        if row[1] is not None
    }
    near_oid_to_id = {
        int(row[0]): int(row[1])
        for row in arcpy.da.SearchCursor(near_fc, [near_oid, near_id_field])
        if row[1] is not None
    }
    gdb_path = score_tool.create_temp_gdb(temp_dir, table_name)
    near_table = str(gdb_path / "near_table")
    logger.info("开始最近距离匹配：输入 %s 个，候选 %s 个。", in_count, near_count)
    arcpy.analysis.GenerateNearTable(input_fc, near_fc, near_table, "", "NO_LOCATION", "NO_ANGLE", "CLOSEST", 1, "PLANAR")
    decisions: dict[int, int] = {}
    with arcpy.da.SearchCursor(near_table, ["IN_FID", "NEAR_FID"]) as cursor:
        for in_fid, near_fid in cursor:
            target_id = input_oid_to_id.get(int(in_fid))
            result_id = near_oid_to_id.get(int(near_fid))
            if target_id is not None and result_id is not None:
                decisions[target_id] = result_id
    return decisions


def generate_nearest_by_class_decisions(
    input_fc: str,
    near_fc: str,
    input_id_field: str,
    near_id_field: str,
    input_class_field: str,
    near_class_field: str,
    temp_dir: Path,
    logger: logging.Logger,
) -> tuple[dict[int, int], set[int]]:
    class_to_target_ids: dict[str, set[int]] = {}
    class_original_value: dict[str, object] = {}
    with arcpy.da.SearchCursor(input_fc, [input_id_field, input_class_field]) as cursor:
        for target_id, class_value in cursor:
            if target_id is None:
                continue
            class_key = str(class_value).strip() if class_value is not None else "<NULL>"
            class_to_target_ids.setdefault(class_key, set()).add(int(target_id))
            class_original_value.setdefault(class_key, class_value)
    decisions: dict[int, int] = {}
    unresolved: set[int] = set()
    for class_key, target_ids in class_to_target_ids.items():
        class_value = class_original_value[class_key]
        target_where = sql_equals(input_fc, input_class_field, class_value)
        near_where = sql_equals(near_fc, near_class_field, class_value)
        target_layer = make_feature_layer(input_fc, "target_same_class", target_where)
        near_layer = make_feature_layer(near_fc, "result_same_class", near_where)
        try:
            if int(arcpy.management.GetCount(near_layer)[0]) <= 0:
                unresolved.update(target_ids)
                logger.warning("地类号 %s 在第三步有值结果中没有候选要素。", class_value)
                continue
            partial = generate_nearest_decisions(
                target_layer,
                near_layer,
                input_id_field,
                near_id_field,
                temp_dir,
                f"near_same_class_{uuid.uuid4().hex[:6]}",
                logger,
            )
            decisions.update(partial)
            unresolved.update(target_ids - set(partial))
        finally:
            arcpy.management.Delete(target_layer)
            arcpy.management.Delete(near_layer)
    return decisions, unresolved


def nearest_quality_decisions(
    target_analysis_fc: str,
    result_analysis_fc: str,
    target_id_field: str,
    result_id_field: str,
    target_class_field: str,
    result_class_field: str,
    unresolved_target_ids: set[int],
    valid_result_ids: set[int],
    nearest_mode: int,
    temp_dir: Path,
    logger: logging.Logger,
) -> tuple[dict[int, int], set[int]]:
    if not unresolved_target_ids:
        return {}, set()
    target_subset = create_id_subset(target_analysis_fc, target_id_field, unresolved_target_ids, temp_dir, "nearest_targets", "nearest_targets")
    valid_result_subset = create_id_subset(result_analysis_fc, result_id_field, valid_result_ids, temp_dir, "valid_results", "valid_results")
    if int(arcpy.management.GetCount(valid_result_subset)[0]) <= 0:
        return {}, set(unresolved_target_ids)
    if nearest_mode == NEAREST_SAME_LAND_CLASS:
        return generate_nearest_by_class_decisions(
            target_subset,
            valid_result_subset,
            target_id_field,
            result_id_field,
            target_class_field,
            result_class_field,
            temp_dir,
            logger,
        )
    decisions = generate_nearest_decisions(
        target_subset,
        valid_result_subset,
        target_id_field,
        result_id_field,
        temp_dir,
        "nearest_quality",
        logger,
    )
    return decisions, unresolved_target_ids - set(decisions)


def decide_admin_sources(
    total_areas: dict[int, float],
    overlaps: dict[int, dict[int, float]],
) -> tuple[dict[int, int], set[int]]:
    decisions: dict[int, int] = {}
    unresolved: set[int] = set()
    for target_id in total_areas:
        by_admin = {admin_id: area for admin_id, area in overlaps.get(target_id, {}).items() if area > 0}
        if not by_admin:
            unresolved.add(target_id)
            continue
        admin_id, _ = max(by_admin.items(), key=lambda item: item[1])
        decisions[target_id] = admin_id
    return decisions, unresolved


def update_constant_fields(output_fc: str, values: dict[str, object]) -> None:
    fields_by_name = {field.name: field for field in arcpy.ListFields(output_fc)}
    field_names = list(values)
    with arcpy.da.UpdateCursor(output_fc, field_names) as cursor:
        for row in cursor:
            for index, name in enumerate(field_names):
                row[index] = score_tool.coerce_value_for_field(values[name], fields_by_name[name])
            cursor.updateRow(row)


def update_value_by_decision(
    output_fc: str,
    target_id_field: str,
    target_field: str,
    decisions: dict[int, int],
    source_values: dict[int, object],
) -> int:
    fields_by_name = {field.name: field for field in arcpy.ListFields(output_fc)}
    updated = 0
    with arcpy.da.UpdateCursor(output_fc, [target_id_field, target_field]) as cursor:
        for row in cursor:
            target_id = int(row[0])
            source_id = decisions.get(target_id)
            if source_id is None:
                continue
            row[1] = score_tool.coerce_value_for_field(source_values.get(source_id), fields_by_name[target_field])
            cursor.updateRow(row)
            updated += 1
    return updated


def update_quality_fields(
    output_fc: str,
    target_id_field: str,
    quality_fields: list[str],
    decisions: dict[int, int],
    result_values: dict[int, tuple],
    logger: logging.Logger,
) -> int:
    output_bindings = [
        score_tool.ScoreFieldBinding(field, field, field_info(output_fc, field).type, field, field_info(output_fc, field).type)
        for field in quality_fields
    ]
    return score_tool.update_output_fields(output_fc, target_id_field, output_bindings, decisions, result_values, logger)


def build_missing_decision_report(output_fc: str, target_id_field: str, missing_ids: set[int], title: str) -> str:
    sample_fields = [
        field
        for field in membership_tool.first_data_field_names(output_fc)
        if field.upper() != target_id_field.upper()
    ]
    lines = [title, "=" * 60, f"缺少来源的要素数：{len(missing_ids)}"]
    detail_limit = membership_tool.ISSUE_DETAIL_LIMIT
    if missing_ids:
        lines.append("")
        lines.append("要素级样例：")
    shown = 0
    with arcpy.da.SearchCursor(output_fc, [target_id_field, *sample_fields]) as cursor:
        for row in cursor:
            target_id = int(row[0])
            if target_id not in missing_ids:
                continue
            if shown >= detail_limit:
                break
            sample_text = (
                "；".join(f"{name}={membership_tool.field_value_text(value)}" for name, value in zip(sample_fields, row[1:]))
                or "无可展示字段值"
            )
            lines.append(f"   - OID={target_id}；{sample_text}")
            shown += 1
    if len(missing_ids) > detail_limit:
        lines.append(f"问题要素过多（{len(missing_ids)} 个），只输出前 {detail_limit} 个样例。")
    return "\n".join(lines)


def calculate_transfer(job: TransferJob, temp_dir: Path, logger: logging.Logger) -> tuple[str, dict[str, int]]:
    require_runtime()
    arcpy.env.overwriteOutput = True
    output_fc, output_field_map = create_land_block_output(job, temp_dir, logger)
    output_id_field = score_tool.add_oid_copy_field(output_fc, "CQ_BLOCK_OID")
    stats: dict[str, int] | None = None
    try:
        target_analysis_fc = score_tool.copy_feature_to_temp(output_fc, temp_dir, "target_analysis", "target_analysis")
        target_id_field = score_tool.field_name_case_insensitive(target_analysis_fc, output_id_field)
        result_analysis_fc = prepare_source_for_analysis(
            job.result_source,
            job.target_spatial_reference,
            temp_dir,
            "third_result_analysis",
            "third_result_analysis",
            logger,
        )
        result_id_field = score_tool.add_oid_copy_field(result_analysis_fc, "CQ_RESULT_OID")
        admin_analysis_fc = prepare_source_for_analysis(
            job.admin_source,
            job.target_spatial_reference,
            temp_dir,
            "admin_analysis",
            "admin_analysis",
            logger,
        )
        admin_id_field = score_tool.add_oid_copy_field(admin_analysis_fc, "CQ_ADMIN_OID")

        result_field_map = {binding.output_field: score_tool.field_name_case_insensitive(result_analysis_fc, binding.source_field) for binding in job.result_bindings}
        admin_name_field = score_tool.field_name_case_insensitive(admin_analysis_fc, job.admin_binding.source_field)
        county_name, county_name_count, county_name_total = most_common_nonblank_value(result_analysis_fc, result_field_map[COUNTY_NAME_FIELD])
        county_code, county_code_count, county_code_total = most_common_nonblank_value(result_analysis_fc, result_field_map[COUNTY_CODE_FIELD])
        update_constant_fields(output_fc, {COUNTY_NAME_FIELD: county_name, COUNTY_CODE_FIELD: county_code})
        logger.info(
            "县名称/县代码统一赋值：%s(%s/%s)，%s(%s/%s)",
            county_name,
            county_name_count,
            county_name_total,
            county_code,
            county_code_count,
            county_code_total,
        )

        total_areas = score_tool.build_target_area_map(target_analysis_fc, target_id_field)
        quality_result_fields = [result_field_map[field] for field in EVALUATION_FIELD_NAMES]
        result_values = score_tool.build_result_value_map(result_analysis_fc, result_id_field, quality_result_fields)
        valid_result_ids = build_valid_result_ids(result_analysis_fc, result_id_field, quality_result_fields)
        result_class_field = result_field_map[LAND_CLASS_FIELD]
        target_class_field = score_tool.field_name_case_insensitive(target_analysis_fc, output_field_map[LAND_CLASS_FIELD])

        quality_overlaps = score_tool.intersect_overlap_areas(
            target_analysis_fc,
            result_analysis_fc,
            target_id_field,
            result_id_field,
            temp_dir,
            logger,
        )
        quality_decisions, nearest_needed, quality_stats = decide_quality_sources(total_areas, quality_overlaps, valid_result_ids)
        nearest_decisions, still_missing_quality = nearest_quality_decisions(
            target_analysis_fc,
            result_analysis_fc,
            target_id_field,
            result_id_field,
            target_class_field,
            result_class_field,
            nearest_needed,
            valid_result_ids,
            job.nearest_mode,
            temp_dir,
            logger,
        )
        quality_decisions.update(nearest_decisions)
        if still_missing_quality:
            report_text = build_missing_decision_report(output_fc, output_id_field, still_missing_quality, "耕评字段最近补值失败报告")
            logger.error(report_text)
            raise RuntimeError(f"有 {len(still_missing_quality)} 个要素没有找到可用第三步结果。详情见日志。")

        quality_updated = update_quality_fields(output_fc, output_id_field, EVALUATION_FIELD_NAMES, quality_decisions, result_values, logger)
        output_bindings = [
            score_tool.ScoreFieldBinding(field, field, field_info(output_fc, field).type, result_field_map[field], field_info(output_fc, field).type)
            for field in EVALUATION_FIELD_NAMES
        ]
        ok, audit_text, audit_stats = score_tool.build_update_match_report(
            output_fc,
            output_id_field,
            output_bindings,
            quality_decisions,
            result_values,
            "第四步耕评字段更新后一致性审计",
        )
        logger.info("耕评字段一致性审计：\n%s", audit_text)
        if not ok:
            raise RuntimeError(f"耕评字段更新后仍有不一致或空值：{audit_stats['issue_values']} 个。详情见日志。")

        admin_name_values = build_single_field_value_map(admin_analysis_fc, admin_id_field, admin_name_field)
        admin_overlaps = score_tool.intersect_overlap_areas(
            target_analysis_fc,
            admin_analysis_fc,
            target_id_field,
            admin_id_field,
            temp_dir,
            logger,
        )
        admin_decisions, missing_admin = decide_admin_sources(total_areas, admin_overlaps)
        admin_nearest_count = 0
        if missing_admin:
            target_subset = create_id_subset(target_analysis_fc, target_id_field, missing_admin, temp_dir, "admin_nearest_targets", "admin_nearest_targets")
            admin_nearest = generate_nearest_decisions(
                target_subset,
                admin_analysis_fc,
                target_id_field,
                admin_id_field,
                temp_dir,
                "nearest_admin",
                logger,
            )
            admin_nearest_count = len(admin_nearest)
            admin_decisions.update(admin_nearest)
            missing_admin = missing_admin - set(admin_nearest)
        if missing_admin:
            report_text = build_missing_decision_report(output_fc, output_id_field, missing_admin, "乡名称行政区匹配失败报告")
            logger.error(report_text)
            raise RuntimeError(f"有 {len(missing_admin)} 个要素没有找到行政区名称。详情见日志。")
        township_updated = update_value_by_decision(output_fc, output_id_field, TOWNSHIP_NAME_FIELD, admin_decisions, admin_name_values)

        blank_ok, blank_text, blank_stats = score_tool.build_blank_result_report(
            output_fc,
            EVALUATION_FIELD_NAMES,
            "第四步耕评字段空值审计",
        )
        logger.info("耕评字段空值审计：\n%s", blank_text)
        if not blank_ok:
            raise RuntimeError(f"第四步输出耕评字段仍有空值：{blank_stats['missing_values']} 个。详情见日志。")

        stats = {
            **quality_stats,
            "nearest_decisions": len(nearest_decisions),
            "quality_updated": quality_updated,
            "township_updated": township_updated,
            "admin_nearest_fallback": admin_nearest_count,
            "audit_checked_features": audit_stats["checked_features"],
            "audit_issue_values": audit_stats["issue_values"],
            "blank_missing_values": blank_stats["missing_values"],
        }
        logger.info("第四步更新统计：%s", stats)
    finally:
        try:
            arcpy.management.DeleteField(output_fc, output_id_field)
            logger.info("已删除输出临时 ID 字段：%s", output_id_field)
        except Exception as exc:
            logger.warning("输出临时 ID 字段删除失败：%s", exc)
    score_tool.audit_output_schema(output_fc)
    logger.info("输出字段结构审计通过。")
    logger.info("输出完成：%s", output_fc)
    if stats is None:
        raise RuntimeError("更新统计未生成，任务状态异常。")
    return output_fc, stats


def setup_job_logger(logs_dir: Path, job_id: str) -> tuple[logging.Logger, Path]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"update_land_blocks_{timestamp_for_file()}_{job_id}.log"
    logger = logging.getLogger(f"update_land_blocks_arcpy.{job_id}")
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


class TransferWorker(threading.Thread):
    def __init__(self, job_queue: queue.Queue, event_queue: queue.Queue, logs_dir: Path, process_dir: Path):
        super().__init__(daemon=True)
        self.job_queue = job_queue
        self.event_queue = event_queue
        self.logs_dir = logs_dir
        self.process_dir = process_dir
        self.history_path = logs_dir / "update_land_blocks_history.jsonl"

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

    def process_job(self, job: TransferJob) -> None:
        logger, log_path = setup_job_logger(self.logs_dir, job.job_id)
        temp_dir = self.process_dir / f"update_land_blocks_{timestamp_for_file()}_{job.job_id}"
        output_target = output_target_path(job.output_path, job.output_feature_name, job.output_kind)
        record = {
            "job_id": job.job_id,
            "created_at": job.created_at,
            "started_at": now_text(),
            "ended_at": None,
            "status": "running",
            "result_source": source_path_for_log(job.result_source),
            "block_source": source_path_for_log(job.block_source),
            "admin_source": source_path_for_log(job.admin_source),
            "output_path": output_target,
            "target_projection_source": source_path_for_log(job.target_projection_source),
            "target_projection": describe_spatial_reference(job.target_spatial_reference),
            "nearest_mode": nearest_mode_text(job.nearest_mode),
            "block_bindings": [binding.__dict__ for binding in job.block_bindings],
            "result_bindings": [binding.__dict__ for binding in job.result_bindings],
            "admin_binding": job.admin_binding.__dict__,
            "validation_report": job.validation_report,
            "log_path": str(log_path),
            "error": None,
            "stats": None,
        }
        self.send("job_started", {"job_id": job.job_id, "message": "开始更新最新耕地图斑", "log_path": str(log_path)})
        try:
            require_runtime()
            temp_dir.mkdir(parents=True, exist_ok=True)
            logger.info("任务开始：%s", job.job_id)
            logger.info("第三步结果：%s", source_path_for_log(job.result_source))
            logger.info("最新地类图斑：%s", source_path_for_log(job.block_source))
            logger.info("最新行政区：%s", source_path_for_log(job.admin_source))
            logger.info("输出目标：%s", output_target)
            logger.info("统一投影：%s", describe_spatial_reference(job.target_spatial_reference))
            logger.info("最近补值模式：%s", nearest_mode_text(job.nearest_mode))
            if job.validation_report:
                logger.info("提交前审查报告：\n%s", job.validation_report)
            output_fc, stats = calculate_transfer(job, temp_dir, logger)
            record.update({"status": "success", "stats": stats})
            self.send(
                "job_done",
                {
                    "job_id": job.job_id,
                    "message": f"更新完成：{output_fc}；耕评字段更新要素 {stats.get('quality_updated', 0)} 个",
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


class LandBlockUpdateApp:
    def __init__(self, root: Tk):
        self.root = root
        self.paths = resolve_paths(Path.cwd())
        self.logs_dir = self.paths.outputs_dir / "logs"
        self.process_dir = self.paths.outputs_dir / "process_files"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.process_dir.mkdir(parents=True, exist_ok=True)

        self.result_path_var = StringVar()
        self.result_layer_var = StringVar()
        self.result_source: VectorSource | None = None
        self.result_gdb_sources: list[VectorSource] = []

        self.block_path_var = StringVar()
        self.block_layer_var = StringVar()
        self.block_source: VectorSource | None = None
        self.block_gdb_sources: list[VectorSource] = []

        self.admin_path_var = StringVar()
        self.admin_layer_var = StringVar()
        self.admin_source: VectorSource | None = None
        self.admin_gdb_sources: list[VectorSource] = []

        self.reference_mode = IntVar(value=1)
        self.reference_extra_var = StringVar()
        self.target_spatial_reference = None
        self.target_projection_source: VectorSource | None = None
        self.nearest_mode_var = IntVar(value=NEAREST_ALL)

        self.output_gdb_var = StringVar()
        self.output_feature_var = StringVar(value="最新耕地图斑_质量评价")

        self.last_report: TransferPreflightReport | None = None
        self.last_report_key: tuple | None = None

        self.job_queue: queue.Queue = queue.Queue()
        self.event_queue: queue.Queue = queue.Queue()
        self.worker = TransferWorker(self.job_queue, self.event_queue, self.logs_dir, self.process_dir)
        self.worker.start()

        self.root.title("第四步：最新耕地图斑质量评价更新工具（ArcPy）")
        self.root.geometry("1180x860")
        self.build_ui()
        self.root.after(200, self.poll_worker_events)

    def build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill=BOTH, expand=True)

        input_frame = ttk.LabelFrame(container, text="1. 输入数据")
        input_frame.pack(fill="x", pady=5)
        self.build_source_rows(input_frame, "第三步结果", self.result_path_var, self.result_layer_var, "result")
        self.build_source_rows(input_frame, "最新地类图斑", self.block_path_var, self.block_layer_var, "block")
        self.build_source_rows(input_frame, "最新行政区", self.admin_path_var, self.admin_layer_var, "admin")

        projection_frame = ttk.LabelFrame(container, text="2. 坐标系统一")
        projection_frame.pack(fill="x", pady=5)
        projection_row = ttk.Frame(projection_frame)
        projection_row.pack(fill="x", padx=5, pady=4)
        ttk.Radiobutton(projection_row, text="使用第三步结果投影", variable=self.reference_mode, value=0, command=self.update_target_projection).pack(side=LEFT)
        ttk.Radiobutton(projection_row, text="使用最新地类图斑投影", variable=self.reference_mode, value=1, command=self.update_target_projection).pack(side=LEFT, padx=8)
        ttk.Radiobutton(projection_row, text="使用最新行政区投影", variable=self.reference_mode, value=2, command=self.update_target_projection).pack(side=LEFT, padx=8)
        ttk.Radiobutton(projection_row, text="使用外部 shp 投影", variable=self.reference_mode, value=3, command=self.update_target_projection).pack(side=LEFT, padx=8)
        ttk.Button(projection_row, text="选择外部 shp", command=self.choose_extra_reference).pack(side=LEFT, padx=5)
        ttk.Entry(projection_row, textvariable=self.reference_extra_var).pack(side=LEFT, fill="x", expand=True, padx=5)

        self.projection_tree = ttk.Treeview(projection_frame, columns=("source", "projection"), show="headings", height=4)
        self.projection_tree.heading("source", text="数据源")
        self.projection_tree.heading("projection", text="投影")
        self.projection_tree.column("source", width=420, anchor="w")
        self.projection_tree.column("projection", width=680, anchor="w")
        self.projection_tree.pack(fill="x", padx=5, pady=4)

        self.projection_text = Text(projection_frame, height=4, wrap="word")
        self.projection_text.pack(fill="x", padx=5, pady=4)
        self.projection_text.insert(END, "选择输入数据后，这里会显示统一投影。")

        rule_frame = ttk.LabelFrame(container, text="3. 无值区补值方式")
        rule_frame.pack(fill="x", pady=5)
        ttk.Radiobutton(rule_frame, text="找最近的有值第三步结果", variable=self.nearest_mode_var, value=NEAREST_ALL).pack(side=LEFT, padx=8, pady=5)
        ttk.Radiobutton(rule_frame, text="找最近相同地类号的有值第三步结果", variable=self.nearest_mode_var, value=NEAREST_SAME_LAND_CLASS).pack(side=LEFT, padx=8, pady=5)

        output_frame = ttk.LabelFrame(container, text="4. 输出位置")
        output_frame.pack(fill="x", pady=5)
        kind_row = ttk.Frame(output_frame)
        kind_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(kind_row, text="第四步固定输出为 GDB 面要素类，以保留完整中文字段名和字段顺序。").pack(side=LEFT)
        gdb_row = ttk.Frame(output_frame)
        gdb_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(gdb_row, text="GDB").pack(side=LEFT)
        ttk.Entry(gdb_row, textvariable=self.output_gdb_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(gdb_row, text="选择已有 gdb", command=self.choose_existing_output_gdb).pack(side=LEFT, padx=3)
        ttk.Button(gdb_row, text="新建 gdb", command=self.choose_output_gdb).pack(side=LEFT, padx=3)
        ttk.Label(gdb_row, text="面要素类名").pack(side=LEFT, padx=5)
        ttk.Entry(gdb_row, textvariable=self.output_feature_var, width=26).pack(side=LEFT)

        report_frame = ttk.LabelFrame(container, text="5. 审查报告、日志和历史记录")
        report_frame.pack(fill="both", expand=True, pady=5)
        action_row = ttk.Frame(report_frame)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="开始审查", command=self.validate_current_inputs).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="提交更新任务", command=self.submit_job).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="查看历史记录", command=self.show_history).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="打开日志文件夹", command=self.open_logs_folder).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="删除日志", command=self.delete_logs).pack(side=LEFT, padx=5, pady=4)
        self.status_text = Text(report_frame, height=22, wrap="word")
        self.status_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.log_status("工具已启动。请先选择第三步结果、最新地类图斑和最新行政区。")

    def build_source_rows(self, parent, label: str, path_var: StringVar, layer_var: StringVar, role: str) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=5, pady=4)
        ttk.Label(row, text=label, width=12).pack(side=LEFT)
        ttk.Entry(row, textvariable=path_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(row, text="选择 shp", command=lambda: self.choose_shp(role)).pack(side=LEFT, padx=3)
        ttk.Button(row, text="选择 gdb", command=lambda: self.choose_gdb(role)).pack(side=LEFT, padx=3)

        layer_row = ttk.Frame(parent)
        layer_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(layer_row, text=f"{label}图层", width=12).pack(side=LEFT)
        combo = ttk.Combobox(layer_row, textvariable=layer_var, state="readonly", width=96)
        combo.pack(side=LEFT, fill="x", expand=True, padx=5)
        combo.bind("<<ComboboxSelected>>", lambda _event: self.update_source_from_layer(role))
        if role == "result":
            self.result_layer_combo = combo
        elif role == "block":
            self.block_layer_combo = combo
        else:
            self.admin_layer_combo = combo

    def set_source(self, role: str, source: VectorSource) -> None:
        if role == "result":
            self.result_source = source
            self.result_path_var.set(str(source.source_path))
            self.result_layer_var.set(source.layer_name or "")
            self.result_gdb_sources = []
            self.result_layer_combo["values"] = []
            self.log_status(f"已选择第三步结果：{source_label(source)}")
        elif role == "block":
            self.block_source = source
            self.block_path_var.set(str(source.source_path))
            self.block_layer_var.set(source.layer_name or "")
            self.block_gdb_sources = []
            self.block_layer_combo["values"] = []
            self.log_status(f"已选择最新地类图斑：{source_label(source)}")
        else:
            self.admin_source = source
            self.admin_path_var.set(str(source.source_path))
            self.admin_layer_var.set(source.layer_name or "")
            self.admin_gdb_sources = []
            self.admin_layer_combo["values"] = []
            self.log_status(f"已选择最新行政区：{source_label(source)}")
        self.last_report = None
        self.update_target_projection()

    def choose_shp(self, role: str) -> None:
        path = filedialog.askopenfilename(title="选择 shp", filetypes=[("Shapefile", "*.shp")])
        if path:
            self.set_source(role, make_vector_source("shp", Path(path)))

    def choose_gdb(self, role: str) -> None:
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
        if role == "result":
            self.result_gdb_sources = sources
            self.result_path_var.set(str(gdb_path))
            self.result_layer_combo["values"] = [source.layer_name for source in sources]
            self.result_layer_var.set(str(sources[0].layer_name))
            self.result_source = sources[0]
        elif role == "block":
            self.block_gdb_sources = sources
            self.block_path_var.set(str(gdb_path))
            self.block_layer_combo["values"] = [source.layer_name for source in sources]
            self.block_layer_var.set(str(sources[0].layer_name))
            self.block_source = sources[0]
        else:
            self.admin_gdb_sources = sources
            self.admin_path_var.set(str(gdb_path))
            self.admin_layer_combo["values"] = [source.layer_name for source in sources]
            self.admin_layer_var.set(str(sources[0].layer_name))
            self.admin_source = sources[0]
        self.last_report = None
        self.log_status(f"已选择 {gdb_path}，面图层数量 {len(sources)}。")
        self.update_target_projection()

    def update_source_from_layer(self, role: str) -> None:
        if role == "result":
            layer_name = self.result_layer_var.get()
            sources = self.result_gdb_sources
        elif role == "block":
            layer_name = self.block_layer_var.get()
            sources = self.block_gdb_sources
        else:
            layer_name = self.admin_layer_var.get()
            sources = self.admin_gdb_sources
        for source in sources:
            if source.layer_name == layer_name:
                if role == "result":
                    self.result_source = source
                elif role == "block":
                    self.block_source = source
                else:
                    self.admin_source = source
                self.last_report = None
                self.log_status(f"已选择 GDB 面图层：{source_label(source)}")
                self.update_target_projection()
                return

    def choose_extra_reference(self) -> None:
        path = filedialog.askopenfilename(title="选择投影参考 shp", filetypes=[("Shapefile", "*.shp")])
        if path:
            self.reference_extra_var.set(path)
            self.reference_mode.set(3)
            self.update_target_projection()

    def update_target_projection(self) -> None:
        if hasattr(self, "projection_tree"):
            for item in self.projection_tree.get_children():
                self.projection_tree.delete(item)
        if hasattr(self, "projection_text"):
            self.projection_text.delete("1.0", END)
        self.target_projection_source = None
        self.target_spatial_reference = None

        sources = [source for source in (self.result_source, self.block_source, self.admin_source) if source is not None]
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
            reference_source = self.result_source
        elif self.reference_mode.get() == 1:
            reference_source = self.block_source
        elif self.reference_mode.get() == 2:
            reference_source = self.admin_source
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
            self.result_source,
            self.block_source,
            self.admin_source,
            self.reference_mode.get(),
            self.reference_extra_var.get(),
            source_label(self.target_projection_source) if self.target_projection_source else "",
            self.nearest_mode_var.get(),
        )

    def validate_current_inputs(self) -> None:
        if self.result_source is None:
            messagebox.showwarning("提示", "请先选择第三步结果。")
            return
        if self.block_source is None:
            messagebox.showwarning("提示", "请先选择最新地类图斑。")
            return
        if self.admin_source is None:
            messagebox.showwarning("提示", "请先选择最新行政区。")
            return
        self.update_target_projection()
        if self.target_projection_source is None or self.target_spatial_reference is None:
            messagebox.showwarning("提示", "请先选择有效的统一投影。")
            return
        try:
            self.log_status("开始审查坐标、字段匹配和固定输出字段结构。")
            report = build_preflight_report(
                self.result_source,
                self.block_source,
                self.admin_source,
                self.target_projection_source,
                self.target_spatial_reference,
                self.nearest_mode_var.get(),
            )
        except Exception as exc:
            messagebox.showerror("审查失败", str(exc))
            return
        self.last_report = report
        self.last_report_key = self.report_key()
        self.show_report(report, ask_continue=False)
        self.log_status("审查通过，可以提交更新任务。" if report.ok else "审查未通过，已在报告中列出问题。")

    def show_report(self, report: TransferPreflightReport, ask_continue: bool) -> bool:
        window = Toplevel(self.root)
        window.title("第四步更新前审查报告")
        window.geometry("1040x720")
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
            ttk.Button(button_row, text="确认无误，继续更新", command=confirm).pack(side=LEFT, padx=5)
            ttk.Button(button_row, text="取消", command=cancel).pack(side=LEFT, padx=5)
            window.transient(self.root)
            window.grab_set()
            self.root.wait_window(window)
            return result["confirmed"]
        ttk.Button(button_row, text="关闭", command=window.destroy).pack(side=LEFT, padx=5)
        return False

    def output_settings(self) -> tuple[str, Path, str | None] | None:
        gdb_text = self.output_gdb_var.get().strip()
        feature_name = self.output_feature_var.get().strip()
        if not gdb_text or not feature_name:
            messagebox.showwarning("提示", "请选择 GDB 并填写面要素类名。")
            return None
        output_path = Path(gdb_text if gdb_text.lower().endswith(".gdb") else f"{gdb_text}.gdb")
        output_feature_name = arcpy.ValidateTableName(feature_name, str(output_path.parent))
        return "gdb", output_path, output_feature_name

    def submit_job(self) -> None:
        if self.result_source is None or self.block_source is None or self.admin_source is None:
            messagebox.showwarning("提示", "请先选择三个输入文件。")
            return
        output = self.output_settings()
        if output is None:
            return
        output_kind, output_path, output_feature_name = output
        output_dataset = output_dataset_path(output_kind, output_path, output_feature_name)
        for label, source in (("第三步结果", self.result_source), ("最新地类图斑", self.block_source), ("最新行政区", self.admin_source)):
            input_dataset = Path(source_dataset_path(source)).resolve()
            if output_dataset.resolve() == input_dataset:
                messagebox.showerror("输出错误", f"输出结果不能覆盖{label}输入数据，请换一个输出名称。")
                return
            if source.kind == "gdb" and output_path.resolve() == source.source_path.resolve():
                self.log_status(f"输出将保存到{label}所在 GDB 的新图层：{output_feature_name}")
        try:
            report = self.last_report
            if report is None or self.last_report_key != self.report_key():
                self.log_status("当前输入没有最新审查报告，正在重新审查。")
                self.validate_current_inputs()
                report = self.last_report
            if report is None:
                return
            if not report.ok or report.admin_binding is None:
                self.show_report(report, ask_continue=False)
                messagebox.showerror("审查未通过", "输入数据存在问题，不能更新。请先按报告修正。")
                return
        except Exception as exc:
            messagebox.showerror("提交失败", str(exc))
            return
        if not self.show_report(report, ask_continue=True):
            self.log_status("用户取消更新任务，未提交。")
            return
        job = TransferJob(
            job_id=uuid.uuid4().hex[:8],
            result_source=self.result_source,
            block_source=self.block_source,
            admin_source=self.admin_source,
            output_path=output_path,
            output_feature_name=output_feature_name,
            output_kind=output_kind,
            target_projection_source=report.target_projection_source,
            target_spatial_reference=report.target_spatial_reference,
            block_bindings=report.block_bindings,
            result_bindings=report.result_bindings,
            admin_binding=report.admin_binding,
            nearest_mode=report.nearest_mode,
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
                if event_type in {"job_done", "job_failed"} and payload.get("log_path"):
                    self.log_status(f"日志：{payload['log_path']}")
        except queue.Empty:
            pass
        self.root.after(200, self.poll_worker_events)

    def log_status(self, message: str) -> None:
        self.status_text.insert(END, f"{now_text()}  {message}\n")
        self.status_text.see(END)

    def show_history(self) -> None:
        history_path = self.logs_dir / "update_land_blocks_history.jsonl"
        if not history_path.exists():
            messagebox.showinfo("历史记录", "暂无历史记录。")
            return
        window = Toplevel(self.root)
        window.title("第四步更新历史记录")
        window.geometry("1000x640")
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
                f"第三步结果：{record.get('result_source')}\n"
                f"最新地类图斑：{record.get('block_source')}\n"
                f"最新行政区：{record.get('admin_source')}\n"
                f"输出：{record.get('output_path')}\n"
                f"补值模式：{record.get('nearest_mode')}\n"
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
        if not messagebox.askyesno("确认删除", "确定删除第四步更新日志和历史记录吗？"):
            return
        deleted = 0
        for path in self.logs_dir.glob("update_land_blocks_*.log"):
            path.unlink()
            deleted += 1
        history_path = self.logs_dir / "update_land_blocks_history.jsonl"
        if history_path.exists():
            history_path.unlink()
            deleted += 1
        self.log_status(f"已删除 {deleted} 个第四步日志/历史文件。")


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
    LandBlockUpdateApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
