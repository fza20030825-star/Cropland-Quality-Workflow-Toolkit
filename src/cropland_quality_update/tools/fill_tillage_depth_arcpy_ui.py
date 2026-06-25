"""ArcPy UI tool for filling tillage depth from Sanpu unit maps."""

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
from cropland_quality_update.tools import gb_to_sanpu_arcpy_ui as convert_tool
from cropland_quality_update.tools import membership_arcpy_ui as membership_tool
from cropland_quality_update.tools import vector_common_arcpy as vector_tool


arcpy = membership_tool.arcpy

DEPTH_FIELD = "耕层厚度"
DEPTH_MEMBERSHIP_FIELD = "F耕层厚度"
DEFAULT_OUTPUT_FEATURE = "高标补耕层厚度"
TEMP_HIGH_ID_FIELD = "CQWT_HSID"
TEMP_SANPU_DEPTH_FIELD = "CQWT_TDEP"
DEPTH_FIELD_ALIASES = [
    "耕层厚度",
    "耕层厚",
    "耕作层厚度",
    "耕作层厚",
    "耕层深度",
    "耕作层深度",
    "耕厚",
]


@dataclass(frozen=True)
class DepthBinding:
    field_name: str
    field_type: str
    matched_label: str


@dataclass(frozen=True)
class DepthFillReport:
    ok: bool
    high_source: membership_tool.VectorSource
    sanpu_source: membership_tool.VectorSource
    rule_set: membership_tool.RuleSet
    high_count: int
    sanpu_count: int
    depth_binding: DepthBinding | None
    missing_or_ambiguous: list[str]
    invalid_depth_count: int
    invalid_depth_samples: list[tuple[int, str]]
    text: str


@dataclass(frozen=True)
class DepthFillJob:
    job_id: str
    high_source: membership_tool.VectorSource
    sanpu_source: membership_tool.VectorSource
    output_gdb: Path
    output_feature_name: str
    rule_set: membership_tool.RuleSet
    depth_binding: DepthBinding
    overwrite_existing: bool
    created_at: str
    validation_report: str


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_for_file() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def require_runtime() -> None:
    membership_tool.require_runtime()


def field_name_key(value: object) -> str:
    return convert_tool.compact_key(value)


def source_dataset(source: membership_tool.VectorSource) -> str:
    return membership_tool.source_dataset_path(source)


def field_alias_labels(source: membership_tool.VectorSource, fields: dict[str, object]) -> dict[str, set[str]]:
    return membership_tool.field_labels_by_actual(source, fields)


def find_depth_binding(source: membership_tool.VectorSource) -> tuple[DepthBinding | None, list[str]]:
    dataset = source_dataset(source)
    fields = {field.name: field for field in arcpy.ListFields(dataset) if membership_tool.is_data_field(field)}
    labels = field_alias_labels(source, fields)
    wanted = {field_name_key(name) for name in DEPTH_FIELD_ALIASES}
    candidates: list[tuple[int, str, str]] = []
    for actual_name, field_labels in labels.items():
        for label in field_labels:
            key = field_name_key(label)
            if key in wanted:
                score = 0 if key == field_name_key(DEPTH_FIELD) else 10
                candidates.append((score, actual_name, label))
    if not candidates:
        return None, [f"三普单元图缺少可识别的 {DEPTH_FIELD} 字段。"]
    candidates.sort(key=lambda item: (item[0], item[1]))
    best_score = candidates[0][0]
    best = [item for item in candidates if item[0] == best_score]
    if len({item[1] for item in best}) > 1:
        return None, ["三普单元图耕层厚度字段匹配歧义：" + "、".join(f"{field}({label})" for _score, field, label in best)]
    _, field_name, label = best[0]
    field = fields[field_name]
    return DepthBinding(field_name=field_name, field_type=field.type, matched_label=label), []


def scan_invalid_depth_values(
    source: membership_tool.VectorSource,
    binding: DepthBinding,
) -> tuple[int, list[tuple[int, str]]]:
    dataset = source_dataset(source)
    oid_name = membership_tool.oid_field_name(dataset)
    count = 0
    samples: list[tuple[int, str]] = []
    with arcpy.da.SearchCursor(dataset, [oid_name, binding.field_name]) as cursor:
        for oid, value in cursor:
            if convert_tool.parse_numeric_value(value) is None:
                count += 1
                if len(samples) < membership_tool.ISSUE_DETAIL_LIMIT:
                    samples.append((int(oid), membership_tool.field_value_text(value)))
    return count, samples


