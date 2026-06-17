"""ArcPy UI tool for merging polygon feature classes by common field names."""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, VERTICAL, IntVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk

try:
    import arcpy
except ImportError as exc:  # pragma: no cover - shown to user at runtime
    arcpy = None
    ARCPY_IMPORT_ERROR = exc
else:
    ARCPY_IMPORT_ERROR = None

from cropland_quality_update.paths import resolve_paths


FieldDef = tuple[str, str, int, int, str]
FIELD_NAME_LIMIT_NOTE = "Shapefile 字段名有硬限制，完整中文字段名请输出为 GDB 面要素类。"
GEOMETRY_FIELD_NAMES = {"shape", "shape_length", "shape_area"}
SKIP_FIELD_TYPES = {"OID", "Geometry", "Blob", "Raster", "GUID", "GlobalID", "XML"}
OVERLAP_DETAIL_LIMIT = 50
OVERLAP_FIELD_VALUE_LIMIT = 10
NUMERIC_TYPE_RANK = {
    "SmallInteger": 1,
    "Integer": 2,
    "Single": 3,
    "Double": 4,
}


@dataclass(frozen=True)
class VectorSource:
    kind: str  # shp | gdb
    source_path: Path
    layer_name: str | None
    display_name: str


@dataclass(frozen=True)
class ProjectionInfo:
    source: VectorSource
    spatial_reference: object | None
    message: str


@dataclass(frozen=True)
class MergeJob:
    job_id: str
    input_sources: list[VectorSource]
    priority_sources: list[VectorSource]
    output_path: Path
    output_feature_name: str | None
    output_kind: str  # shp | gdb
    target_spatial_reference: object
    target_projection_source: VectorSource
    projections_same: bool
    common_fields: list[FieldDef]
    created_at: str
    preflight_report: str = ""


@dataclass(frozen=True)
class PreparedSource:
    path: str
    source: VectorSource


@dataclass(frozen=True)
class OverlapFieldDifference:
    field_name: str
    left_value: str
    right_value: str


@dataclass(frozen=True)
class FieldMergeAnalysis:
    common_fields: list[FieldDef]
    missing_by_source: dict[VectorSource, list[FieldDef]]


@dataclass(frozen=True)
class SourcePreflightSummary:
    source: VectorSource
    feature_count: int
    data_field_count: int
    total_field_count: int


@dataclass(frozen=True)
class OverlapFeatureSample:
    left_oid: int
    right_oid: int
    overlap_area: float
    left_field_values: list[tuple[str, str]]
    right_field_values: list[tuple[str, str]]
    different_fields: list[OverlapFieldDifference]


@dataclass(frozen=True)
class OverlapIssue:
    left_source: VectorSource
    right_source: VectorSource
    overlap_feature_count: int
    conflicting_overlap_count: int
    overlap_area: float
    samples: list[OverlapFeatureSample]
    sample_error: str = ""


@dataclass(frozen=True)
class PreflightReport:
    source_summaries: list[SourcePreflightSummary]
    field_analysis: FieldMergeAnalysis
    projection_infos: list[ProjectionInfo]
    projections_same: bool
    expected_output_feature_count: int
    expected_output_field_count: int
    overlap_issues: list[OverlapIssue]
    text: str


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_for_file() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def require_arcpy() -> None:
    if arcpy is None:
        raise RuntimeError(f"缺少 arcpy。请用 ArcGIS Pro Python 环境运行：{ARCPY_IMPORT_ERROR}")


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


def source_path_for_log(source: VectorSource) -> str:
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


def output_format(path: Path) -> str:
    return "shp" if path.suffix.lower() == ".shp" else "gdb"


def output_dataset_path(output_kind: str, output_path: Path, output_feature_name: str | None) -> Path:
    return output_path if output_kind == "shp" else output_path / str(output_feature_name)


def sidecar_paths(shp_path: Path) -> list[Path]:
    base = shp_path.with_suffix("")
    return [base.with_suffix(ext) for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".shp.xml", ".fields.json")]


def delete_output_dataset(output_path: Path, output_feature_name: str | None = None) -> None:
    require_arcpy()
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


def list_shp_sources(folder: Path) -> list[VectorSource]:
    return [make_vector_source("shp", path) for path in sorted(folder.rglob("*.shp"))]


def iter_gdb_paths(root: Path) -> list[Path]:
    root = root.resolve()
    gdb_paths: list[Path] = []
    if is_gdb_path(root):
        gdb_paths.append(root)
    if root.is_dir():
        for candidate in root.rglob("*.gdb"):
            if candidate.is_dir() and candidate.resolve() not in {path.resolve() for path in gdb_paths}:
                gdb_paths.append(candidate)
    return gdb_paths


def list_gdb_polygon_layers(gdb_path: Path) -> list[VectorSource]:
    require_arcpy()
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


def discover_sources_in_folder_with_errors(folder: Path) -> tuple[list[VectorSource], list[str]]:
    sources: list[VectorSource] = []
    errors: list[str] = []
    seen = set()
    for shp in list_shp_sources(folder):
        key = ("shp", shp.source_path.resolve(), None)
        if key not in seen:
            seen.add(key)
            sources.append(shp)
    for gdb in iter_gdb_paths(folder):
        try:
            gdb_sources = list_gdb_polygon_layers(gdb)
        except Exception as exc:
            errors.append(f"{gdb}: {exc}")
            continue
        for source in gdb_sources:
            key = (source.kind, source.source_path.resolve(), source.layer_name)
            if key not in seen:
                seen.add(key)
                sources.append(source)
    return sources, errors


def merge_source_lists(existing: list[VectorSource], incoming: list[VectorSource]) -> list[VectorSource]:
    merged: list[VectorSource] = []
    seen = set()
    for source in [*existing, *incoming]:
        key = (source.kind, source.source_path.resolve(), source.layer_name)
        if key in seen:
            continue
        seen.add(key)
        merged.append(source)
    return merged


def is_data_field(field) -> bool:
    if field.type in SKIP_FIELD_TYPES:
        return False
    if field.name.lower() in GEOMETRY_FIELD_NAMES:
        return False
    return True


def get_field_defs(source: VectorSource) -> list[FieldDef]:
    require_arcpy()
    fields: list[FieldDef] = []
    for field in arcpy.ListFields(source_dataset_path(source)):
        if not is_data_field(field):
            continue
        fields.append((field.name, field.type, int(field.length or 0), int(field.scale or 0), field.aliasName or field.name))
    return fields


def merge_field_defs(fields: list[FieldDef]) -> FieldDef:
    name = fields[0][0]
    alias = next((field[4] for field in fields if field[4]), name)
    types = [field[1] for field in fields]
    if any(field_type == "String" for field_type in types):
        length = min(max(max(field[2] or 1 for field in fields), 1), 255)
        return (name, "String", length, 0, alias)
    if all(field_type in NUMERIC_TYPE_RANK for field_type in types):
        field_type = max(types, key=lambda item: NUMERIC_TYPE_RANK[item])
        if "Double" in types or "Single" in types:
            field_type = "Double"
        return (name, field_type, 0, max(field[3] for field in fields), alias)
    if all(field_type == "Date" for field_type in types):
        return (name, "Date", 0, 0, alias)
    length = min(max(max(field[2] or 80 for field in fields), 80), 255)
    return (name, "String", length, 0, alias)


