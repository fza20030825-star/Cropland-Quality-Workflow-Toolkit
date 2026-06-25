"""Auxiliary ArcPy UI tool for recalculating membership scores in third-step results."""

from __future__ import annotations

import json
import logging
import queue
import threading
import traceback
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk

from cropland_quality_update.paths import resolve_paths
from cropland_quality_update.tools import gb_to_sanpu_arcpy_ui as convert_tool
from cropland_quality_update.tools import membership_arcpy_ui as membership_tool
from cropland_quality_update.tools import update_scores_arcpy_ui as score_tool
from cropland_quality_update.tools import vector_common_arcpy as vector_tool


arcpy = score_tool.arcpy
ARCPY_IMPORT_ERROR = score_tool.ARCPY_IMPORT_ERROR

VectorSource = score_tool.VectorSource
OUTPUT_FIELD_NAMES = score_tool.OUTPUT_FIELD_NAMES
RESULT_SCORE_FIELD = score_tool.RESULT_SCORE_FIELD
RESULT_GRADE_FIELD = score_tool.RESULT_GRADE_FIELD
DEFAULT_OUTPUT_FEATURE = "重算隶属度结果"


@dataclass(frozen=True)
class RecalculateIssue:
    indicator: str
    field_name: str
    value_text: str
    count: int
    sample_oids: list[int]


@dataclass(frozen=True)
class RecalculateReport:
    ok: bool
    source: VectorSource
    rule_set: membership_tool.RuleSet
    feature_count: int
    schema_errors: list[str]
    type_errors: list[str]
    blank_issues: list[RecalculateIssue]
    invalid_issues: list[RecalculateIssue]
    text: str


@dataclass(frozen=True)
class RecalculateJob:
    job_id: str
    source: VectorSource
    output_gdb: Path
    output_feature_name: str
    rule_set: membership_tool.RuleSet
    created_at: str
    validation_report: str


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_for_file() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def require_runtime() -> None:
    if arcpy is None:
        raise RuntimeError(f"缺少运行包：arcpy ({ARCPY_IMPORT_ERROR})")
    membership_tool.require_runtime()


def source_dataset_path(source: VectorSource) -> str:
    return score_tool.source_dataset_path(source)


def source_label(source: VectorSource) -> str:
    return score_tool.source_label(source)


def output_dataset_path(output_kind: str, output_path: Path, output_feature_name: str | None) -> Path:
    return output_path if output_kind == "shp" else output_path / str(output_feature_name)


def field_info(feature_class: str, field_name: str):
    wanted = field_name.upper()
    for field in arcpy.ListFields(feature_class):
        if field.name.upper() == wanted:
            return field
    raise RuntimeError(f"字段不存在：{field_name}")


def fixed_field_map(feature_class: str) -> dict[str, str]:
    score_tool.audit_output_schema(feature_class)
    return {name: score_tool.field_name_case_insensitive(feature_class, name) for name in OUTPUT_FIELD_NAMES}


def required_raw_indicators(rule_set: membership_tool.RuleSet) -> list[str]:
    return [indicator for indicator in membership_tool.STANDARD_INDICATOR_FIELDS if indicator in rule_set.indicators]


def type_errors_for_source(feature_class: str, field_map: dict[str, str], rule_set: membership_tool.RuleSet) -> list[str]:
    errors: list[str] = []
    for indicator in required_raw_indicators(rule_set):
        field = field_info(feature_class, field_map[indicator])
        if indicator in rule_set.concept_memberships:
            if field.type not in membership_tool.TEXT_FIELD_TYPES:
                errors.append(f"概念指标字段必须为文本型：{indicator} -> {field.name}（实际 {field.type}）")
        elif field.type not in membership_tool.NUMERIC_FIELD_TYPES:
            errors.append(f"数值指标字段必须为数值型：{indicator} -> {field.name}（实际 {field.type}）")
    return errors