def build_report_text(report: DepthFillReport) -> str:
    lines: list[str] = []
    lines.append("三普单元图补耕层厚度审查报告")
    lines.append("=" * 60)
    lines.append(f"高标单元图：{membership_tool.source_label(report.high_source)}")
    lines.append(f"三普单元图：{membership_tool.source_label(report.sanpu_source)}")
    lines.append(f"国标二级农业区：{report.rule_set.area_name}")
    lines.append(f"规则文件：{report.rule_set.rule_path}")
    lines.append(f"高标要素数：{report.high_count}")
    lines.append(f"三普要素数：{report.sanpu_count}")
    lines.append("")
    lines.append("一、三普耕层厚度字段")
    if report.depth_binding:
        lines.append(
            f"   - {DEPTH_FIELD} -> {report.depth_binding.field_name}（{report.depth_binding.field_type}；匹配标签：{report.depth_binding.matched_label}）"
        )
    if report.missing_or_ambiguous:
        lines.extend(f"   - {item}" for item in report.missing_or_ambiguous)
    lines.append("")
    lines.append("二、三普耕层厚度值检查")
    if report.invalid_depth_count:
        lines.append(f"发现无法转成数字的耕层厚度值 {report.invalid_depth_count} 个，不能补充。")
        for oid, value in report.invalid_depth_samples:
            lines.append(f"   - OID={oid}；值={value}")
    else:
        lines.append("三普耕层厚度字段值均可转成数字。")
    lines.append("")
    lines.append("三、输出说明")
    lines.append("输出为高标单元图副本；仅新增或更新“耕层厚度”和“F耕层厚度”，其他高标字段和值保持不变。")
    lines.append("空间对应采用高标图斑与三普单元图最大重叠面积。")
    lines.append("")
    lines.append("审查通过，可以提交补充。" if report.ok else "审查未通过，请先修正输入数据。")
    return "\n".join(lines)


def validate_depth_fill_sources(
    high_source: membership_tool.VectorSource,
    sanpu_source: membership_tool.VectorSource,
    rule_set: membership_tool.RuleSet,
) -> DepthFillReport:
    require_runtime()
    if DEPTH_FIELD not in rule_set.numeric_rules:
        raise RuntimeError(f"当前规则文件缺少 {DEPTH_FIELD} 的数值隶属函数。")
    for label, source in (("高标单元图", high_source), ("三普单元图", sanpu_source)):
        dataset = source_dataset(source)
        if not arcpy.Exists(dataset):
            raise RuntimeError(f"{label}不存在：{membership_tool.source_label(source)}")
        desc = arcpy.Describe(dataset)
        if getattr(desc, "shapeType", "") != "Polygon":
            raise RuntimeError(f"{label}必须是面矢量。")
    high_count = int(arcpy.management.GetCount(source_dataset(high_source))[0])
    sanpu_count = int(arcpy.management.GetCount(source_dataset(sanpu_source))[0])
    binding, issues = find_depth_binding(sanpu_source)
    invalid_count = 0
    invalid_samples: list[tuple[int, str]] = []
    if binding is not None:
        invalid_count, invalid_samples = scan_invalid_depth_values(sanpu_source, binding)
    ok = binding is not None and not issues and invalid_count == 0
    draft = DepthFillReport(
        ok=ok,
        high_source=high_source,
        sanpu_source=sanpu_source,
        rule_set=rule_set,
        high_count=high_count,
        sanpu_count=sanpu_count,
        depth_binding=binding,
        missing_or_ambiguous=issues,
        invalid_depth_count=invalid_count,
        invalid_depth_samples=invalid_samples,
        text="",
    )
    return DepthFillReport(
        ok=draft.ok,
        high_source=draft.high_source,
        sanpu_source=draft.sanpu_source,
        rule_set=draft.rule_set,
        high_count=draft.high_count,
        sanpu_count=draft.sanpu_count,
        depth_binding=draft.depth_binding,
        missing_or_ambiguous=draft.missing_or_ambiguous,
        invalid_depth_count=draft.invalid_depth_count,
        invalid_depth_samples=draft.invalid_depth_samples,
        text=build_report_text(draft),
    )


def unique_temp_field_name(feature_class: str, base_name: str) -> str:
    existing = {field.name.upper() for field in arcpy.ListFields(feature_class)}
    candidate = base_name
    index = 1
    while candidate.upper() in existing:
        candidate = f"{base_name}{index}"
        index += 1
    return candidate