def analyze_common_fields_by_name(input_sources: list[VectorSource]) -> FieldMergeAnalysis:
    defs_by_source = {source: get_field_defs(source) for source in input_sources}
    first_source = input_sources[0]
    first_fields = defs_by_source[first_source]
    field_names_by_source = {source: {field[0] for field in fields} for source, fields in defs_by_source.items()}
    common_names = set(field_names_by_source[first_source])
    for names in field_names_by_source.values():
        common_names &= names

    common_fields: list[FieldDef] = []
    for first_field in first_fields:
        name = first_field[0]
        if name not in common_names:
            continue
        same_name_fields = []
        for source in input_sources:
            field_by_name = {field[0]: field for field in defs_by_source[source]}
            same_name_fields.append(field_by_name[name])
        common_fields.append(merge_field_defs(same_name_fields))

    missing_by_source = {
        source: [field for field in fields if field[0] not in common_names]
        for source, fields in defs_by_source.items()
    }
    return FieldMergeAnalysis(common_fields, missing_by_source)


def read_source_spatial_reference(source: VectorSource) -> ProjectionInfo:
    require_arcpy()
    try:
        desc = arcpy.Describe(source_dataset_path(source))
        sr = desc.spatialReference
    except Exception as exc:
        return ProjectionInfo(source, None, f"投影读取失败：{exc}")
    if sr is None or not getattr(sr, "name", None) or sr.name == "Unknown":
        return ProjectionInfo(source, None, "缺少空间参考，无法识别投影")
    return ProjectionInfo(source, sr, describe_spatial_reference(sr))


def describe_spatial_reference(sr: object | None) -> str:
    if sr is None:
        return "未知投影"
    code = getattr(sr, "factoryCode", 0) or 0
    name = getattr(sr, "name", "") or "未命名"
    return f"{name}；EPSG:{code}" if code else f"{name}；无 EPSG/Authority"


def projection_text(sr: object | None) -> str:
    if sr is None:
        return "未知投影"
    lines = [describe_spatial_reference(sr)]
    text = getattr(sr, "exportToString", lambda: "")()
    if text:
        lines.extend(["", text])
    return "\n".join(lines)


def spatial_reference_equal(a: object | None, b: object | None) -> bool:
    if a is None or b is None:
        return False
    return bool(getattr(a, "factoryCode", 0) and getattr(a, "factoryCode", 0) == getattr(b, "factoryCode", 0)) or (
        getattr(a, "exportToString", lambda: "")() == getattr(b, "exportToString", lambda: "")()
    )


def analyze_projection_state(input_sources: list[VectorSource]) -> tuple[list[ProjectionInfo], bool]:
    infos = [read_source_spatial_reference(source) for source in input_sources]
    if any(info.spatial_reference is None for info in infos):
        return infos, False
    first = infos[0].spatial_reference
    return infos, all(spatial_reference_equal(first, info.spatial_reference) for info in infos[1:])


def source_feature_count(source: VectorSource) -> int:
    require_arcpy()
    return int(arcpy.management.GetCount(source_dataset_path(source))[0])


def source_preflight_summary(source: VectorSource) -> SourcePreflightSummary:
    require_arcpy()
    all_fields = list(arcpy.ListFields(source_dataset_path(source)))
    data_fields = [field for field in all_fields if is_data_field(field)]
    return SourcePreflightSummary(
        source=source,
        feature_count=source_feature_count(source),
        data_field_count=len(data_fields),
        total_field_count=len(all_fields),
    )


def feature_class_area_sum(feature_class: str) -> float:
    total = 0.0
    with arcpy.da.SearchCursor(feature_class, ["SHAPE@AREA"]) as cursor:
        for (area,) in cursor:
            if area:
                total += float(area)
    return total


def oid_field_name(dataset_path: str) -> str:
    return arcpy.Describe(dataset_path).OIDFieldName


def first_data_field_names(source: VectorSource, limit: int = OVERLAP_FIELD_VALUE_LIMIT) -> list[str]:
    names: list[str] = []
    for field in arcpy.ListFields(source_dataset_path(source)):
        if is_data_field(field):
            names.append(field.name)
        if len(names) >= limit:
            break
    return names