def scan_recalculate_issues(
    source: VectorSource,
    field_map: dict[str, str],
    rule_set: membership_tool.RuleSet,
) -> tuple[list[RecalculateIssue], list[RecalculateIssue]]:
    dataset = source_dataset_path(source)
    oid_name = membership_tool.oid_field_name(dataset)
    indicators = required_raw_indicators(rule_set)
    fields = [oid_name, *[field_map[indicator] for indicator in indicators]]
    blanks: dict[tuple[str, str], list[int]] = defaultdict(list)
    invalids: dict[tuple[str, str, str], list[int]] = defaultdict(list)

    with arcpy.da.SearchCursor(dataset, fields) as cursor:
        for row in cursor:
            oid = int(row[0])
            for indicator, value in zip(indicators, row[1:]):
                field_name = field_map[indicator]
                if indicator in rule_set.concept_memberships:
                    normalized = convert_tool.normalize_concept_value(rule_set, indicator, value)
                    if normalized is None:
                        if membership_tool.is_blank_value(value):
                            blanks[(indicator, field_name)].append(oid)
                        else:
                            invalids[(indicator, field_name, membership_tool.field_value_text(value))].append(oid)
                    continue
                numeric_value = convert_tool.parse_numeric_value(value)
                if numeric_value is None:
                    if membership_tool.is_blank_value(value):
                        blanks[(indicator, field_name)].append(oid)
                    else:
                        invalids[(indicator, field_name, membership_tool.field_value_text(value))].append(oid)

    blank_issues = [
        RecalculateIssue(indicator, field_name, "", len(oids), oids[: membership_tool.ISSUE_DETAIL_LIMIT])
        for (indicator, field_name), oids in sorted(blanks.items())
    ]
    invalid_issues = [
        RecalculateIssue(indicator, field_name, value_text, len(oids), oids[: membership_tool.ISSUE_DETAIL_LIMIT])
        for (indicator, field_name, value_text), oids in sorted(invalids.items())
    ]
    return blank_issues, invalid_issues


def build_report_text(report: RecalculateReport) -> str:
    lines: list[str] = []
    lines.append("辅助工具：第三步结果重算隶属度审查报告")
    lines.append("=" * 60)
    lines.append(f"输入数据：{source_label(report.source)}")
    lines.append(f"国标二级农业区：{report.rule_set.area_name}")
    lines.append(f"规则文件：{report.rule_set.rule_path}")
    lines.append(f"面要素数：{report.feature_count}")
    lines.append("")
    lines.append("一、固定字段结构")
    if report.schema_errors:
        lines.append("字段结构不符合第三步固定 39 字段要求，已打回：")
        lines.extend(f"   - {item}" for item in report.schema_errors)
    else:
        lines.append("输入符合第三步固定 39 字段结构。")
    lines.append("")
    lines.append("二、字段类型检查")
    if report.type_errors:
        lines.append("发现字段类型不符合规则，已打回：")
        lines.extend(f"   - {item}" for item in report.type_errors)
    else:
        lines.append("参与重算的原始指标字段类型检查通过。")
    lines.append("")
    lines.append("三、空值检查")
    blank_total = sum(issue.count for issue in report.blank_issues)
    if blank_total:
        lines.append(f"发现参与重算的必需原始指标空值 {blank_total} 个，不能重算。")
        for issue in report.blank_issues:
            sample = "、".join(str(oid) for oid in issue.sample_oids)
            lines.append(f"   - {issue.indicator} -> {issue.field_name}: {issue.count} 个；样例 OID：{sample}")
    else:
        lines.append("未发现参与重算的必需原始指标空值。")
    lines.append("")
    lines.append("四、类别/数值合法性检查")
    invalid_total = sum(issue.count for issue in report.invalid_issues)
    if invalid_total:
        lines.append(f"发现无法按规则计算的原始指标值 {invalid_total} 个，不能重算。")
        for issue in report.invalid_issues:
            sample = "、".join(str(oid) for oid in issue.sample_oids)
            lines.append(f"   - {issue.indicator} -> {issue.field_name}: 值“{issue.value_text}”共 {issue.count} 个；样例 OID：{sample}")
            if issue.indicator in report.rule_set.concept_memberships:
                allowed = "、".join(sorted(report.rule_set.concept_memberships[issue.indicator]))
                lines.append(f"     允许值：{allowed}")
    else:
        lines.append("类别值和数值字段均可按规则计算。")
    lines.append("")
    lines.append("五、输出说明")
    lines.append("输出为新的 GDB 面要素类，不修改输入数据。")
    lines.append("基础字段和 13 个原始指标按输入保留；13 个 F 隶属度、评价得分和质量等级按所选国标二级农业区重新计算。")
    lines.append("非海拔必需农业区中，海拔高度和 F海拔高度允许为空。")
    lines.append("")
    lines.append("审查通过，可以提交重算任务。" if report.ok else "审查未通过，请先修正输入字段或值。")
    return "\n".join(lines)