def ensure_double_field(feature_class: str, field_name: str) -> str:
    fields = {field.name.upper(): field for field in arcpy.ListFields(feature_class)}
    existing = fields.get(field_name.upper())
    if existing is not None:
        if existing.type not in membership_tool.NUMERIC_FIELD_TYPES:
            raise RuntimeError(f"输出字段 {field_name} 已存在但不是数值型，不能写入。")
        return existing.name
    arcpy.management.AddField(feature_class, field_name, "DOUBLE", field_alias=field_name)
    return field_name


def copy_high_to_output(job: DepthFillJob) -> str:
    membership_tool.delete_output_dataset(job.output_gdb, job.output_feature_name)
    job.output_gdb.parent.mkdir(parents=True, exist_ok=True)
    if not job.output_gdb.exists():
        arcpy.management.CreateFileGDB(str(job.output_gdb.parent), job.output_gdb.name)
    output_fc = str(job.output_gdb / job.output_feature_name)
    arcpy.conversion.ExportFeatures(source_dataset(job.high_source), output_fc)
    return output_fc


def fill_field_with_oid(feature_class: str, field_name: str) -> None:
    oid_name = membership_tool.oid_field_name(feature_class)
    with arcpy.da.UpdateCursor(feature_class, [oid_name, field_name]) as cursor:
        for oid, _value in cursor:
            cursor.updateRow([oid, int(oid)])


def prepare_sanpu_depth_feature(
    sanpu_source: membership_tool.VectorSource,
    target_spatial_reference: object,
    depth_binding: DepthBinding,
    temp_dir: Path,
    logger: logging.Logger,
) -> str:
    temp_gdb = temp_dir / "sanpu_depth.gdb"
    arcpy.management.CreateFileGDB(str(temp_dir), temp_gdb.name)
    source_path = source_dataset(sanpu_source)
    info = vector_tool.read_source_spatial_reference(sanpu_source)
    copied_fc = str(temp_gdb / "sanpu_depth")
    if info.spatial_reference is not None and vector_tool.spatial_reference_equal(info.spatial_reference, target_spatial_reference):
        arcpy.management.CopyFeatures(source_path, copied_fc)
        logger.info("三普单元图投影一致，直接复制参与叠置。")
    else:
        arcpy.management.Project(source_path, copied_fc, target_spatial_reference)
        logger.info("三普单元图已投影到高标单元图坐标系。")

    depth_temp = unique_temp_field_name(copied_fc, TEMP_SANPU_DEPTH_FIELD)
    arcpy.management.AddField(copied_fc, depth_temp, "DOUBLE", field_alias=depth_temp)
    with arcpy.da.UpdateCursor(copied_fc, [depth_binding.field_name, depth_temp]) as cursor:
        for raw_value, _target in cursor:
            cursor.updateRow([raw_value, convert_tool.parse_numeric_value(raw_value)])
    return copied_fc


def build_depth_by_high_id(
    output_fc: str,
    sanpu_fc: str,
    high_id_field: str,
    sanpu_depth_field: str,
    temp_dir: Path,
    logger: logging.Logger,
) -> tuple[dict[int, float], int]:
    intersect_gdb = temp_dir / "depth_intersect.gdb"
    arcpy.management.CreateFileGDB(str(temp_dir), intersect_gdb.name)
    intersect_fc = str(intersect_gdb / "overlap")
    arcpy.analysis.PairwiseIntersect([output_fc, sanpu_fc], intersect_fc, "ALL")
    best: dict[int, tuple[float, float]] = {}
    with arcpy.da.SearchCursor(intersect_fc, [high_id_field, sanpu_depth_field, "SHAPE@AREA"]) as cursor:
        for high_id, depth, area in cursor:
            if high_id is None or depth is None or not area or area <= 0:
                continue
            high_id = int(high_id)
            area = float(area)
            previous = best.get(high_id)
            if previous is None or area > previous[0]:
                best[high_id] = (area, float(depth))
    logger.info("空间叠置匹配到高标图斑：%s 个。", len(best))
    return {high_id: depth for high_id, (_area, depth) in best.items()}, int(arcpy.management.GetCount(intersect_fc)[0])


def delete_temp_fields(feature_class: str, fields: list[str], logger: logging.Logger) -> None:
    existing = {field.name for field in arcpy.ListFields(feature_class)}
    delete_fields = [field for field in fields if field in existing]
    if delete_fields:
        try:
            arcpy.management.DeleteField(feature_class, delete_fields)
        except Exception as exc:
            logger.warning("临时字段删除失败，可手动删除：%s；%s", delete_fields, exc)