def field_value_text(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > 80:
        return text[:77] + "..."
    return text


def fetch_field_values_by_oid(source: VectorSource, oid_value: int, field_names: list[str]) -> list[tuple[str, str]]:
    if oid_value is None or oid_value < 0 or not field_names:
        return []
    dataset = source_dataset_path(source)
    oid_name = oid_field_name(dataset)
    where = f"{arcpy.AddFieldDelimiters(dataset, oid_name)} = {int(oid_value)}"
    with arcpy.da.SearchCursor(dataset, field_names, where_clause=where) as cursor:
        for row in cursor:
            return [(name, field_value_text(value)) for name, value in zip(field_names, row)]
    return []


def fetch_raw_values_by_oid(source: VectorSource, oid_value: int, field_names: list[str]) -> dict[str, object]:
    if oid_value is None or oid_value < 0 or not field_names:
        return {}
    dataset = source_dataset_path(source)
    oid_name = oid_field_name(dataset)
    where = f"{arcpy.AddFieldDelimiters(dataset, oid_name)} = {int(oid_value)}"
    with arcpy.da.SearchCursor(dataset, field_names, where_clause=where) as cursor:
        for row in cursor:
            return {name: value for name, value in zip(field_names, row)}
    return {}


def values_equivalent(left, right) -> bool:
    if left is None and right in (None, ""):
        return True
    if right is None and left in (None, ""):
        return True
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= 1e-9
    return str(left).strip() == str(right).strip()


def geometry_is_empty(geometry) -> bool:
    if geometry is None:
        return True
    try:
        return bool(geometry.isEmpty)
    except AttributeError:
        return (getattr(geometry, "partCount", 0) or 0) <= 0 or (getattr(geometry, "area", 0) or 0) <= 0


def intersect_fid_fields(intersect_fc: str) -> tuple[str, str]:
    fid_fields = [field.name for field in arcpy.ListFields(intersect_fc) if field.name.upper().startswith("FID_")]
    if len(fid_fields) < 2:
        raise RuntimeError(f"交集结果中没有找到两个 FID 字段：{intersect_fc}")
    return fid_fields[0], fid_fields[1]


def overlap_samples(
    intersect_fc: str,
    left: VectorSource,
    right: VectorSource,
    common_field_names: list[str],
    only_conflicting: bool = False,
) -> tuple[list[OverlapFeatureSample], str]:
    try:
        left_fid_field, right_fid_field = intersect_fid_fields(intersect_fc)
        left_fields = first_data_field_names(left)
        right_fields = first_data_field_names(right)
        samples: list[OverlapFeatureSample] = []
        with arcpy.da.SearchCursor(intersect_fc, [left_fid_field, right_fid_field, "SHAPE@AREA"]) as cursor:
            for left_oid, right_oid, area in cursor:
                left_raw = fetch_raw_values_by_oid(left, int(left_oid), common_field_names)
                right_raw = fetch_raw_values_by_oid(right, int(right_oid), common_field_names)
                differences = [
                    OverlapFieldDifference(field_name, field_value_text(left_raw.get(field_name)), field_value_text(right_raw.get(field_name)))
                    for field_name in common_field_names
                    if not values_equivalent(left_raw.get(field_name), right_raw.get(field_name))
                ]
                if only_conflicting and not differences:
                    continue
                samples.append(
                    OverlapFeatureSample(
                        left_oid=int(left_oid),
                        right_oid=int(right_oid),
                        overlap_area=float(area or 0),
                        left_field_values=fetch_field_values_by_oid(left, int(left_oid), left_fields),
                        right_field_values=fetch_field_values_by_oid(right, int(right_oid), right_fields),
                        different_fields=differences,
                    )
                )
        return samples, ""
    except Exception as exc:
        return [], f"重叠字段值样例提取失败：{exc}"


def overlap_conflict_count(intersect_fc: str, left: VectorSource, right: VectorSource, common_field_names: list[str]) -> int:
    left_fid_field, right_fid_field = intersect_fid_fields(intersect_fc)
    count = 0
    with arcpy.da.SearchCursor(intersect_fc, [left_fid_field, right_fid_field]) as cursor:
        for left_oid, right_oid in cursor:
            left_raw = fetch_raw_values_by_oid(left, int(left_oid), common_field_names)
            right_raw = fetch_raw_values_by_oid(right, int(right_oid), common_field_names)
            if any(not values_equivalent(left_raw.get(field_name), right_raw.get(field_name)) for field_name in common_field_names):
                count += 1
    return count


def check_pair_overlap(
    left: VectorSource,
    right: VectorSource,
    temp_dir: Path,
    index: int,
    common_field_names: list[str],
) -> OverlapIssue | None:
    require_arcpy()
    out_gdb = temp_dir / f"overlap_{index:03d}.gdb"
    arcpy.management.CreateFileGDB(str(temp_dir), out_gdb.name)
    out_fc = str(out_gdb / "intersect")
    try:
        arcpy.analysis.PairwiseIntersect(
            [source_dataset_path(left), source_dataset_path(right)],
            out_fc,
            "ONLY_FID",
        )
        count = int(arcpy.management.GetCount(out_fc)[0])
        if count <= 0:
            return None
        conflict_count = overlap_conflict_count(out_fc, left, right, common_field_names)
        return OverlapIssue(left, right, count, conflict_count, feature_class_area_sum(out_fc), [], "")
    finally:
        if out_gdb.exists():
            shutil.rmtree(out_gdb, ignore_errors=True)


def check_source_overlaps(sources: list[VectorSource], temp_dir: Path, common_field_names: list[str]) -> list[OverlapIssue]:
    issues: list[OverlapIssue] = []
    temp_dir.mkdir(parents=True, exist_ok=True)
    pair_index = 1
    for left_index, left in enumerate(sources):
        for right in sources[left_index + 1 :]:
            issue = check_pair_overlap(left, right, temp_dir, pair_index, common_field_names)
            pair_index += 1
            if issue is not None:
                issues.append(issue)
    total_overlap_count = sum(issue.overlap_feature_count for issue in issues)
    total_conflict_count = sum(issue.conflicting_overlap_count for issue in issues)
    if total_conflict_count >= OVERLAP_DETAIL_LIMIT:
        return [
            OverlapIssue(
                issue.left_source,
                issue.right_source,
                issue.overlap_feature_count,
                issue.conflicting_overlap_count,
                issue.overlap_area,
                [],
                f"公共字段值不一致的重叠要素过多（共 {total_conflict_count} 个），不展开字段值。",
            )
            for issue in issues
        ]
    if total_overlap_count >= OVERLAP_DETAIL_LIMIT and total_conflict_count == 0:
        return [
            OverlapIssue(
                issue.left_source,
                issue.right_source,
                issue.overlap_feature_count,
                issue.conflicting_overlap_count,
                issue.overlap_area,
                [],
                f"重叠要素较多（总体重叠 {total_overlap_count} 个），公共字段值全部一致，不展开字段值；确认后将直接 Erase。",
            )
            for issue in issues
        ]
    only_conflicting_samples = total_overlap_count >= OVERLAP_DETAIL_LIMIT and total_conflict_count > 0
    detailed_issues: list[OverlapIssue] = []
    pair_index = 1
    for issue in issues:
        out_gdb = temp_dir / f"overlap_detail_{pair_index:03d}.gdb"
        arcpy.management.CreateFileGDB(str(temp_dir), out_gdb.name)
        out_fc = str(out_gdb / "intersect")
        try:
            arcpy.analysis.PairwiseIntersect(
                [source_dataset_path(issue.left_source), source_dataset_path(issue.right_source)],
                out_fc,
                "ONLY_FID",
            )
            samples, sample_error = overlap_samples(
                out_fc,
                issue.left_source,
                issue.right_source,
                common_field_names,
                only_conflicting=only_conflicting_samples,
            )
            if only_conflicting_samples and not sample_error and issue.conflicting_overlap_count:
                sample_error = (
                    f"总体重叠 {total_overlap_count} 个，未全部展开；"
                    "这里只展示公共字段值不一致、需要决定优先级的重叠样例。"
                )
            detailed_issues.append(
                OverlapIssue(
                    issue.left_source,
                    issue.right_source,
                    issue.overlap_feature_count,
                    issue.conflicting_overlap_count,
                    issue.overlap_area,
                    samples,
                    sample_error,
                )
            )
        finally:
            shutil.rmtree(out_gdb, ignore_errors=True)
        pair_index += 1
    return detailed_issues


def format_field_def(field: FieldDef) -> str:
    name, field_type, length, scale, _alias = field
    if field_type == "String":
        return f"{name}({field_type}, 长度 {length})"
    if field_type in NUMERIC_TYPE_RANK:
        return f"{name}({field_type}, 小数位 {scale})"
    return f"{name}({field_type})"


def format_overlap_values(values: list[tuple[str, str]]) -> str:
    if not values:
        return "无可展示字段值"
    return "；".join(f"{name}={value}" for name, value in values)


def build_preflight_text(
    summaries: list[SourcePreflightSummary],
    field_analysis: FieldMergeAnalysis,
    projection_infos: list[ProjectionInfo],
    projections_same: bool,
    overlap_issues: list[OverlapIssue],
) -> str:
    expected_feature_count = sum(summary.feature_count for summary in summaries)
    expected_field_count = len(field_analysis.common_fields)
    lines: list[str] = []
    lines.append("合并前检查报告")
    lines.append("=" * 60)
    lines.append(f"输入数据源数量：{len(summaries)}")
    lines.append(f"预计合并后面要素数：{expected_feature_count}")
    lines.append(f"预计合并后属性字段数：{expected_field_count}")
    lines.append(f"投影是否一致：{'是' if projections_same else '否，需要按基准投影重投影'}")
    lines.append("")
    lines.append("一、每个输入数据源统计")
    for index, summary in enumerate(summaries, start=1):
        lines.append(f"{index}. {source_label(summary.source)}")
        lines.append(f"   面要素数：{summary.feature_count}")
        lines.append(f"   可参与合并的属性字段数：{summary.data_field_count}")
        lines.append(f"   全部字段数（含系统字段）：{summary.total_field_count}")
        info = next((item for item in projection_infos if item.source == summary.source), None)
        if info is not None:
            lines.append(f"   投影：{info.message}")
    lines.append("")
    lines.append("二、字段合并情况")
    if field_analysis.common_fields:
        lines.append(f"成功识别为相同字段并将参与合并：{len(field_analysis.common_fields)} 个")
        lines.append("字段清单：")
        for field in field_analysis.common_fields:
            lines.append(f"   - {format_field_def(field)}")
    else:
        lines.append("没有识别到公共字段，不能继续合并。")
    lines.append("")
    lines.append("未合并字段（字段名没有在所有输入数据源中同时出现）：")
    any_missing = False
    for source, fields in field_analysis.missing_by_source.items():
        if not fields:
            continue
        any_missing = True
        lines.append(f"   - {source_label(source)}")
        for field in fields:
            lines.append(f"     * {format_field_def(field)}")
    if not any_missing:
        lines.append("   无。所有可参与字段都能在选中数据源之间对应。")
    lines.append("")
    lines.append("三、空间重叠检查（不同输入数据源之间）")
    if overlap_issues:
        total_overlap_count = sum(issue.overlap_feature_count for issue in overlap_issues)
        total_conflict_count = sum(issue.conflicting_overlap_count for issue in overlap_issues)
        lines.append(f"发现 {len(overlap_issues)} 组输入数据源存在空间重叠；合并时会自动 Erase 后写入，最终输出不允许保留重叠面。")
        lines.append(f"重叠要素总数：{total_overlap_count}；公共字段值不一致的重叠要素数：{total_conflict_count}")
        if total_conflict_count:
            lines.append("存在公共字段值不一致的重叠。提交任务前需要给数据源排序，排在前面的数据源优先保留重叠区域。")
        else:
            lines.append("所有重叠要素的公共字段值完全一致，确认报告后可直接自动去重叠。")
        for issue in overlap_issues:
            lines.append(f"   - {source_label(issue.left_source)}")
            lines.append(f"     与 {source_label(issue.right_source)}")
            lines.append(f"     重叠要素数：{issue.overlap_feature_count}，重叠总面积：{issue.overlap_area:.6f}")
            if issue.conflicting_overlap_count:
                lines.append(f"     公共字段值不一致：{issue.conflicting_overlap_count} 个")
            else:
                lines.append("     公共字段值检查：全部一致，可直接自动 Erase")
            if issue.sample_error:
                lines.append(f"     {issue.sample_error}")
            if issue.samples:
                lines.append(f"     重叠要素字段值样例（每个源文件前 {OVERLAP_FIELD_VALUE_LIMIT} 个业务字段）：")
                for sample_index, sample in enumerate(issue.samples, start=1):
                    lines.append(
                        f"       {sample_index}. 重叠面积：{sample.overlap_area:.6f}；"
                        f"左源 OID={sample.left_oid}；右源 OID={sample.right_oid}"
                    )
                    lines.append(f"          左源字段值：{format_overlap_values(sample.left_field_values)}")
                    lines.append(f"          右源字段值：{format_overlap_values(sample.right_field_values)}")
                    if sample.different_fields:
                        diff_text = "；".join(
                            f"{diff.field_name}: 左={diff.left_value}, 右={diff.right_value}"
                            for diff in sample.different_fields[:OVERLAP_FIELD_VALUE_LIMIT]
                        )
                        lines.append(f"          不一致公共字段：{diff_text}")
                    else:
                        lines.append("          公共字段值：完全一致")
    else:
        lines.append("未发现不同输入数据源之间的面要素空间重叠。")
    lines.append("")
    lines.append("确认以上信息没有问题后，才建议继续执行合并。")
    return "\n".join(lines)


def build_preflight_report(sources: list[VectorSource], temp_dir: Path) -> PreflightReport:
    field_analysis = analyze_common_fields_by_name(sources)
    projection_infos, same = analyze_projection_state(sources)
    summaries = [source_preflight_summary(source) for source in sources]
    overlap_temp_dir = temp_dir / f"preflight_{timestamp_for_file()}_{uuid.uuid4().hex[:8]}"
    try:
        common_field_names = [field[0] for field in field_analysis.common_fields]
        overlap_issues = check_source_overlaps(sources, overlap_temp_dir, common_field_names)
    finally:
        shutil.rmtree(overlap_temp_dir, ignore_errors=True)
    text = build_preflight_text(summaries, field_analysis, projection_infos, same, overlap_issues)
    return PreflightReport(
        source_summaries=summaries,
        field_analysis=field_analysis,
        projection_infos=projection_infos,
        projections_same=same,
        expected_output_feature_count=sum(summary.feature_count for summary in summaries),
        expected_output_field_count=len(field_analysis.common_fields),
        overlap_issues=overlap_issues,
        text=text,
    )


def add_field_from_def(feature_class: str, field: FieldDef, workspace: str | None = None) -> str:
    require_arcpy()
    name, field_type, length, _scale, alias = field
    target_name = arcpy.ValidateFieldName(name, workspace) if workspace else name
    existing = {field_obj.name for field_obj in arcpy.ListFields(feature_class)}
    base_name = target_name
    suffix = 1
    while target_name in existing:
        suffix_text = str(suffix)
        target_name = arcpy.ValidateFieldName(f"{base_name[: max(1, 10 - len(suffix_text))]}{suffix_text}", workspace)
        suffix += 1
    kwargs = {"field_alias": alias or name}
    if field_type == "String":
        kwargs["field_length"] = max(1, min(length or 255, 255))
        arcpy.management.AddField(feature_class, target_name, "TEXT", **kwargs)
    elif field_type in NUMERIC_TYPE_RANK:
        arcpy.management.AddField(feature_class, target_name, field_type.upper(), **kwargs)
    elif field_type == "Date":
        arcpy.management.AddField(feature_class, target_name, "DATE", **kwargs)
    else:
        kwargs["field_length"] = max(1, min(length or 255, 255))
        arcpy.management.AddField(feature_class, target_name, "TEXT", **kwargs)
    return target_name


def ascii_shp_field_name(original_name: str, index: int, existing: set[str]) -> str:
    ascii_name = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_" for ch in original_name)
    ascii_name = ascii_name.strip("_") or f"F{index:03d}"
    if len(ascii_name) < 3:
        ascii_name = f"F{index:03d}"
    candidate = ascii_name[:10]
    suffix = 1
    while not candidate or candidate.upper() in existing:
        suffix_text = str(suffix)
        candidate = f"{ascii_name[: max(1, 10 - len(suffix_text))]}{suffix_text}"[:10]
        suffix += 1
    existing.add(candidate.upper())
    return candidate


def add_shp_field_from_def(feature_class: str, field: FieldDef, index: int, existing: set[str]) -> str:
    require_arcpy()
    name, field_type, length, _scale, _alias = field
    target_name = ascii_shp_field_name(name, index, existing)
    if field_type == "String":
        arcpy.management.AddField(feature_class, target_name, "TEXT", field_length=max(1, min(length or 254, 254)))
    elif field_type in NUMERIC_TYPE_RANK:
        arcpy.management.AddField(feature_class, target_name, field_type.upper())
    elif field_type == "Date":
        arcpy.management.AddField(feature_class, target_name, "DATE")
    else:
        arcpy.management.AddField(feature_class, target_name, "TEXT", field_length=max(1, min(length or 254, 254)))
    return target_name


def create_output_feature_class(job: MergeJob, logger: logging.Logger) -> tuple[str, dict[str, str]]:
    require_arcpy()
    delete_output_dataset(job.output_path, job.output_feature_name)
    if job.output_kind == "shp":
        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        arcpy.management.CreateFeatureclass(
            str(job.output_path.parent),
            job.output_path.name,
            "POLYGON",
            spatial_reference=job.target_spatial_reference,
        )
        output_fc = str(job.output_path)
        workspace = str(job.output_path.parent)
    else:
        job.output_path.parent.mkdir(parents=True, exist_ok=True)
        if not job.output_path.exists():
            arcpy.management.CreateFileGDB(str(job.output_path.parent), job.output_path.name)
        if not job.output_feature_name:
            raise RuntimeError("输出到 GDB 时必须指定面要素类名称。")
        arcpy.management.CreateFeatureclass(
            str(job.output_path),
            job.output_feature_name,
            "POLYGON",
            spatial_reference=job.target_spatial_reference,
        )
        output_fc = str(job.output_path / job.output_feature_name)
        workspace = str(job.output_path)

    field_map: dict[str, str] = {}
    existing_shp_names = {field.name.upper() for field in arcpy.ListFields(output_fc)} if job.output_kind == "shp" else set()
    for index, field in enumerate(job.common_fields, start=1):
        if job.output_kind == "shp":
            output_field_name = add_shp_field_from_def(output_fc, field, index, existing_shp_names)
        else:
            output_field_name = add_field_from_def(output_fc, field, None)
        field_map[field[0]] = output_field_name
    logger.info("输出字段映射：%s", field_map)
    if job.output_kind == "shp":
        (job.output_path.with_suffix(".fields.json")).write_text(
            json.dumps(field_map, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return output_fc, field_map


def prepare_sources(
    input_sources: list[VectorSource],
    target_spatial_reference: object,
    temp_dir: Path,
    logger: logging.Logger,
) -> list[PreparedSource]:
    require_arcpy()
    prepared: list[PreparedSource] = []
    for index, source in enumerate(input_sources, start=1):
        source_path = source_dataset_path(source)
        info = read_source_spatial_reference(source)
        if info.spatial_reference is None:
            raise RuntimeError(f"输入数据缺少可识别投影，无法重投影：{source_label(source)}")
        if spatial_reference_equal(info.spatial_reference, target_spatial_reference):
            prepared.append(PreparedSource(source_path, source))
            logger.info("投影一致，直接参与合并：%s", source_path_for_log(source))
            continue
        out_path = temp_dir / f"projected_{index:03d}.gdb"
        arcpy.management.CreateFileGDB(str(temp_dir), out_path.name)
        out_fc = str(out_path / "projected")
        logger.info("重投影：%s -> %s", source_path_for_log(source), out_fc)
        arcpy.management.Project(source_path, out_fc, target_spatial_reference)
        prepared.append(PreparedSource(out_fc, source))
    return prepared


def coerce_value(value, field: FieldDef):
    if value is None:
        return None
    field_type = field[1]
    if field_type == "String":
        text = str(value)
        length = field[2] or 255
        return text[:length]
    if field_type in NUMERIC_TYPE_RANK:
        try:
            return float(value) if field_type in {"Single", "Double"} else int(float(value))
        except Exception:
            return None
    return value


def create_empty_union_feature_class(
    output_fc: str,
    temp_dir: Path,
    target_spatial_reference: object,
    logger: logging.Logger,
) -> str:
    temp_dir.mkdir(parents=True, exist_ok=True)
    union_gdb = temp_dir / "kept_union.gdb"
    arcpy.management.CreateFileGDB(str(temp_dir), union_gdb.name)
    union_fc = str(union_gdb / "kept_union")
    arcpy.management.CreateFeatureclass(
        str(union_gdb),
        "kept_union",
        "POLYGON",
        template=output_fc,
        spatial_reference=target_spatial_reference,
    )
    logger.info("创建已保留区域临时图层：%s", union_fc)
    return union_fc


def source_after_erasing_kept_area(
    prepared: PreparedSource,
    union_fc: str,
    temp_dir: Path,
    index: int,
    logger: logging.Logger,
) -> str:
    kept_count = int(arcpy.management.GetCount(union_fc)[0])
    if kept_count <= 0:
        return prepared.path
    temp_dir.mkdir(parents=True, exist_ok=True)
    erase_gdb = temp_dir / f"erase_{index:03d}.gdb"
    arcpy.management.CreateFileGDB(str(temp_dir), erase_gdb.name)
    erased_fc = str(erase_gdb / "erased")
    logger.info("Erase 去重叠：%s - 已保留区域(%s 个面)", source_path_for_log(prepared.source), kept_count)
    arcpy.analysis.Erase(prepared.path, union_fc, erased_fc)
    return erased_fc


def append_geometry_to_union(source_fc: str, union_fc: str, logger: logging.Logger) -> None:
    before = int(arcpy.management.GetCount(union_fc)[0])
    inserted = 0
    with arcpy.da.InsertCursor(union_fc, ["SHAPE@"]) as insert_cursor:
        with arcpy.da.SearchCursor(source_fc, ["SHAPE@"]) as search_cursor:
            for (geometry,) in search_cursor:
                if geometry_is_empty(geometry):
                    continue
                insert_cursor.insertRow([geometry])
                inserted += 1
    after = int(arcpy.management.GetCount(union_fc)[0])
    logger.info("更新已保留区域：%s -> %s，本次加入 %s 个面", before, after, inserted)


def order_prepared_sources(prepared_sources: list[PreparedSource], priority_sources: list[VectorSource]) -> list[PreparedSource]:
    by_source = {prepared.source: prepared for prepared in prepared_sources}
    ordered = [by_source[source] for source in priority_sources if source in by_source]
    remaining = [prepared for prepared in prepared_sources if prepared not in ordered]
    return ordered + remaining


def merge_prepared_sources_to_output(
    prepared_sources: list[PreparedSource],
    job: MergeJob,
    temp_dir: Path,
    logger: logging.Logger,
) -> int:
    require_arcpy()
    output_fc, output_field_map = create_output_feature_class(job, logger)
    output_fields = [output_field_map[field[0]] for field in job.common_fields]
    read_fields = [field[0] for field in job.common_fields]
    insert_fields = ["SHAPE@"] + output_fields
    ordered_sources = order_prepared_sources(prepared_sources, job.priority_sources)
    union_fc = create_empty_union_feature_class(output_fc, temp_dir, job.target_spatial_reference, logger)

    total = 0
    with arcpy.da.InsertCursor(output_fc, insert_fields) as insert_cursor:
        for index, prepared in enumerate(ordered_sources, start=1):
            effective_path = source_after_erasing_kept_area(prepared, union_fc, temp_dir, index, logger)
            count = 0
            with arcpy.da.SearchCursor(effective_path, ["SHAPE@"] + read_fields) as search_cursor:
                for row in search_cursor:
                    geometry = row[0]
                    if geometry_is_empty(geometry):
                        continue
                    values = [coerce_value(value, field) for value, field in zip(row[1:], job.common_fields)]
                    insert_cursor.insertRow([geometry, *values])
                    count += 1
            total += count
            append_geometry_to_union(effective_path, union_fc, logger)
            logger.info("已合并 %s，Erase 后写入记录数：%s", source_path_for_log(prepared.source), count)
    logger.info("输出完成：%s；总记录数：%s", output_fc, total)
    return total


def setup_job_logger(logs_dir: Path, job_id: str) -> tuple[logging.Logger, Path]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"merge_vector_{timestamp_for_file()}_{job_id}.log"
    logger = logging.getLogger(f"merge_vector_arcpy.{job_id}")
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


class MergeWorker(threading.Thread):
    def __init__(self, job_queue: queue.Queue, event_queue: queue.Queue, logs_dir: Path, process_dir: Path):
        super().__init__(daemon=True)
        self.job_queue = job_queue
        self.event_queue = event_queue
        self.logs_dir = logs_dir
        self.process_dir = process_dir
        self.history_path = logs_dir / "merge_vector_history.jsonl"

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

    def process_job(self, job: MergeJob) -> None:
        logger, log_path = setup_job_logger(self.logs_dir, job.job_id)
        temp_dir = self.process_dir / f"merge_{timestamp_for_file()}_{job.job_id}"
        output_target = str(job.output_path / job.output_feature_name) if job.output_kind == "gdb" else str(job.output_path)
        record = {
            "job_id": job.job_id,
            "created_at": job.created_at,
            "started_at": now_text(),
            "ended_at": None,
            "status": "running",
            "input_sources": [source_path_for_log(source) for source in job.input_sources],
            "priority_sources": [source_path_for_log(source) for source in job.priority_sources],
            "output_path": output_target,
            "target_projection_source": source_path_for_log(job.target_projection_source),
            "target_projection": describe_spatial_reference(job.target_spatial_reference),
            "common_fields": [list(field) for field in job.common_fields],
            "preflight_report": job.preflight_report,
            "log_path": str(log_path),
            "error": None,
            "merged_count": None,
        }
        self.send("job_started", {"job_id": job.job_id, "message": "开始合并", "log_path": str(log_path)})
        try:
            require_arcpy()
            temp_dir.mkdir(parents=True, exist_ok=True)
            arcpy.env.overwriteOutput = True
            logger.info("任务开始：%s", job.job_id)
            logger.info("输入源：%s", [source_path_for_log(source) for source in job.input_sources])
            logger.info("重叠保留优先级：%s", [source_path_for_log(source) for source in job.priority_sources])
            logger.info("输出目标：%s", output_target)
            logger.info("目标投影：%s", describe_spatial_reference(job.target_spatial_reference))
            logger.info("公共字段数量：%s", len(job.common_fields))
            if job.preflight_report:
                logger.info("提交前检查报告：\n%s", job.preflight_report)
            prepared_sources = prepare_sources(job.input_sources, job.target_spatial_reference, temp_dir, logger)
            merged_count = merge_prepared_sources_to_output(prepared_sources, job, temp_dir, logger)
            record.update({"status": "success", "merged_count": merged_count})
            self.send(
                "job_done",
                {
                    "job_id": job.job_id,
                    "message": f"合并完成：{output_target}；记录数 {merged_count}",
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
                    "message": f"合并失败：{exc}",
                    "log_path": str(log_path),
                },
            )
        finally:
            record["ended_at"] = now_text()
            append_history(self.history_path, record)
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info("过程目录已清理：%s", temp_dir)
            close_job_logger(logger)


class ScrollableCheckList(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.tree = None
        self.selected_keys: set[str] = set()
        self.source_map: dict[str, VectorSource] = {}
        self._build()

    def _build(self) -> None:
        self.tree = ttk.Treeview(self, columns=("selected", "kind", "path"), show="headings", height=10, selectmode="browse")
        self.tree.heading("selected", text="选中")
        self.tree.heading("kind", text="类型")
        self.tree.heading("path", text="数据源")
        self.tree.column("selected", width=60, anchor="center", stretch=False)
        self.tree.column("kind", width=80, anchor="center", stretch=False)
        self.tree.column("path", width=940, anchor="w")
        y_scroll = ttk.Scrollbar(self, orient=VERTICAL, command=self.tree.yview)
        x_scroll = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._click_event)
        self.tree.bind("<Double-1>", self._toggle_event)
        self.tree.bind("<space>", self._toggle_event)

    def set_sources(self, sources: list[VectorSource]) -> None:
        existing_selected = set(self.selected_keys)
        self.selected_keys.clear()
        self.source_map.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for source in sources:
            key = source_path_for_log(source)
            self.source_map[key] = source
            if key in existing_selected:
                self.selected_keys.add(key)
            self.tree.insert("", END, iid=key, values=("✓" if key in self.selected_keys else "", source.kind.upper(), source_label(source)))

    def _toggle_event(self, _event=None) -> str:
        self.toggle_current()
        return "break"

    def _click_event(self, event) -> None:
        region = self.tree.identify("region", event.x, event.y)
        column = self.tree.identify_column(event.x)
        item_id = self.tree.identify_row(event.y)
        if region == "cell" and column == "#1" and item_id:
            self.toggle_key(item_id)

    def toggle_current(self) -> None:
        item_id = self.tree.focus()
        if not item_id:
            selected = self.tree.selection()
            item_id = selected[0] if selected else ""
        if item_id:
            self.toggle_key(item_id)

    def toggle_key(self, key: str) -> None:
        if key in self.selected_keys:
            self.selected_keys.remove(key)
        else:
            self.selected_keys.add(key)
        source = self.source_map[key]
        self.tree.item(key, values=("✓" if key in self.selected_keys else "", source.kind.upper(), source_label(source)))

    def selected_sources(self) -> list[VectorSource]:
        return [self.source_map[key] for key in self.tree.get_children() if key in self.selected_keys]

    def select_all(self, value: bool) -> None:
        self.selected_keys = set(self.source_map.keys()) if value else set()
        for key, source in self.source_map.items():
            self.tree.item(key, values=("✓" if key in self.selected_keys else "", source.kind.upper(), source_label(source)))


class MergeArcpyApp:
    def __init__(self, root: Tk):
        self.root = root
        self.paths = resolve_paths(Path.cwd())
        self.logs_dir = self.paths.outputs_dir / "logs"
        self.process_dir = self.paths.outputs_dir / "process_files"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.process_dir.mkdir(parents=True, exist_ok=True)

        self.folder_var = StringVar()
        self.output_kind_var = IntVar(value=1)
        self.output_shp_var = StringVar()
        self.output_gdb_var = StringVar()
        self.output_feature_var = StringVar(value="合并高标准农田区域")
        self.reference_mode = IntVar(value=0)
        self.reference_file_var = StringVar()
        self.reference_extra_var = StringVar()
        self.projections_same = False
        self.projection_infos: list[ProjectionInfo] = []
        self.target_spatial_reference = None
        self.target_source: VectorSource | None = None
        self.discovered_sources: list[VectorSource] = []
        self.analyzed_sources: tuple[VectorSource, ...] = ()
        self.last_preflight_report: PreflightReport | None = None
        self.last_preflight_sources: tuple[VectorSource, ...] = ()

        self.job_queue: queue.Queue = queue.Queue()
        self.event_queue: queue.Queue = queue.Queue()
        self.worker = MergeWorker(self.job_queue, self.event_queue, self.logs_dir, self.process_dir)
        self.worker.start()

        self.root.title("高标准农田面矢量合并工具（ArcPy）")
        self.root.geometry("1120x850")
        self.build_ui()
        self.root.after(200, self.poll_worker_events)

    def build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill=BOTH, expand=True)

        source_frame = ttk.LabelFrame(container, text="1. 添加待合并数据源")
        source_frame.pack(fill="x", pady=5)
        source_row = ttk.Frame(source_frame)
        source_row.pack(fill="x", padx=5, pady=5)
        ttk.Entry(source_row, textvariable=self.folder_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(source_row, text="添加文件夹", command=self.choose_folder).pack(side=LEFT, padx=3)
        ttk.Button(source_row, text="添加 shp", command=self.add_shp_files).pack(side=LEFT, padx=3)
        ttk.Button(source_row, text="添加 gdb", command=self.add_gdb_folder).pack(side=LEFT, padx=3)
        ttk.Button(source_row, text="刷新清单", command=self.refresh_sources).pack(side=LEFT, padx=3)

        file_frame = ttk.LabelFrame(container, text="2. 勾选需要合并的面数据")
        file_frame.pack(fill="x", pady=5)
        buttons = ttk.Frame(file_frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="全选", command=lambda: self.source_checklist.select_all(True)).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(buttons, text="全不选", command=lambda: self.source_checklist.select_all(False)).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(buttons, text="分析投影和字段", command=self.analyze_selection).pack(side=LEFT, padx=5, pady=4)
        self.source_checklist = ScrollableCheckList(file_frame)
        self.source_checklist.pack(fill="both", expand=True, padx=5, pady=5)

        projection_frame = ttk.LabelFrame(container, text="3. 投影确认")
        projection_frame.pack(fill="both", pady=5)
        self.projection_tree = ttk.Treeview(projection_frame, columns=("file", "projection"), show="headings", height=5)
        self.projection_tree.heading("file", text="数据源")
        self.projection_tree.heading("projection", text="投影")
        self.projection_tree.column("file", width=520)
        self.projection_tree.column("projection", width=520)
        self.projection_tree.pack(fill="x", padx=5, pady=5)

        reference_frame = ttk.Frame(projection_frame)
        reference_frame.pack(fill="x", padx=5, pady=2)
        ttk.Radiobutton(reference_frame, text="使用选中的数据源作为基准", variable=self.reference_mode, value=0, command=self.update_target_projection).pack(side=LEFT)
        self.reference_combo = ttk.Combobox(reference_frame, textvariable=self.reference_file_var, state="readonly", width=72)
        self.reference_combo.pack(side=LEFT, padx=5)
        self.reference_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_target_projection())
        ttk.Radiobutton(reference_frame, text="使用额外参考文件", variable=self.reference_mode, value=1, command=self.update_target_projection).pack(side=LEFT, padx=5)
        ttk.Button(reference_frame, text="选择参考 shp", command=self.choose_extra_reference).pack(side=LEFT)

        self.projection_text = Text(projection_frame, height=6, wrap="word")
        self.projection_text.pack(fill="both", expand=True, padx=5, pady=5)

        output_frame = ttk.LabelFrame(container, text="4. 输出位置和任务")
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
        ttk.Button(gdb_row, text="提交合并任务", command=self.submit_job).pack(side=LEFT, padx=8)

        history_frame = ttk.LabelFrame(container, text="5. 日志和历史记录")
        history_frame.pack(fill="both", expand=True, pady=5)
        action_row = ttk.Frame(history_frame)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="查看历史记录", command=self.show_history).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="打开日志文件夹", command=self.open_logs_folder).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="删除日志", command=self.delete_logs).pack(side=LEFT, padx=5, pady=4)
        self.status_text = Text(history_frame, height=10, wrap="word")
        self.status_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.log_status("工具已启动。ArcPy 模式下推荐输出 GDB 面要素类。")

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择一个可添加数据源的文件夹")
        if folder:
            self.folder_var.set(folder)
            self.add_folder_sources(Path(folder))

    def add_folder_sources(self, folder: Path) -> None:
        if not folder.exists():
            messagebox.showwarning("提示", "请先选择有效文件夹。")
            return
        sources, errors = discover_sources_in_folder_with_errors(folder)
        before = len(self.discovered_sources)
        self.discovered_sources = merge_source_lists(self.discovered_sources, sources)
        self.discovered_sources.sort(key=source_path_for_log)
        self.source_checklist.set_sources(self.discovered_sources)
        added = len(self.discovered_sources) - before
        self.log_status(f"从文件夹添加 {added} 个数据源，当前清单共 {len(self.discovered_sources)} 个。")
        if added == 0 and not errors:
            messagebox.showinfo("提示", "这个文件夹里没有发现新的 shp 或 gdb 面图层。")
        for error in errors:
            self.log_status(f"跳过 gdb：{error}")

    def add_shp_files(self) -> None:
        paths = filedialog.askopenfilenames(title="添加 shp 文件", filetypes=[("Shapefile", "*.shp")])
        if not paths:
            return
        before = len(self.discovered_sources)
        incoming = [make_vector_source("shp", Path(path)) for path in paths]
        self.discovered_sources = merge_source_lists(self.discovered_sources, incoming)
        added = len(self.discovered_sources) - before
        self.discovered_sources.sort(key=source_path_for_log)
        self.source_checklist.set_sources(self.discovered_sources)
        self.log_status(f"已添加 {added} 个 shp，当前清单共 {len(self.discovered_sources)} 个。")

    def add_gdb_folder(self) -> None:
        path = filedialog.askdirectory(title="选择 .gdb 文件夹")
        if not path:
            return
        gdb_path = find_nearest_gdb_path(Path(path))
        if gdb_path is None or not is_gdb_path(gdb_path):
            messagebox.showwarning("提示", "请选择一个 .gdb 文件夹。")
            return
        self.folder_var.set(str(gdb_path))
        try:
            gdb_sources = list_gdb_polygon_layers(gdb_path)
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        if not gdb_sources:
            messagebox.showwarning("提示", "这个 gdb 中没有识别到面图层。")
            return
        before = len(self.discovered_sources)
        self.discovered_sources = merge_source_lists(self.discovered_sources, gdb_sources)
        added = len(self.discovered_sources) - before
        self.discovered_sources.sort(key=source_path_for_log)
        self.source_checklist.set_sources(self.discovered_sources)
        self.log_status(f"已添加 {added} 个 gdb 面图层，当前清单共 {len(self.discovered_sources)} 个。")

    def refresh_sources(self) -> None:
        folder_text = self.folder_var.get().strip()
        if not folder_text:
            messagebox.showwarning("提示", "请先选择或输入一个文件夹。")
            return
        folder = Path(folder_text)
        if not folder.exists():
            messagebox.showwarning("提示", "请先选择有效文件夹。")
            return
        self.discovered_sources, errors = discover_sources_in_folder_with_errors(folder)
        self.discovered_sources.sort(key=source_path_for_log)
        self.source_checklist.set_sources(self.discovered_sources)
        self.log_status(f"发现 {len(self.discovered_sources)} 个可用数据源。")
        for error in errors:
            self.log_status(f"跳过 gdb：{error}")

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

    def analyze_selection(self) -> None:
        sources = self.source_checklist.selected_sources()
        if len(sources) < 2:
            messagebox.showwarning("提示", "至少选择 2 个数据源。")
            return
        try:
            self.log_status("开始提交前检查：统计要素、字段并检查源间空间重叠，请稍等。")
            report = build_preflight_report(sources, self.process_dir)
            field_analysis = report.field_analysis
            projection_infos = report.projection_infos
            same = report.projections_same
        except Exception as exc:
            messagebox.showerror("分析失败", str(exc))
            return
        self.last_preflight_report = report
        self.last_preflight_sources = tuple(sources)
        self.projection_infos = projection_infos
        self.projections_same = same
        self.analyzed_sources = tuple(sources)
        self.reference_combo["values"] = [source_label(source) for source in sources]
        self.reference_file_var.set(source_label(sources[0]))
        self.fill_projection_table(projection_infos)
        if field_analysis.common_fields:
            self.log_status(f"字段名一致的公共字段数量：{len(field_analysis.common_fields)}。")
        else:
            self.log_status("警告：这些数据源没有字段名一致的公共字段。")
        self.log_unmerged_fields(field_analysis)
        if same:
            self.reference_mode.set(0)
            self.reference_file_var.set(source_label(sources[0]))
            self.update_target_projection()
            self.log_status("投影一致，合并时跳过重投影。")
        else:
            self.update_target_projection()
            self.log_status("投影不一致或存在未知投影，请确认基准投影。")
        self.show_preflight_report(report, ask_continue=False)

    def show_preflight_report(self, report: PreflightReport, ask_continue: bool) -> bool:
        window = Toplevel(self.root)
        window.title("提交前检查报告")
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
            ttk.Button(button_row, text="确认无误，继续合并", command=confirm).pack(side=LEFT, padx=5)
            ttk.Button(button_row, text="取消", command=cancel).pack(side=LEFT, padx=5)
            window.transient(self.root)
            window.grab_set()
            self.root.wait_window(window)
            return result["confirmed"]
        ttk.Button(button_row, text="关闭", command=window.destroy).pack(side=LEFT, padx=5)
        return False

    def choose_overlap_priority(self, sources: list[VectorSource], report: PreflightReport) -> list[VectorSource] | None:
        conflict_count = sum(issue.conflicting_overlap_count for issue in report.overlap_issues)
        if conflict_count <= 0:
            return sources
        window = Toplevel(self.root)
        window.title("重叠字段不一致：选择保留优先级")
        window.geometry("920x520")
        ttk.Label(
            window,
            text="检测到公共字段值不一致的重叠区域。排在上面的数据源优先保留，排在下面的数据源会被 Erase 掉已保留区域。",
            wraplength=860,
        ).pack(fill="x", padx=10, pady=8)

        source_list = list(sources)
        listbox = ttk.Treeview(window, columns=("order", "source"), show="headings", height=12, selectmode="browse")
        listbox.heading("order", text="优先级")
        listbox.heading("source", text="数据源")
        listbox.column("order", width=80, anchor="center", stretch=False)
        listbox.column("source", width=780, anchor="w")
        listbox.pack(fill=BOTH, expand=True, padx=10, pady=5)

        def refresh() -> None:
            for item in listbox.get_children():
                listbox.delete(item)
            for index, source in enumerate(source_list, start=1):
                listbox.insert("", END, iid=str(index - 1), values=(index, source_label(source)))

        def selected_index() -> int | None:
            selected = listbox.selection()
            if not selected:
                return None
            return int(selected[0])

        def move(delta: int) -> None:
            index = selected_index()
            if index is None:
                return
            new_index = index + delta
            if new_index < 0 or new_index >= len(source_list):
                return
            source_list[index], source_list[new_index] = source_list[new_index], source_list[index]
            refresh()
            listbox.selection_set(str(new_index))
            listbox.focus(str(new_index))

        result = {"confirmed": False}

        def confirm() -> None:
            result["confirmed"] = True
            window.destroy()

        def cancel() -> None:
            result["confirmed"] = False
            window.destroy()

        button_row = ttk.Frame(window)
        button_row.pack(fill="x", padx=10, pady=8)
        ttk.Button(button_row, text="上移", command=lambda: move(-1)).pack(side=LEFT, padx=5)
        ttk.Button(button_row, text="下移", command=lambda: move(1)).pack(side=LEFT, padx=5)
        ttk.Button(button_row, text="确认优先级并继续", command=confirm).pack(side=LEFT, padx=20)
        ttk.Button(button_row, text="取消", command=cancel).pack(side=LEFT, padx=5)
        refresh()
        if source_list:
            listbox.selection_set("0")
            listbox.focus("0")
        window.transient(self.root)
        window.grab_set()
        self.root.wait_window(window)
        if not result["confirmed"]:
            return None
        return source_list

    def fill_projection_table(self, infos: list[ProjectionInfo]) -> None:
        for item in self.projection_tree.get_children():
            self.projection_tree.delete(item)
        for info in infos:
            self.projection_tree.insert("", END, values=(source_label(info.source), info.message))

    def choose_extra_reference(self) -> None:
        path = filedialog.askopenfilename(title="选择投影参考 shp", filetypes=[("Shapefile", "*.shp")])
        if path:
            self.reference_extra_var.set(path)
            self.reference_mode.set(1)
            self.update_target_projection()

    def update_target_projection(self) -> None:
        target_source = None
        if self.reference_mode.get() == 0:
            value = self.reference_file_var.get().strip()
            for source in self.analyzed_sources:
                if source_label(source) == value:
                    target_source = source
                    break
        else:
            value = self.reference_extra_var.get().strip()
            if value:
                target_source = make_vector_source("shp", Path(value))
        self.target_source = target_source
        self.target_spatial_reference = None
        self.projection_text.delete("1.0", END)
        if target_source is None:
            self.projection_text.insert(END, "尚未选择投影基准。")
            return
        info = read_source_spatial_reference(target_source)
        self.target_spatial_reference = info.spatial_reference
        self.projection_text.insert(END, f"投影来源：{source_label(target_source)}\n\n{projection_text(info.spatial_reference)}")

    def submit_job(self) -> None:
        sources = self.source_checklist.selected_sources()
        if len(sources) < 2:
            messagebox.showwarning("提示", "至少选择 2 个数据源。")
            return
        if self.target_spatial_reference is None or self.target_source is None:
            messagebox.showwarning("提示", "请先分析并确认投影。")
            return
        if tuple(sources) != self.analyzed_sources:
            messagebox.showwarning("提示", "当前勾选的数据源和上次分析结果不一致，请重新点击“分析投影和字段”。")
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
            messagebox.showinfo("Shapefile 字段限制", FIELD_NAME_LIMIT_NOTE)
        else:
            gdb_text = self.output_gdb_var.get().strip()
            feature_name = self.output_feature_var.get().strip()
            if not gdb_text or not feature_name:
                messagebox.showwarning("提示", "请选择 GDB 并填写面要素类名。")
                return
            output_path = Path(gdb_text if gdb_text.lower().endswith(".gdb") else f"{gdb_text}.gdb")
            output_feature_name = arcpy.ValidateTableName(feature_name, str(output_path.parent))
        output_dataset = output_dataset_path(output_kind, output_path, output_feature_name)
        for source in sources:
            if output_dataset.resolve() == Path(source_dataset_path(source)).resolve():
                messagebox.showerror("输出错误", f"输出结果不能覆盖输入数据：{source_label(source)}")
                return
        try:
            report = self.last_preflight_report
            if report is None or tuple(sources) != self.last_preflight_sources:
                self.log_status("当前数据源没有最新提交前检查报告，正在重新检查。")
                report = build_preflight_report(sources, self.process_dir)
                self.last_preflight_report = report
                self.last_preflight_sources = tuple(sources)
            field_analysis = report.field_analysis
            common_fields = field_analysis.common_fields
            if not common_fields:
                messagebox.showerror("字段错误", "这些数据源没有字段名一致的公共字段，无法合并。")
                return
        except Exception as exc:
            messagebox.showerror("提交失败", str(exc))
            return
        if not self.show_preflight_report(report, ask_continue=True):
            self.log_status("用户取消合并任务，未提交。")
            return
        priority_sources = self.choose_overlap_priority(sources, report)
        if priority_sources is None:
            self.log_status("用户取消重叠保留优先级选择，未提交。")
            return
        job = MergeJob(
            job_id=uuid.uuid4().hex[:8],
            input_sources=sources,
            priority_sources=priority_sources,
            output_path=output_path,
            output_feature_name=output_feature_name,
            output_kind=output_kind,
            target_spatial_reference=self.target_spatial_reference,
            target_projection_source=self.target_source,
            projections_same=self.projections_same,
            common_fields=common_fields,
            created_at=now_text(),
            preflight_report=report.text,
        )
        self.job_queue.put(job)
        self.log_status(f"已提交任务 {job.job_id}，后台执行中。你可以继续选择下一组数据。")

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

    def log_unmerged_fields(self, field_analysis: FieldMergeAnalysis) -> None:
        any_missing = False
        for source, fields in field_analysis.missing_by_source.items():
            if not fields:
                continue
            any_missing = True
            names = "、".join(field[0] for field in fields)
            self.log_status(f"未合并字段 - {source_label(source)}：{names}")
        if not any_missing:
            self.log_status("所有字段名都能在选中数据源之间对应。")

    def show_history(self) -> None:
        history_path = self.logs_dir / "merge_vector_history.jsonl"
        window = Toplevel(self.root)
        window.title("历史记录")
        window.geometry("900x520")
        text = Text(window, wrap="word")
        text.pack(fill=BOTH, expand=True, padx=8, pady=8)
        if not history_path.exists():
            text.insert(END, "暂无历史记录。")
            return
        with history_path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    text.insert(END, line + "\n")
                    continue
                text.insert(END, f"任务：{record.get('job_id')}  状态：{record.get('status')}\n")
                text.insert(END, f"开始：{record.get('started_at')}  结束：{record.get('ended_at')}\n")
                text.insert(END, f"输出：{record.get('output_path')}\n")
                text.insert(END, f"记录数：{record.get('merged_count')}  投影：{record.get('target_projection')}\n")
                if record.get("error"):
                    text.insert(END, f"错误：{record.get('error')}\n")
                text.insert(END, f"日志：{record.get('log_path')}\n")
                if record.get("preflight_report"):
                    text.insert(END, "提交前检查报告：\n")
                    text.insert(END, record.get("preflight_report") + "\n")
                text.insert(END, "-" * 100 + "\n")

    def open_logs_folder(self) -> None:
        import os

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.logs_dir)

    def delete_logs(self) -> None:
        if not messagebox.askyesno("确认", "确定删除所有合并日志和历史记录吗？"):
            return
        failed = []
        for path in self.logs_dir.glob("merge_vector_*.log"):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                failed.append(f"{path.name}: {exc}")
        history_path = self.logs_dir / "merge_vector_history.jsonl"
        try:
            history_path.unlink(missing_ok=True)
        except OSError as exc:
            failed.append(f"{history_path.name}: {exc}")
        if failed:
            messagebox.showwarning("部分日志未删除", "\n".join(failed))
            self.log_status("部分日志未删除，可能正在被后台任务使用。")
        else:
            self.log_status("日志和历史记录已删除。")


def main() -> int:
    if ARCPY_IMPORT_ERROR:
        text = f"请先使用 ArcGIS Pro Python/克隆环境运行。本工具缺少 arcpy：{ARCPY_IMPORT_ERROR}"
        try:
            root = Tk()
            root.withdraw()
            messagebox.showerror("缺少依赖", text)
            root.destroy()
        except Exception:
            print(text)
        return 1
    root = Tk()
    MergeArcpyApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