def validate_recalculate_source(source: VectorSource, rule_set: membership_tool.RuleSet) -> RecalculateReport:
    require_runtime()
    dataset = source_dataset_path(source)
    if not arcpy.Exists(dataset):
        raise RuntimeError(f"输入数据不存在：{source_label(source)}")
    desc = arcpy.Describe(dataset)
    if getattr(desc, "shapeType", "") != "Polygon":
        raise RuntimeError("输入数据必须是面矢量。")
    feature_count = int(arcpy.management.GetCount(dataset)[0])

    schema_errors: list[str] = []
    field_map: dict[str, str] = {}
    try:
        field_map = fixed_field_map(dataset)
    except Exception as exc:
        schema_errors.append(str(exc))

    type_errors: list[str] = []
    blank_issues: list[RecalculateIssue] = []
    invalid_issues: list[RecalculateIssue] = []
    if not schema_errors:
        type_errors = type_errors_for_source(dataset, field_map, rule_set)
    if not schema_errors and not type_errors:
        blank_issues, invalid_issues = scan_recalculate_issues(source, field_map, rule_set)

    ok = not schema_errors and not type_errors and not blank_issues and not invalid_issues
    draft = RecalculateReport(
        ok=ok,
        source=source,
        rule_set=rule_set,
        feature_count=feature_count,
        schema_errors=schema_errors,
        type_errors=type_errors,
        blank_issues=blank_issues,
        invalid_issues=invalid_issues,
        text="",
    )
    return RecalculateReport(
        ok=draft.ok,
        source=draft.source,
        rule_set=draft.rule_set,
        feature_count=draft.feature_count,
        schema_errors=draft.schema_errors,
        type_errors=draft.type_errors,
        blank_issues=draft.blank_issues,
        invalid_issues=draft.invalid_issues,
        text=build_report_text(draft),
    )


def recalculated_values(rule_set: membership_tool.RuleSet, raw_values: dict[str, object]) -> tuple[dict[str, float | None], float, int]:
    membership_values: dict[str, float | None] = {}
    score = 0.0
    for indicator in membership_tool.STANDARD_INDICATOR_FIELDS:
        if indicator not in rule_set.indicators:
            membership_values[indicator] = None
            continue
        source_value = raw_values.get(indicator)
        if indicator in rule_set.concept_memberships:
            category = convert_tool.normalize_concept_value(rule_set, indicator, source_value)
            if category is None:
                raise RuntimeError(f"{indicator} 存在无法计算的类别值：{source_value}")
            membership = rule_set.concept_memberships[indicator][category]
        else:
            numeric_value = convert_tool.parse_numeric_value(source_value)
            if numeric_value is None:
                raise RuntimeError(f"{indicator} 存在无法计算的数值：{source_value}")
            membership = membership_tool.membership_for_numeric(rule_set.numeric_rules[indicator], numeric_value)
        membership_values[indicator] = membership
        score += membership * rule_set.weights[indicator]
    grade = membership_tool.grade_for_score(score, rule_set.grade_rules)
    return membership_values, score, grade