def calculate_depth_fill_output(
    job: DepthFillJob,
    temp_dir: Path,
    logger: logging.Logger,
) -> tuple[str, dict[str, int]]:
    require_runtime()
    arcpy.env.overwriteOutput = True
    output_fc = copy_high_to_output(job)
    depth_field = ensure_double_field(output_fc, DEPTH_FIELD)
    f_depth_field = ensure_double_field(output_fc, DEPTH_MEMBERSHIP_FIELD)
    high_id_field = unique_temp_field_name(output_fc, TEMP_HIGH_ID_FIELD)
    arcpy.management.AddField(output_fc, high_id_field, "LONG", field_alias=high_id_field)
    fill_field_with_oid(output_fc, high_id_field)

    temp_dir.mkdir(parents=True, exist_ok=True)
    sanpu_fc = prepare_sanpu_depth_feature(
        job.sanpu_source,
        arcpy.Describe(output_fc).spatialReference,
        job.depth_binding,
        temp_dir,
        logger,
    )
    sanpu_depth_field = next(
        field.name for field in arcpy.ListFields(sanpu_fc) if field.name.upper().startswith(TEMP_SANPU_DEPTH_FIELD)
    )
    depth_by_high_id, overlap_rows = build_depth_by_high_id(
        output_fc,
        sanpu_fc,
        high_id_field,
        sanpu_depth_field,
        temp_dir,
        logger,
    )

    rule = job.rule_set.numeric_rules[DEPTH_FIELD]
    total = int(arcpy.management.GetCount(output_fc)[0])
    updated = 0
    skipped_existing = 0
    missing_match = 0
    with arcpy.da.UpdateCursor(output_fc, [high_id_field, depth_field, f_depth_field]) as cursor:
        for high_id, old_depth, old_f_depth in cursor:
            depth = depth_by_high_id.get(int(high_id))
            if depth is None:
                missing_match += 1
                continue
            if not job.overwrite_existing and not membership_tool.is_blank_value(old_depth) and not membership_tool.is_blank_value(old_f_depth):
                skipped_existing += 1
                continue
            f_depth = membership_tool.membership_for_numeric(rule, depth)
            cursor.updateRow([high_id, depth, f_depth])
            updated += 1

    delete_temp_fields(output_fc, [high_id_field], logger)
    stats = {
        "total_features": total,
        "updated_features": updated,
        "skipped_existing": skipped_existing,
        "missing_spatial_match": missing_match,
        "intersect_rows": overlap_rows,
    }
    logger.info("补充完成：%s", stats)
    if missing_match:
        logger.warning("有 %s 个高标图斑未与三普单元图叠置匹配，请检查范围或坐标系。", missing_match)
    return output_fc, stats


def setup_job_logger(logs_dir: Path, job_id: str) -> tuple[logging.Logger, Path]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"fill_tillage_depth_{timestamp_for_file()}_{job_id}.log"
    logger = logging.getLogger(f"fill_tillage_depth.{job_id}")
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