def create_output_feature_class(job: RecalculateJob) -> tuple[str, dict[str, str]]:
    membership_tool.delete_output_dataset(job.output_gdb, job.output_feature_name)
    job.output_gdb.parent.mkdir(parents=True, exist_ok=True)
    if not job.output_gdb.exists():
        arcpy.management.CreateFileGDB(str(job.output_gdb.parent), job.output_gdb.name)
    source_dataset = source_dataset_path(job.source)
    spatial_reference = arcpy.Describe(source_dataset).spatialReference
    output_fc = str(job.output_gdb / job.output_feature_name)
    arcpy.management.CreateFeatureclass(str(job.output_gdb), job.output_feature_name, "POLYGON", spatial_reference=spatial_reference)
    source_types = {field.name: field.type for field in arcpy.ListFields(source_dataset)}
    field_map = score_tool.add_ordered_output_fields(output_fc, source_types)
    score_tool.audit_output_schema(output_fc)
    return output_fc, field_map


def calculate_recalculated_output(job: RecalculateJob, logger: logging.Logger) -> tuple[str, int, dict[str, int]]:
    require_runtime()
    arcpy.env.overwriteOutput = True
    source_dataset = source_dataset_path(job.source)
    source_field_map = fixed_field_map(source_dataset)
    output_fc, output_field_map = create_output_feature_class(job)
    output_fields_by_name = {field.name: field for field in arcpy.ListFields(output_fc)}

    read_fields = ["SHAPE@", *[source_field_map[name] for name in OUTPUT_FIELD_NAMES]]
    insert_fields = ["SHAPE@", *[output_field_map[name] for name in OUTPUT_FIELD_NAMES]]
    recalculated = 0
    with arcpy.da.SearchCursor(source_dataset, read_fields) as search_cursor, arcpy.da.InsertCursor(output_fc, insert_fields) as insert_cursor:
        for row in search_cursor:
            values_by_name = {name: value for name, value in zip(OUTPUT_FIELD_NAMES, row[1:])}
            raw_values = {indicator: values_by_name.get(indicator) for indicator in membership_tool.STANDARD_INDICATOR_FIELDS}
            membership_values, score, grade = recalculated_values(job.rule_set, raw_values)
            for indicator in membership_tool.STANDARD_INDICATOR_FIELDS:
                values_by_name[f"F{indicator}"] = membership_values[indicator]
            values_by_name[RESULT_SCORE_FIELD] = score
            values_by_name[RESULT_GRADE_FIELD] = grade
            output_values = [
                score_tool.coerce_value_for_field(values_by_name.get(name), output_fields_by_name[output_field_map[name]])
                for name in OUTPUT_FIELD_NAMES
            ]
            insert_cursor.insertRow([row[0], *output_values])
            recalculated += 1

    check_fields = score_tool.required_update_field_names()
    ok, audit_text, audit_stats = score_tool.build_blank_result_report(output_fc, check_fields, "重算后必需耕评字段完整性审计")
    logger.info("输出完整性审计：\n%s", audit_text)
    if not ok:
        raise RuntimeError(f"重算后仍有必需耕评字段空值：{audit_stats['missing_values']} 个。详情见日志。")
    score_tool.audit_output_schema(output_fc)
    logger.info("输出字段结构审计通过。")
    logger.info("重算完成：%s；要素数：%s", output_fc, recalculated)
    return output_fc, recalculated, audit_stats


def setup_job_logger(logs_dir: Path, job_id: str) -> tuple[logging.Logger, Path]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"recalculate_scores_{timestamp_for_file()}_{job_id}.log"
    logger = logging.getLogger(f"recalculate_scores_arcpy.{job_id}")
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