class DepthFillWorker(threading.Thread):
    def __init__(self, job_queue: queue.Queue, event_queue: queue.Queue, logs_dir: Path, process_dir: Path):
        super().__init__(daemon=True)
        self.job_queue = job_queue
        self.event_queue = event_queue
        self.logs_dir = logs_dir
        self.process_dir = process_dir
        self.history_path = logs_dir / "fill_tillage_depth_history.jsonl"

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

    def process_job(self, job: DepthFillJob) -> None:
        logger, log_path = setup_job_logger(self.logs_dir, job.job_id)
        temp_dir = self.process_dir / f"fill_tillage_depth_{timestamp_for_file()}_{job.job_id}"
        record = {
            "job_id": job.job_id,
            "created_at": job.created_at,
            "high_source": membership_tool.source_label(job.high_source),
            "sanpu_source": membership_tool.source_label(job.sanpu_source),
            "output_path": str(job.output_gdb / job.output_feature_name),
            "rule_path": str(job.rule_set.rule_path),
            "overwrite_existing": job.overwrite_existing,
            "status": "running",
            "log_path": str(log_path),
        }
        try:
            logger.info("任务开始：%s", job.job_id)
            logger.info("审查报告：\n%s", job.validation_report)
            output_fc, stats = calculate_depth_fill_output(job, temp_dir, logger)
            record.update({"status": "success", "output_path": output_fc, "stats": stats})
            append_history(self.history_path, record)
            self.send(
                "job_done",
                {
                    "job_id": job.job_id,
                    "message": (
                        f"补耕层厚度完成：{output_fc}；更新 {stats['updated_features']} 个；"
                        f"未匹配 {stats['missing_spatial_match']} 个"
                    ),
                    "output_path": output_fc,
                    "log_path": str(log_path),
                },
            )
        except Exception as exc:  # pragma: no cover - shown to user at runtime
            record.update({"status": "failed", "error": str(exc)})
            append_history(self.history_path, record)
            logger.error("任务失败：%s\n%s", exc, traceback.format_exc())
            self.send(
                "job_failed",
                {"job_id": job.job_id, "message": f"补耕层厚度失败：{exc}", "log_path": str(log_path)},
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            close_job_logger(logger)


class TillageDepthFillApp:
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
        self.rules_dir = self.paths.data_dir / "rules"
        self.logs_dir = self.paths.outputs_dir / "logs"
        self.process_dir = self.paths.outputs_dir / "process_files"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.process_dir.mkdir(parents=True, exist_ok=True)

        self.area_options = self.load_area_options()
        self.area_var = StringVar(value=self.area_options[0] if self.area_options else membership_tool.DEFAULT_AREA_NAME)
        self.high_path_var = StringVar()
        self.high_layer_var = StringVar()
        self.sanpu_path_var = StringVar()
        self.sanpu_layer_var = StringVar()
        self.output_gdb_var = StringVar()
        self.output_feature_var = StringVar(value=DEFAULT_OUTPUT_FEATURE)
        self.overwrite_existing_var = IntVar(value=0)
        self.high_gdb_sources: list[membership_tool.VectorSource] = []
        self.sanpu_gdb_sources: list[membership_tool.VectorSource] = []
        self.high_source: membership_tool.VectorSource | None = None
        self.sanpu_source: membership_tool.VectorSource | None = None
        self.last_report: DepthFillReport | None = None
        self.last_report_key: tuple | None = None

        self.job_queue: queue.Queue = queue.Queue()
        self.event_queue: queue.Queue = queue.Queue()
        self.worker = DepthFillWorker(self.job_queue, self.event_queue, self.logs_dir, self.process_dir)
        self.worker.start()

        if not self.embedded:
            self.root.title("辅助工具：三普单元图补耕层厚度")
            self.root.geometry("1120x820")
            self.root.minsize(1040, 720)
        self.build_ui()
        self.root.after(200, self.poll_worker_events)

    def load_area_options(self) -> list[str]:
        options = membership_tool.list_rule_area_options(self.rules_dir)
        if membership_tool.DEFAULT_AREA_NAME in options:
            options.remove(membership_tool.DEFAULT_AREA_NAME)
            options.insert(0, membership_tool.DEFAULT_AREA_NAME)
        return options

    def build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill=BOTH, expand=True)

        rule_frame = ttk.LabelFrame(container, text="1. 规则")
        rule_frame.pack(fill="x", pady=5)
        rule_row = ttk.Frame(rule_frame)
        rule_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(rule_row, text="国标二级农业区").pack(side=LEFT)
        self.area_combo = ttk.Combobox(rule_row, textvariable=self.area_var, state="readonly", values=self.area_options, width=36)
        self.area_combo.pack(side=LEFT, padx=5)
        ttk.Button(rule_row, text="检查规则文件", command=self.check_rule_file).pack(side=LEFT, padx=5)
        ttk.Button(rule_row, text="刷新规则列表", command=self.refresh_area_options).pack(side=LEFT, padx=5)

        high_frame = ttk.LabelFrame(container, text="2. 高标单元图（输出副本将保留原字段和值）")
        high_frame.pack(fill="x", pady=5)
        high_row = ttk.Frame(high_frame)
        high_row.pack(fill="x", padx=5, pady=5)
        ttk.Entry(high_row, textvariable=self.high_path_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(high_row, text="选择 shp", command=lambda: self.choose_shp("high")).pack(side=LEFT, padx=3)
        ttk.Button(high_row, text="选择 gdb", command=lambda: self.choose_gdb("high")).pack(side=LEFT, padx=3)
        high_layer_row = ttk.Frame(high_frame)
        high_layer_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(high_layer_row, text="GDB 图层").pack(side=LEFT)
        self.high_layer_combo = ttk.Combobox(high_layer_row, textvariable=self.high_layer_var, state="readonly", width=80)
        self.high_layer_combo.pack(side=LEFT, fill="x", expand=True, padx=5)
        self.high_layer_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_source_from_layer("high"))

        sanpu_frame = ttk.LabelFrame(container, text="3. 三普单元图（提供耕层厚度字段）")
        sanpu_frame.pack(fill="x", pady=5)
        sanpu_row = ttk.Frame(sanpu_frame)
        sanpu_row.pack(fill="x", padx=5, pady=5)
        ttk.Entry(sanpu_row, textvariable=self.sanpu_path_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(sanpu_row, text="选择 shp", command=lambda: self.choose_shp("sanpu")).pack(side=LEFT, padx=3)
        ttk.Button(sanpu_row, text="选择 gdb", command=lambda: self.choose_gdb("sanpu")).pack(side=LEFT, padx=3)
        sanpu_layer_row = ttk.Frame(sanpu_frame)
        sanpu_layer_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(sanpu_layer_row, text="GDB 图层").pack(side=LEFT)
        self.sanpu_layer_combo = ttk.Combobox(sanpu_layer_row, textvariable=self.sanpu_layer_var, state="readonly", width=80)
        self.sanpu_layer_combo.pack(side=LEFT, fill="x", expand=True, padx=5)
        self.sanpu_layer_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_source_from_layer("sanpu"))

        output_frame = ttk.LabelFrame(container, text="4. 输出位置")
        output_frame.pack(fill="x", pady=5)
        output_row = ttk.Frame(output_frame)
        output_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(output_row, text="输出 GDB").pack(side=LEFT)
        ttk.Entry(output_row, textvariable=self.output_gdb_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(output_row, text="选择已有 gdb", command=self.choose_existing_output_gdb).pack(side=LEFT, padx=3)
        ttk.Button(output_row, text="新建 gdb", command=self.choose_output_gdb).pack(side=LEFT, padx=3)
        ttk.Label(output_row, text="面要素类").pack(side=LEFT, padx=5)
        ttk.Entry(output_row, textvariable=self.output_feature_var, width=28).pack(side=LEFT)
        ttk.Checkbutton(output_frame, text="覆盖已有耕层厚度和 F耕层厚度", variable=self.overwrite_existing_var).pack(anchor="w", padx=10, pady=(0, 5))

        report_frame = ttk.LabelFrame(container, text="5. 审查、补充和日志")
        report_frame.pack(fill="both", expand=True, pady=5)
        action_row = ttk.Frame(report_frame)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="提交补充任务", command=self.submit_job).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="查看历史记录", command=self.show_history).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="打开日志文件夹", command=self.open_logs_folder).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="删除日志", command=self.delete_logs).pack(side=LEFT, padx=5, pady=4)
        if self.shared_status_text is None:
            self.status_text = Text(report_frame, height=22, wrap="word")
            self.status_text.pack(fill="both", expand=True, padx=5, pady=5)
        else:
            self.status_text = self.shared_status_text
            ttk.Label(report_frame, text="运行详细信息显示在窗口底部“详细信息”区域。").pack(anchor="w", padx=5, pady=5)
        self.log_status("三普补耕层厚度工具已启动。请选择高标单元图、三普单元图和输出 GDB，提交时会自动审查。")

    def refresh_area_options(self) -> None:
        self.area_options = self.load_area_options()
        self.area_combo["values"] = self.area_options
        if not self.area_options:
            self.area_var.set("")
            messagebox.showwarning("提示", f"没有在规则目录中找到 *_机器读取规则.xlsx：\n{self.rules_dir}")
            return
        if self.area_var.get() not in self.area_options:
            self.area_var.set(self.area_options[0])
        self.last_report = None
        self.log_status(f"已刷新规则列表：{len(self.area_options)} 个。")

    def check_rule_file(self) -> None:
        if not self.area_var.get().strip():
            messagebox.showwarning("提示", "请先选择国标二级农业区。")
            return
        try:
            rule_set = membership_tool.load_rule_set(self.rules_dir, self.area_var.get())
            if DEPTH_FIELD not in rule_set.numeric_rules:
                raise RuntimeError(f"规则文件缺少 {DEPTH_FIELD} 的数值隶属函数。")
        except Exception as exc:
            messagebox.showerror("规则文件错误", str(exc))
            return
        messagebox.showinfo("规则文件", f"已读取规则文件：\n{rule_set.rule_path}\n\n可计算 {DEPTH_MEMBERSHIP_FIELD}")
        self.log_status(f"规则文件检查通过：{rule_set.rule_path}")

    def choose_shp(self, role: str) -> None:
        path = filedialog.askopenfilename(title="选择输入 shp", filetypes=[("Shapefile", "*.shp")])
        if not path:
            return
        source = membership_tool.make_vector_source("shp", Path(path))
        self.set_source(role, source, [])

    def choose_gdb(self, role: str) -> None:
        path = filedialog.askdirectory(title="选择输入 .gdb 文件夹")
        if not path:
            return
        gdb_path = membership_tool.find_nearest_gdb_path(Path(path))
        if gdb_path is None or not membership_tool.is_gdb_path(gdb_path):
            messagebox.showwarning("提示", "请选择一个 .gdb 文件夹。")
            return
        try:
            sources = vector_tool.list_gdb_polygon_layers(gdb_path)
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        if not sources:
            messagebox.showwarning("提示", "这个 gdb 中没有识别到面图层。")
            return
        self.set_source(role, sources[0], sources)

    def set_source(self, role: str, source: membership_tool.VectorSource, sources: list[membership_tool.VectorSource]) -> None:
        if role == "high":
            self.high_source = source
            self.high_gdb_sources = sources
            self.high_path_var.set(str(source.source_path))
            self.high_layer_var.set(source.layer_name or "")
            self.high_layer_combo["values"] = [item.layer_name for item in sources]
            label = "高标单元图"
        else:
            self.sanpu_source = source
            self.sanpu_gdb_sources = sources
            self.sanpu_path_var.set(str(source.source_path))
            self.sanpu_layer_var.set(source.layer_name or "")
            self.sanpu_layer_combo["values"] = [item.layer_name for item in sources]
            label = "三普单元图"
        self.last_report = None
        self.log_status(f"已选择{label}：{source.display_name}")

    def update_source_from_layer(self, role: str) -> None:
        if role == "high":
            layer = self.high_layer_var.get()
            for source in self.high_gdb_sources:
                if source.layer_name == layer:
                    self.set_source("high", source, self.high_gdb_sources)
                    return
        else:
            layer = self.sanpu_layer_var.get()
            for source in self.sanpu_gdb_sources:
                if source.layer_name == layer:
                    self.set_source("sanpu", source, self.sanpu_gdb_sources)
                    return

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
        gdb_path = membership_tool.find_nearest_gdb_path(Path(path))
        if gdb_path is None or not membership_tool.is_gdb_path(gdb_path):
            messagebox.showwarning("提示", "请选择一个已有的 .gdb 文件夹。")
            return
        self.output_gdb_var.set(str(gdb_path))
        self.log_status(f"已选择输出 GDB：{gdb_path}")

    def report_key(self) -> tuple:
        return (
            membership_tool.source_label(self.high_source) if self.high_source else "",
            membership_tool.source_label(self.sanpu_source) if self.sanpu_source else "",
            self.area_var.get(),
            self.overwrite_existing_var.get(),
        )

    def validate_current_input(self, show_report: bool = True):
        if self.high_source is None:
            messagebox.showwarning("提示", "请先选择高标单元图。")
            return None
        if self.sanpu_source is None:
            messagebox.showwarning("提示", "请先选择三普单元图。")
            return None
        if not self.area_var.get().strip():
            messagebox.showwarning("提示", "请先选择国标二级农业区。")
            return None
        try:
            rule_set = membership_tool.load_rule_set(self.rules_dir, self.area_var.get())
            self.log_status("正在进行提交前审查：三普耕层厚度字段和值。")
            report = validate_depth_fill_sources(self.high_source, self.sanpu_source, rule_set)
        except Exception as exc:
            messagebox.showerror("审查失败", str(exc))
            return None
        self.last_report = report
        self.last_report_key = self.report_key()
        if show_report:
            self.show_report(report, ask_continue=False)
            self.log_status("审查通过，可以提交补充任务。" if report.ok else "审查未通过，已在报告中列出问题。")
        return report

    def show_report(self, report: DepthFillReport, ask_continue: bool) -> bool:
        window = Toplevel(self.root)
        window.title("三普补耕层厚度审查报告")
        window.geometry("1000x700")
        text_frame = ttk.Frame(window)
        text_frame.pack(fill=BOTH, expand=True, padx=8, pady=8)
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        text = Text(text_frame, wrap="word")
        scroll = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        text.insert(END, report.text)
        text.configure(state="disabled")
        result = {"confirmed": False}
        buttons = ttk.Frame(window)
        buttons.pack(fill="x", padx=8, pady=8)

        def confirm() -> None:
            result["confirmed"] = True
            window.destroy()

        def cancel() -> None:
            result["confirmed"] = False
            window.destroy()

        if ask_continue:
            ttk.Button(buttons, text="确认无误，继续补充", command=confirm).pack(side=LEFT, padx=5)
            ttk.Button(buttons, text="取消", command=cancel).pack(side=LEFT, padx=5)
            window.transient(self.root)
            window.grab_set()
            self.root.wait_window(window)
            return result["confirmed"]
        ttk.Button(buttons, text="关闭", command=window.destroy).pack(side=LEFT, padx=5)
        return False

    def output_settings(self) -> tuple[Path, str] | None:
        gdb_text = self.output_gdb_var.get().strip()
        feature_name = self.output_feature_var.get().strip()
        if not gdb_text or not feature_name:
            messagebox.showwarning("提示", "请选择输出 GDB 并填写面要素类名。")
            return None
        output_gdb = Path(gdb_text if gdb_text.lower().endswith(".gdb") else f"{gdb_text}.gdb")
        output_feature_name = membership_tool.validate_gdb_feature_name(feature_name, output_gdb)
        if output_feature_name != feature_name:
            self.output_feature_var.set(output_feature_name)
            self.log_status(f"输出要素类名已按 FileGDB 规则修正为：{output_feature_name}")
        return output_gdb, output_feature_name

    def submit_job(self) -> None:
        if self.high_source is None or self.sanpu_source is None:
            messagebox.showwarning("提示", "请先选择高标单元图和三普单元图。")
            return
        output = self.output_settings()
        if output is None:
            return
        output_gdb, output_feature_name = output
        output_dataset = output_gdb / output_feature_name
        if output_dataset.resolve() == Path(source_dataset(self.high_source)).resolve():
            messagebox.showerror("输出错误", "输出结果不能覆盖输入高标数据。")
            return
        try:
            report = self.last_report
            if report is None or self.last_report_key != self.report_key():
                self.log_status("当前输入没有最新审查报告，正在进行提交前自动审查。")
                report = self.validate_current_input(show_report=False)
            if report is None:
                return
            if not report.ok or report.depth_binding is None:
                self.show_report(report, ask_continue=False)
                self.log_status("提交前审查未通过，任务未提交。")
                return
        except Exception as exc:
            messagebox.showerror("提交失败", str(exc))
            return
        if not self.show_report(report, ask_continue=True):
            self.log_status("用户取消补充任务，未提交。")
            return
        job = DepthFillJob(
            job_id=uuid.uuid4().hex[:8],
            high_source=self.high_source,
            sanpu_source=self.sanpu_source,
            output_gdb=output_gdb,
            output_feature_name=output_feature_name,
            rule_set=report.rule_set,
            depth_binding=report.depth_binding,
            overwrite_existing=bool(self.overwrite_existing_var.get()),
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
        history_path = self.logs_dir / "fill_tillage_depth_history.jsonl"
        if not history_path.exists():
            messagebox.showinfo("历史记录", "暂无历史记录。")
            return
        window = Toplevel(self.root)
        window.title("三普补耕层厚度历史记录")
        window.geometry("980x620")
        frame = ttk.Frame(window)
        frame.pack(fill=BOTH, expand=True, padx=8, pady=8)
        text = Text(frame, wrap="word")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=LEFT, fill="y")
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
                f"高标：{record.get('high_source')}\n"
                f"三普：{record.get('sanpu_source')}\n"
                f"输出：{record.get('output_path')}\n"
                f"规则：{record.get('rule_path')}\n"
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
        if not messagebox.askyesno("确认删除", "确定删除三普补耕层厚度日志和历史记录吗？"):
            return
        deleted = 0
        for path in self.logs_dir.glob("fill_tillage_depth_*.log"):
            path.unlink()
            deleted += 1
        history_path = self.logs_dir / "fill_tillage_depth_history.jsonl"
        if history_path.exists():
            history_path.unlink()
            deleted += 1
        self.log_status(f"已删除 {deleted} 个三普补耕层厚度日志/历史文件。")

    def reset_inputs(self) -> None:
        self.area_options = self.load_area_options()
        if hasattr(self, "area_combo"):
            self.area_combo["values"] = self.area_options
        self.area_var.set(self.area_options[0] if self.area_options else membership_tool.DEFAULT_AREA_NAME)
        self.high_path_var.set("")
        self.high_layer_var.set("")
        self.sanpu_path_var.set("")
        self.sanpu_layer_var.set("")
        self.output_gdb_var.set("")
        self.output_feature_var.set(DEFAULT_OUTPUT_FEATURE)
        self.overwrite_existing_var.set(0)
        self.high_gdb_sources = []
        self.sanpu_gdb_sources = []
        self.high_source = None
        self.sanpu_source = None
        self.last_report = None
        self.last_report_key = None
        if hasattr(self, "high_layer_combo"):
            self.high_layer_combo["values"] = []
        if hasattr(self, "sanpu_layer_combo"):
            self.sanpu_layer_combo["values"] = []
        self.log_status("三普补耕层厚度工具输入和参数已恢复为启动默认值。")


def main() -> int:
    try:
        require_runtime()
    except Exception as exc:
        root = Tk()
        root.withdraw()
        messagebox.showerror("缺少运行环境", str(exc))
        return 1
    root = Tk()
    TillageDepthFillApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