class RecalculateWorker(threading.Thread):
    def __init__(self, job_queue: queue.Queue, event_queue: queue.Queue, logs_dir: Path):
        super().__init__(daemon=True)
        self.job_queue = job_queue
        self.event_queue = event_queue
        self.logs_dir = logs_dir
        self.history_path = logs_dir / "recalculate_scores_history.jsonl"

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

    def process_job(self, job: RecalculateJob) -> None:
        logger, log_path = setup_job_logger(self.logs_dir, job.job_id)
        output_path = str(job.output_gdb / job.output_feature_name)
        record = {
            "job_id": job.job_id,
            "created_at": job.created_at,
            "started_at": now_text(),
            "ended_at": None,
            "status": "running",
            "input_source": source_label(job.source),
            "output_path": output_path,
            "rule_path": str(job.rule_set.rule_path),
            "validation_report": job.validation_report,
            "log_path": str(log_path),
            "error": None,
            "recalculated_count": None,
            "audit_stats": None,
        }
        self.send("job_started", {"job_id": job.job_id, "message": "开始重算隶属度字段", "log_path": str(log_path)})
        try:
            logger.info("任务开始：%s", job.job_id)
            logger.info("输入数据：%s", source_label(job.source))
            logger.info("输出目标：%s", output_path)
            logger.info("规则文件：%s", job.rule_set.rule_path)
            if job.validation_report:
                logger.info("提交前审查报告：\n%s", job.validation_report)
            output_fc, recalculated_count, audit_stats = calculate_recalculated_output(job, logger)
            record.update({"status": "success", "recalculated_count": recalculated_count, "audit_stats": audit_stats})
            self.send(
                "job_done",
                {
                    "job_id": job.job_id,
                    "message": f"重算完成：{output_fc}；要素数 {recalculated_count}",
                    "output_path": output_fc,
                    "log_path": str(log_path),
                    "recalculated_count": recalculated_count,
                },
            )
        except Exception as exc:
            error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            logger.error(error_text)
            record.update({"status": "failed", "error": str(exc)})
            self.send("job_failed", {"job_id": job.job_id, "message": f"重算失败：{exc}", "log_path": str(log_path)})
        finally:
            record["ended_at"] = now_text()
            append_history(self.history_path, record)
            close_job_logger(logger)


class RecalculateScoresApp:
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
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.area_options = self.load_area_options()
        self.area_var = StringVar(value=self.area_options[0] if self.area_options else membership_tool.DEFAULT_AREA_NAME)
        self.source_path_var = StringVar()
        self.source_layer_var = StringVar()
        self.output_gdb_var = StringVar()
        self.output_feature_var = StringVar(value=DEFAULT_OUTPUT_FEATURE)
        self.gdb_sources: list[VectorSource] = []
        self.source: VectorSource | None = None
        self.last_report: RecalculateReport | None = None
        self.last_report_key: tuple | None = None

        self.job_queue: queue.Queue = queue.Queue()
        self.event_queue: queue.Queue = queue.Queue()
        self.worker = RecalculateWorker(self.job_queue, self.event_queue, self.logs_dir)
        self.worker.start()

        if not self.embedded:
            self.root.title("辅助工具：第三步结果重算隶属度")
            self.root.geometry("1120x760")
            self.root.minsize(1040, 700)
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

        input_frame = ttk.LabelFrame(container, text="1. 输入数据和规则")
        input_frame.pack(fill="x", pady=5)
        area_row = ttk.Frame(input_frame)
        area_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(area_row, text="国标二级农业区").pack(side=LEFT)
        self.area_combo = ttk.Combobox(area_row, textvariable=self.area_var, state="readonly", values=self.area_options, width=36)
        self.area_combo.pack(side=LEFT, padx=5)
        ttk.Button(area_row, text="检查规则文件", command=self.check_rule_file).pack(side=LEFT, padx=5)
        ttk.Button(area_row, text="刷新规则列表", command=self.refresh_area_options).pack(side=LEFT, padx=5)

        source_row = ttk.Frame(input_frame)
        source_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(source_row, text="修改后的第三步结果 GDB").pack(side=LEFT)
        ttk.Entry(source_row, textvariable=self.source_path_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(source_row, text="选择 gdb", command=self.choose_gdb).pack(side=LEFT, padx=3)

        layer_row = ttk.Frame(input_frame)
        layer_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(layer_row, text="GDB 图层").pack(side=LEFT)
        self.layer_combo = ttk.Combobox(layer_row, textvariable=self.source_layer_var, state="readonly", width=70)
        self.layer_combo.pack(side=LEFT, fill="x", expand=True, padx=5)
        self.layer_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_source_from_layer())

        output_frame = ttk.LabelFrame(container, text="2. 输出位置")
        output_frame.pack(fill="x", pady=5)
        output_row = ttk.Frame(output_frame)
        output_row.pack(fill="x", padx=5, pady=5)
        ttk.Label(output_row, text="输出 GDB").pack(side=LEFT)
        ttk.Entry(output_row, textvariable=self.output_gdb_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(output_row, text="选择已有 gdb", command=self.choose_existing_output_gdb).pack(side=LEFT, padx=3)
        ttk.Button(output_row, text="新建 gdb", command=self.choose_output_gdb).pack(side=LEFT, padx=3)
        ttk.Label(output_row, text="面要素类").pack(side=LEFT, padx=5)
        ttk.Entry(output_row, textvariable=self.output_feature_var, width=28).pack(side=LEFT)

        report_frame = ttk.LabelFrame(container, text="3. 审查、重算和日志")
        report_frame.pack(fill="both", expand=True, pady=5)
        action_row = ttk.Frame(report_frame)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="提交重算任务", command=self.submit_job).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="查看历史记录", command=self.show_history).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="打开日志文件夹", command=self.open_logs_folder).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="删除日志", command=self.delete_logs).pack(side=LEFT, padx=5, pady=4)
        if self.shared_status_text is None:
            self.status_text = Text(report_frame, height=24, wrap="word")
            self.status_text.pack(fill="both", expand=True, padx=5, pady=5)
        else:
            self.status_text = self.shared_status_text
            ttk.Label(report_frame, text="运行详细信息显示在窗口底部“详细信息”区域。").pack(anchor="w", padx=5, pady=5)
        self.log_status("重算隶属度辅助工具已启动。请选择修改后的第三步结果、农业区和输出 GDB，提交时会自动审查。")

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
        except Exception as exc:
            messagebox.showerror("规则文件错误", str(exc))
            return
        messagebox.showinfo("规则文件", f"已读取规则文件：\n{rule_set.rule_path}\n\n指标数：{len(rule_set.weights)}")
        self.log_status(f"规则文件检查通过：{rule_set.rule_path}")

    def choose_gdb(self) -> None:
        path = filedialog.askdirectory(title="选择输入 .gdb 文件夹")
        if not path:
            return
        gdb_path = membership_tool.find_nearest_gdb_path(Path(path))
        if gdb_path is None or not membership_tool.is_gdb_path(gdb_path):
            messagebox.showwarning("提示", "请选择一个 .gdb 文件夹。")
            return
        try:
            self.gdb_sources = vector_tool.list_gdb_polygon_layers(gdb_path)
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        if not self.gdb_sources:
            messagebox.showwarning("提示", "这个 gdb 中没有识别到面图层。")
            return
        self.source_path_var.set(str(gdb_path))
        layer_names = [source.layer_name for source in self.gdb_sources]
        self.layer_combo["values"] = layer_names
        self.source_layer_var.set(layer_names[0])
        self.update_source_from_layer()
        self.log_status(f"已读取 GDB 面图层 {len(self.gdb_sources)} 个。")

    def update_source_from_layer(self) -> None:
        value = self.source_layer_var.get()
        for source in self.gdb_sources:
            if source.layer_name == value:
                self.source = source
                self.last_report = None
                self.log_status(f"已选择输入图层：{source.display_name}")
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
            source_label(self.source) if self.source else "",
            self.area_var.get(),
            self.output_gdb_var.get(),
            self.output_feature_var.get(),
        )

    def validate_current_input(self, show_report: bool = True):
        if self.source is None:
            messagebox.showwarning("提示", "请先选择修改后的第三步结果。")
            return None
        if not self.area_var.get().strip():
            messagebox.showwarning("提示", "请先选择国标二级农业区。")
            return None
        try:
            rule_set = membership_tool.load_rule_set(self.rules_dir, self.area_var.get())
            self.log_status("正在进行提交前审查：固定字段、字段类型、空值和类别/数值。")
            report = validate_recalculate_source(self.source, rule_set)
        except Exception as exc:
            messagebox.showerror("审查失败", str(exc))
            return None
        self.last_report = report
        self.last_report_key = self.report_key()
        if show_report:
            self.show_report(report, ask_continue=False)
            if report.ok:
                self.log_status("审查通过，可以提交重算任务。")
            else:
                self.log_status("审查未通过，已在报告中列出问题。")
        return report

    def show_report(self, report: RecalculateReport, ask_continue: bool) -> bool:
        window = Toplevel(self.root)
        window.title("第三步结果重算隶属度审查报告")
        window.geometry("1000x700")
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
            ttk.Button(button_row, text="确认无误，继续重算", command=confirm).pack(side=LEFT, padx=5)
            ttk.Button(button_row, text="取消", command=cancel).pack(side=LEFT, padx=5)
            window.transient(self.root)
            window.grab_set()
            self.root.wait_window(window)
            return result["confirmed"]
        ttk.Button(button_row, text="关闭", command=window.destroy).pack(side=LEFT, padx=5)
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
        if self.source is None:
            messagebox.showwarning("提示", "请先选择修改后的第三步结果。")
            return
        output = self.output_settings()
        if output is None:
            return
        output_gdb, output_feature_name = output
        output_dataset = output_gdb / output_feature_name
        if output_dataset.resolve() == Path(source_dataset_path(self.source)).resolve():
            messagebox.showerror("输出错误", "输出结果不能覆盖输入数据。")
            return
        try:
            report = self.last_report
            if report is None or self.last_report_key != self.report_key():
                self.log_status("当前输入没有最新审查报告，正在进行提交前自动审查。")
                report = self.validate_current_input(show_report=False)
            if report is None:
                return
            if not report.ok:
                self.show_report(report, ask_continue=False)
                self.log_status("提交前审查未通过，任务未提交。")
                return
        except Exception as exc:
            messagebox.showerror("提交失败", str(exc))
            return
        if not self.show_report(report, ask_continue=True):
            self.log_status("用户取消重算任务，未提交。")
            return
        job = RecalculateJob(
            job_id=uuid.uuid4().hex[:8],
            source=self.source,
            output_gdb=output_gdb,
            output_feature_name=output_feature_name,
            rule_set=report.rule_set,
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
        history_path = self.logs_dir / "recalculate_scores_history.jsonl"
        if not history_path.exists():
            messagebox.showinfo("历史记录", "暂无历史记录。")
            return
        window = Toplevel(self.root)
        window.title("重算隶属度历史记录")
        window.geometry("980x620")
        text_frame = ttk.Frame(window)
        text_frame.pack(fill=BOTH, expand=True, padx=8, pady=8)
        text = Text(text_frame, wrap="word")
        text_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=text_scroll.set)
        text.pack(side=LEFT, fill=BOTH, expand=True)
        text_scroll.pack(side=LEFT, fill="y")
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
                f"统计：{record.get('recalculated_count')}\n"
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
        if not messagebox.askyesno("确认删除", "确定删除重算隶属度日志和历史记录吗？"):
            return
        deleted = 0
        for path in self.logs_dir.glob("recalculate_scores_*.log"):
            path.unlink()
            deleted += 1
        history_path = self.logs_dir / "recalculate_scores_history.jsonl"
        if history_path.exists():
            history_path.unlink()
            deleted += 1
        self.log_status(f"已删除 {deleted} 个重算隶属度日志/历史文件。")

    def reset_inputs(self) -> None:
        self.area_options = self.load_area_options()
        if hasattr(self, "area_combo"):
            self.area_combo["values"] = self.area_options
        self.area_var.set(self.area_options[0] if self.area_options else membership_tool.DEFAULT_AREA_NAME)
        self.source_path_var.set("")
        self.source_layer_var.set("")
        self.output_gdb_var.set("")
        self.output_feature_var.set(DEFAULT_OUTPUT_FEATURE)
        self.gdb_sources = []
        self.source = None
        self.last_report = None
        self.last_report_key = None
        if hasattr(self, "layer_combo"):
            self.layer_combo["values"] = []
        self.log_status("重算隶属度工具输入和参数已恢复为启动默认值。")


def main() -> int:
    try:
        require_runtime()
    except Exception as exc:
        root = Tk()
        root.withdraw()
        messagebox.showerror("缺少运行环境", str(exc))
        return 1
    root = Tk()
    RecalculateScoresApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
