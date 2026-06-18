"""ArcPy UI tool for balancing cropland area and calculating weighted grade."""

from __future__ import annotations

import json
import logging
import queue
import threading
import traceback
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk

from cropland_quality_update.paths import resolve_paths
from cropland_quality_update.tools import membership_arcpy_ui as membership_tool
from cropland_quality_update.tools import update_scores_arcpy_ui as score_tool


arcpy = score_tool.arcpy
ARCPY_IMPORT_ERROR = score_tool.ARCPY_IMPORT_ERROR

VectorSource = score_tool.VectorSource

LAND_CLASS_FIELD = "地类号"
ENTITY_AREA_FIELD = "实体面积"
BALANCED_AREA_FIELD = "平差面积"
GRADE_FIELD = "质量等级"
COUNTY_NAME_FIELD = "县名称"
WEIGHTED_AREA_FIELD = "等级面积"
WEIGHTED_AREA_ALIAS = "等级*面积"

CROPLAND_CLASSES = [
    ("0101", "水田"),
    ("0102", "旱地"),
    ("0103", "水浇地"),
]


@dataclass(frozen=True)
class FieldBinding:
    canonical_name: str
    field_name: str
    field_type: str


@dataclass(frozen=True)
class FieldProblem:
    field_name: str
    side: str
    message: str


@dataclass(frozen=True)
class AreaClassStats:
    code: str
    name: str
    official_area: float
    entity_area: float
    feature_count: int
    coefficient: float


@dataclass(frozen=True)
class AreaBalancePreflightReport:
    ok: bool
    source: VectorSource
    feature_count: int
    official_areas: dict[str, float]
    bindings: dict[str, FieldBinding]
    class_stats: list[AreaClassStats]
    county_name: str
    problems: list[FieldProblem]
    warnings: list[str]
    text: str


@dataclass(frozen=True)
class AreaBalanceJob:
    job_id: str
    source: VectorSource
    output_path: Path
    output_feature_name: str | None
    output_kind: str
    bindings: dict[str, FieldBinding]
    class_stats: list[AreaClassStats]
    county_name: str
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


def parse_positive_float(text: str, label: str) -> float:
    value_text = str(text or "").strip()
    if not value_text:
        raise ValueError(f"{label}不能为空。")
    try:
        value = float(value_text)
    except ValueError as exc:
        raise ValueError(f"{label}必须是双精度浮点型数字。") from exc
    if value <= 0:
        raise ValueError(f"{label}必须大于 0。")
    return value


def numeric_value(value: object) -> float | None:
    if membership_tool.is_blank_value(value):
        return None
    try:
        return float(value)
    except Exception:
        return None


def validate_polygon_source(source: VectorSource) -> int:
    dataset = source_dataset_path(source)
    if not arcpy.Exists(dataset):
        raise RuntimeError(f"第三步结果不存在：{dataset}")
    desc = arcpy.Describe(dataset)
    if getattr(desc, "shapeType", "") != "Polygon":
        raise RuntimeError("第三步结果必须是面矢量。")
    return int(arcpy.management.GetCount(dataset)[0])


def bind_required_fields(source: VectorSource) -> tuple[dict[str, FieldBinding], list[FieldProblem]]:
    bindings: dict[str, FieldBinding] = {}
    problems: list[FieldProblem] = []
    for name in (LAND_CLASS_FIELD, ENTITY_AREA_FIELD, BALANCED_AREA_FIELD, GRADE_FIELD, COUNTY_NAME_FIELD):
        match, problem = score_tool.choose_field_match(source, name, "第三步结果")
        if problem or match is None:
            problems.append(FieldProblem(name, "第三步结果", "缺少必须字段"))
            continue
        bindings[name] = FieldBinding(name, match.field_name, match.field_type)
    return bindings, problems


def mode_nonblank(values: list[object], field_name: str) -> tuple[str, FieldProblem | None]:
    counts: Counter[str] = Counter()
    for value in values:
        if membership_tool.is_blank_value(value):
            continue
        counts[str(value).strip()] += 1
    if not counts:
        return "", FieldProblem(field_name, "第三步结果", "没有非空值，无法生成加权等级名称")
    return counts.most_common(1)[0][0], None


def compute_class_stats(
    source: VectorSource,
    bindings: dict[str, FieldBinding],
    official_areas: dict[str, float],
) -> tuple[list[AreaClassStats], str, list[FieldProblem], list[str]]:
    dataset = source_dataset_path(source)
    class_sums = {code: 0.0 for code, _name in CROPLAND_CLASSES}
    class_counts = {code: 0 for code, _name in CROPLAND_CLASSES}
    invalid_area_count = 0
    invalid_grade_count = 0
    ignored_class_counts: Counter[str] = Counter()
    county_values: list[object] = []
    problems: list[FieldProblem] = []
    warnings: list[str] = []
    fields = [
        bindings[LAND_CLASS_FIELD].field_name,
        bindings[ENTITY_AREA_FIELD].field_name,
        bindings[GRADE_FIELD].field_name,
        bindings[COUNTY_NAME_FIELD].field_name,
    ]
    with arcpy.da.SearchCursor(dataset, fields) as cursor:
        for land_value, area_value, grade_value, county_value in cursor:
            code = normalize_land_class_code(land_value)
            county_values.append(county_value)
            if code not in class_sums:
                ignored_class_counts[code or "<空>"] += 1
                continue
            area = numeric_value(area_value)
            grade = numeric_value(grade_value)
            if area is None or area <= 0:
                invalid_area_count += 1
                continue
            if grade is None:
                invalid_grade_count += 1
                continue
            class_sums[code] += area
            class_counts[code] += 1

    if invalid_area_count:
        problems.append(FieldProblem(ENTITY_AREA_FIELD, "第三步结果", f"有 {invalid_area_count} 个耕地图斑实体面积为空、非数字或小于等于 0"))
    if invalid_grade_count:
        problems.append(FieldProblem(GRADE_FIELD, "第三步结果", f"有 {invalid_grade_count} 个耕地图斑质量等级为空或非数字"))
    if ignored_class_counts:
        detail = "、".join(f"{code}={count}" for code, count in ignored_class_counts.most_common(10))
        problems.append(FieldProblem(LAND_CLASS_FIELD, "第三步结果", f"发现非 0101/0102/0103 地类要素：{detail}。第四步只接受第三步筛选后的耕地图斑结果"))

    county_name, county_problem = mode_nonblank(county_values, COUNTY_NAME_FIELD)
    if county_problem:
        problems.append(county_problem)

    stats: list[AreaClassStats] = []
    for code, name in CROPLAND_CLASSES:
        entity_area = class_sums[code]
        official_area = official_areas[code]
        if class_counts[code] <= 0:
            problems.append(FieldProblem(LAND_CLASS_FIELD, "第三步结果", f"{code}{name} 没有可参与平差的要素"))
            coefficient = 0.0
        elif entity_area <= 0:
            problems.append(FieldProblem(ENTITY_AREA_FIELD, "第三步结果", f"{code}{name} 实体面积合计小于等于 0"))
            coefficient = 0.0
        else:
            coefficient = official_area / entity_area
        stats.append(
            AreaClassStats(
                code=code,
                name=name,
                official_area=official_area,
                entity_area=entity_area,
                feature_count=class_counts[code],
                coefficient=coefficient,
            )
        )
    return stats, county_name, problems, warnings


def format_float(value: float, digits: int = 6) -> str:
    return f"{value:.{digits}f}"


def build_preflight_text(report: AreaBalancePreflightReport) -> str:
    lines: list[str] = []
    lines.append("第四步耕地面积平差前审查报告")
    lines.append("=" * 60)
    lines.append(f"第三步结果：{source_label(report.source)}")
    lines.append(f"输入要素数：{report.feature_count}")
    lines.append(f"县名称众数：{report.county_name or '未确定'}")
    lines.append("")
    lines.append("一、字段检查")
    for name in (LAND_CLASS_FIELD, ENTITY_AREA_FIELD, BALANCED_AREA_FIELD, GRADE_FIELD, COUNTY_NAME_FIELD):
        binding = report.bindings.get(name)
        if binding:
            lines.append(f"   - {name}: {binding.field_name}({binding.field_type})")
        else:
            lines.append(f"   - {name}: 未匹配")
    lines.append("")
    lines.append("二、官方面积、实体面积和平差系数")
    lines.append("   平差系数 = 用户输入的 2024 年湖北省耕地面积统计数据 / 第三步结果对应地类实体面积合计")
    for stat in report.class_stats:
        balanced_sum = stat.entity_area * stat.coefficient if stat.coefficient else 0.0
        lines.append(
            f"   - {stat.code}{stat.name}: 要素数 {stat.feature_count}；"
            f"实体面积合计 {format_float(stat.entity_area, 6)} 公顷；"
            f"官方面积 {format_float(stat.official_area, 6)} 公顷；"
            f"平差系数 {format_float(stat.coefficient, 10)}；"
            f"预计平差后合计 {format_float(balanced_sum, 6)} 公顷"
        )
    lines.append("")
    lines.append("三、计算规则")
    lines.append("   每个要素按自己的地类号选择平差系数。")
    lines.append("   平差面积 = 实体面积 * 对应地类平差系数。")
    lines.append(f"   {WEIGHTED_AREA_ALIAS} = 平差面积 * 质量等级。")
    lines.append("   县域耕地质量等级 = Σ(等级*面积) / Σ(平差面积)。")
    lines.append(f"   注意：ArcGIS 字段名通常不允许 *，工具创建真实字段名 {WEIGHTED_AREA_FIELD}，字段别名为 {WEIGHTED_AREA_ALIAS}。")
    lines.append("")
    lines.append("四、需要修正的问题")
    if report.problems:
        for problem in report.problems:
            lines.append(f"   - {problem.field_name}；{problem.side}：{problem.message}")
    else:
        lines.append("   无。")
    lines.append("")
    lines.append("五、提示")
    if report.warnings:
        lines.extend(f"   - {item}" for item in report.warnings)
    else:
        lines.append("   无。")
    lines.append("")
    lines.append("审查通过，可以继续提交平差任务。" if report.ok else "审查未通过，请先修正输入数据或面积参数后重新审查。")
    return "\n".join(lines)


def build_preflight_report(source: VectorSource, official_areas: dict[str, float]) -> AreaBalancePreflightReport:
    require_runtime()
    feature_count = validate_polygon_source(source)
    problems: list[FieldProblem] = []
    warnings: list[str] = []
    try:
        score_tool.audit_output_schema(source_dataset_path(source))
    except Exception as exc:
        problems.append(FieldProblem("固定字段结构", "第三步结果", str(exc)))
    bindings, field_problems = bind_required_fields(source)
    problems.extend(field_problems)
    class_stats: list[AreaClassStats] = []
    county_name = ""
    if not field_problems:
        stats, county_name, stat_problems, stat_warnings = compute_class_stats(source, bindings, official_areas)
        class_stats = stats
        problems.extend(stat_problems)
        warnings.extend(stat_warnings)
    ok = not problems and len(class_stats) == len(CROPLAND_CLASSES)
    draft = AreaBalancePreflightReport(
        ok=ok,
        source=source,
        feature_count=feature_count,
        official_areas=official_areas,
        bindings=bindings,
        class_stats=class_stats,
        county_name=county_name,
        problems=problems,
        warnings=warnings,
        text="",
    )
    return AreaBalancePreflightReport(
        ok=draft.ok,
        source=draft.source,
        feature_count=draft.feature_count,
        official_areas=draft.official_areas,
        bindings=draft.bindings,
        class_stats=draft.class_stats,
        county_name=draft.county_name,
        problems=draft.problems,
        warnings=draft.warnings,
        text=build_preflight_text(draft),
    )


def add_or_find_weighted_area_field(feature_class: str) -> str:
    for field in arcpy.ListFields(feature_class):
        if field.name.upper() == WEIGHTED_AREA_FIELD.upper() or (field.aliasName or "").strip() == WEIGHTED_AREA_ALIAS:
            try:
                arcpy.management.AlterField(feature_class, field.name, new_field_alias=WEIGHTED_AREA_ALIAS)
            except Exception:
                pass
            return field.name
    workspace = str(Path(feature_class).parent)
    existing = {field.name.upper() for field in arcpy.ListFields(feature_class)}
    field_name = arcpy.ValidateFieldName(WEIGHTED_AREA_FIELD, workspace)
    base_name = field_name
    suffix = 1
    while field_name.upper() in existing:
        field_name = arcpy.ValidateFieldName(f"{base_name}_{suffix}", workspace)
        suffix += 1
    arcpy.management.AddField(feature_class, field_name, "DOUBLE", field_alias=WEIGHTED_AREA_ALIAS)
    return field_name


def copy_source_to_output(job: AreaBalanceJob) -> str:
    if job.output_kind != "gdb":
        raise RuntimeError("第四步建议并要求输出到 GDB 面要素类，以保留中文字段和新增字段别名。")
    if not job.output_feature_name:
        raise RuntimeError("输出到 GDB 时必须指定面要素类名称。")
    delete_output_dataset(job.output_path, job.output_feature_name)
    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    if not job.output_path.exists():
        arcpy.management.CreateFileGDB(str(job.output_path.parent), job.output_path.name)
    output_fc = str(job.output_path / job.output_feature_name)
    arcpy.conversion.ExportFeatures(source_dataset_path(job.source), output_fc)
    return output_fc


def calculate_balance(job: AreaBalanceJob, logger: logging.Logger) -> tuple[str, dict[str, float | int | str]]:
    require_runtime()
    arcpy.env.overwriteOutput = True
    output_fc = copy_source_to_output(job)
    coefficient_by_code = {stat.code: stat.coefficient for stat in job.class_stats}
    official_by_code = {stat.code: stat.official_area for stat in job.class_stats}
    weighted_field = add_or_find_weighted_area_field(output_fc)
    land_field = score_tool.field_name_case_insensitive(output_fc, job.bindings[LAND_CLASS_FIELD].field_name)
    entity_field = score_tool.field_name_case_insensitive(output_fc, job.bindings[ENTITY_AREA_FIELD].field_name)
    balance_field = score_tool.field_name_case_insensitive(output_fc, job.bindings[BALANCED_AREA_FIELD].field_name)
    grade_field = score_tool.field_name_case_insensitive(output_fc, job.bindings[GRADE_FIELD].field_name)

    sums_by_code = {code: {"balanced": 0.0, "weighted": 0.0, "count": 0} for code, _name in CROPLAND_CLASSES}
    skipped = 0
    total_balanced = 0.0
    total_weighted = 0.0
    with arcpy.da.UpdateCursor(output_fc, [land_field, entity_field, balance_field, grade_field, weighted_field]) as cursor:
        for row in cursor:
            code = normalize_land_class_code(row[0])
            if code not in coefficient_by_code:
                raise RuntimeError(f"输出要素包含非 0101/0102/0103 地类号，无法平差：{code or '<空>'}")
            entity_area = numeric_value(row[1])
            grade = numeric_value(row[3])
            if entity_area is None or entity_area <= 0 or grade is None:
                raise RuntimeError(f"输出要素存在无法平差的实体面积或质量等级，地类号={code}")
            balanced_area = entity_area * coefficient_by_code[code]
            weighted_area = balanced_area * grade
            row[2] = balanced_area
            row[4] = weighted_area
            cursor.updateRow(row)
            sums_by_code[code]["balanced"] += balanced_area
            sums_by_code[code]["weighted"] += weighted_area
            sums_by_code[code]["count"] += 1
            total_balanced += balanced_area
            total_weighted += weighted_area

    if total_balanced <= 0:
        raise RuntimeError("平差面积合计小于等于 0，无法计算加权平均质量等级。")
    weighted_grade = total_weighted / total_balanced
    lines = ["耕地面积平差结果统计", "=" * 60]
    for code, name in CROPLAND_CLASSES:
        balanced = float(sums_by_code[code]["balanced"])
        official = official_by_code[code]
        diff = balanced - official
        lines.append(
            f"{code}{name}: 要素数 {int(sums_by_code[code]['count'])}；"
            f"平差面积合计 {format_float(balanced, 6)} 公顷；"
            f"官方面积 {format_float(official, 6)} 公顷；差值 {format_float(diff, 10)} 公顷"
        )
    lines.append(f"平差面积总和：{format_float(total_balanced, 6)} 公顷")
    lines.append(f"等级*面积总和：{format_float(total_weighted, 6)}")
    lines.append(f"{job.county_name}耕地质量等级：{format_float(weighted_grade, 6)}")
    logger.info("\n%s", "\n".join(lines))
    stats: dict[str, float | int | str] = {
        "weighted_field": weighted_field,
        "weighted_field_alias": WEIGHTED_AREA_ALIAS,
        "skipped_non_cropland_features": skipped,
        "total_balanced_area": total_balanced,
        "total_weighted_area": total_weighted,
        "weighted_grade": weighted_grade,
        "weighted_grade_label": f"{job.county_name}耕地质量等级",
    }
    for code, _name in CROPLAND_CLASSES:
        stats[f"{code}_coefficient"] = coefficient_by_code[code]
        stats[f"{code}_balanced_area"] = float(sums_by_code[code]["balanced"])
        stats[f"{code}_official_area"] = official_by_code[code]
    logger.info("输出完成：%s", output_fc)
    return output_fc, stats


def setup_job_logger(logs_dir: Path, job_id: str) -> tuple[logging.Logger, Path]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"area_balance_{timestamp_for_file()}_{job_id}.log"
    logger = logging.getLogger(f"area_balance_arcpy.{job_id}")
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


class AreaBalanceWorker(threading.Thread):
    def __init__(self, job_queue: queue.Queue, event_queue: queue.Queue, logs_dir: Path):
        super().__init__(daemon=True)
        self.job_queue = job_queue
        self.event_queue = event_queue
        self.logs_dir = logs_dir
        self.history_path = logs_dir / "area_balance_history.jsonl"

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

    def process_job(self, job: AreaBalanceJob) -> None:
        logger, log_path = setup_job_logger(self.logs_dir, job.job_id)
        output_target = output_target_path(job.output_path, job.output_feature_name, job.output_kind)
        record = {
            "job_id": job.job_id,
            "created_at": job.created_at,
            "started_at": now_text(),
            "ended_at": None,
            "status": "running",
            "source": source_path_for_log(job.source),
            "output_path": output_target,
            "class_stats": [stat.__dict__ for stat in job.class_stats],
            "county_name": job.county_name,
            "validation_report": job.validation_report,
            "log_path": str(log_path),
            "error": None,
            "stats": None,
        }
        self.send("job_started", {"job_id": job.job_id, "message": "开始计算耕地面积平差", "log_path": str(log_path)})
        try:
            require_runtime()
            logger.info("任务开始：%s", job.job_id)
            logger.info("第三步结果：%s", source_path_for_log(job.source))
            logger.info("输出目标：%s", output_target)
            if job.validation_report:
                logger.info("提交前审查报告：\n%s", job.validation_report)
            output_fc, stats = calculate_balance(job, logger)
            record.update({"status": "success", "stats": stats})
            self.send(
                "job_done",
                {
                    "job_id": job.job_id,
                    "message": (
                        f"平差完成：{output_fc}；"
                        f"{stats['weighted_grade_label']} = {format_float(float(stats['weighted_grade']), 6)}"
                    ),
                    "output_path": output_fc,
                    "log_path": str(log_path),
                    "stats": stats,
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
                    "message": f"平差失败：{exc}",
                    "log_path": str(log_path),
                },
            )
        finally:
            record["ended_at"] = now_text()
            append_history(self.history_path, record)
            close_job_logger(logger)


class AreaBalanceApp:
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
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.source_path_var = StringVar()
        self.source_layer_var = StringVar()
        self.source: VectorSource | None = None
        self.gdb_sources: list[VectorSource] = []

        self.official_area_vars = {code: StringVar() for code, _name in CROPLAND_CLASSES}
        self.coefficient_vars = {code: StringVar(value=f"{code}{name}: 尚未计算") for code, name in CROPLAND_CLASSES}

        self.output_gdb_var = StringVar()
        self.output_feature_var = StringVar(value="Step4_面积平差")
        self.result_grade_var = StringVar(value="尚未计算")

        self.last_report: AreaBalancePreflightReport | None = None
        self.last_report_key: tuple | None = None

        self.job_queue: queue.Queue = queue.Queue()
        self.event_queue: queue.Queue = queue.Queue()
        self.worker = AreaBalanceWorker(self.job_queue, self.event_queue, self.logs_dir)
        self.worker.start()

        if not self.embedded:
            self.root.title("第四步：耕地面积平差工具（ArcPy）")
            self.root.geometry("1060x760")
            self.root.minsize(1060, 700)
        self.build_ui()
        self.root.after(200, self.poll_worker_events)

    def build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill=BOTH, expand=True)

        input_frame = ttk.LabelFrame(container, text="1. 输入第三步结果")
        input_frame.pack(fill="x", pady=5)
        row = ttk.Frame(input_frame)
        row.pack(fill="x", padx=5, pady=4)
        ttk.Label(row, text="第三步结果", width=12).pack(side=LEFT)
        ttk.Entry(row, textvariable=self.source_path_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(row, text="选择 shp", command=self.choose_shp).pack(side=LEFT, padx=3)
        ttk.Button(row, text="选择 gdb", command=self.choose_gdb).pack(side=LEFT, padx=3)
        layer_row = ttk.Frame(input_frame)
        layer_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(layer_row, text="GDB 图层", width=12).pack(side=LEFT)
        self.layer_combo = ttk.Combobox(layer_row, textvariable=self.source_layer_var, state="readonly", width=96)
        self.layer_combo.pack(side=LEFT, fill="x", expand=True, padx=5)
        self.layer_combo.bind("<<ComboboxSelected>>", self.update_source_from_layer)

        area_frame = ttk.LabelFrame(container, text="2. 2024 年湖北省耕地面积统计数据（公顷）")
        area_frame.pack(fill="x", pady=5)
        for code, name in CROPLAND_CLASSES:
            area_row = ttk.Frame(area_frame)
            area_row.pack(fill="x", padx=5, pady=3)
            ttk.Label(area_row, text=f"{code}{name}", width=12).pack(side=LEFT)
            ttk.Entry(area_row, textvariable=self.official_area_vars[code], width=24).pack(side=LEFT, padx=5)

        coefficient_frame = ttk.LabelFrame(container, text="3. 平差系数预览")
        coefficient_frame.pack(fill="x", pady=5)
        for code, _name in CROPLAND_CLASSES:
            ttk.Label(coefficient_frame, textvariable=self.coefficient_vars[code]).pack(anchor="w", padx=5, pady=2)

        output_frame = ttk.LabelFrame(container, text="4. 输出位置")
        output_frame.pack(fill="x", pady=5)
        ttk.Label(output_frame, text="第四步固定输出为 GDB 面要素类；不建议使用 Shapefile 保存平差结果。").pack(anchor="w", padx=5, pady=2)
        gdb_row = ttk.Frame(output_frame)
        gdb_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(gdb_row, text="GDB").pack(side=LEFT)
        ttk.Entry(gdb_row, textvariable=self.output_gdb_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(gdb_row, text="选择已有 gdb", command=self.choose_existing_output_gdb).pack(side=LEFT, padx=3)
        ttk.Button(gdb_row, text="新建 gdb", command=self.choose_output_gdb).pack(side=LEFT, padx=3)
        ttk.Label(gdb_row, text="面要素类名").pack(side=LEFT, padx=5)
        ttk.Entry(gdb_row, textvariable=self.output_feature_var, width=26).pack(side=LEFT)

        result_frame = ttk.LabelFrame(container, text="5. 加权平均结果")
        result_frame.pack(fill="x", pady=5)
        ttk.Label(result_frame, textvariable=self.result_grade_var, font=("", 12, "bold")).pack(anchor="w", padx=5, pady=6)

        report_frame = ttk.LabelFrame(container, text="6. 审查报告、日志和历史记录")
        report_frame.pack(fill="both", expand=True, pady=5)
        action_row = ttk.Frame(report_frame)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="开始审查", command=self.validate_current_inputs).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="提交平差任务", command=self.submit_job).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="查看历史记录", command=self.show_history).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="打开日志文件夹", command=self.open_logs_folder).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="删除日志", command=self.delete_logs).pack(side=LEFT, padx=5, pady=4)
        if self.shared_status_text is None:
            self.status_text = Text(report_frame, height=22, wrap="word")
            self.status_text.pack(fill="both", expand=True, padx=5, pady=5)
        else:
            self.status_text = self.shared_status_text
            ttk.Label(report_frame, text="运行详细信息显示在窗口底部“详细信息”区域。").pack(anchor="w", padx=5, pady=5)
        self.log_status("第四步工具已启动。请先选择第三步结果并填写三类官方面积。")

    def choose_shp(self) -> None:
        path = filedialog.askopenfilename(title="选择第三步结果 shp", filetypes=[("Shapefile", "*.shp")])
        if not path:
            return
        self.source = make_vector_source("shp", Path(path))
        self.source_path_var.set(str(self.source.source_path))
        self.source_layer_var.set("")
        self.gdb_sources = []
        self.layer_combo["values"] = []
        self.last_report = None
        self.log_status(f"已选择第三步结果 Shapefile：{source_label(self.source)}")

    def choose_gdb(self) -> None:
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
        self.gdb_sources = sources
        self.source_path_var.set(str(gdb_path))
        self.layer_combo["values"] = [source.layer_name for source in sources]
        self.source_layer_var.set(str(sources[0].layer_name))
        self.source = sources[0]
        self.last_report = None
        self.log_status(f"已选择第三步结果 GDB：{gdb_path}，面图层数量 {len(sources)}。")

    def update_source_from_layer(self, _event=None) -> None:
        layer_name = self.source_layer_var.get()
        for source in self.gdb_sources:
            if source.layer_name == layer_name:
                self.source = source
                self.last_report = None
                self.log_status(f"已选择第三步结果 GDB 面图层：{source_label(source)}")
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
        gdb_path = find_nearest_gdb_path(Path(path))
        if gdb_path is None or not is_gdb_path(gdb_path):
            messagebox.showwarning("提示", "请选择一个已有的 .gdb 文件夹。")
            return
        self.output_gdb_var.set(str(gdb_path))
        self.log_status(f"已选择已有 GDB 输出库：{gdb_path}")

    def current_official_areas(self) -> dict[str, float] | None:
        try:
            return {
                code: parse_positive_float(self.official_area_vars[code].get(), f"{code}{name}官方面积")
                for code, name in CROPLAND_CLASSES
            }
        except ValueError as exc:
            messagebox.showwarning("面积输入错误", str(exc))
            return None

    def report_key(self) -> tuple:
        area_values = tuple((code, self.official_area_vars[code].get().strip()) for code, _name in CROPLAND_CLASSES)
        return (self.source, area_values)

    def validate_current_inputs(self) -> None:
        if self.source is None:
            messagebox.showwarning("提示", "请先选择第三步结果。")
            return
        official_areas = self.current_official_areas()
        if official_areas is None:
            return
        try:
            self.log_status("开始审查字段、实体面积汇总和三类平差系数。")
            report = build_preflight_report(self.source, official_areas)
        except Exception as exc:
            messagebox.showerror("审查失败", str(exc))
            return
        self.last_report = report
        self.last_report_key = self.report_key()
        self.update_coefficient_preview(report)
        self.show_report(report, ask_continue=False)
        self.log_status("审查通过，可以提交平差任务。" if report.ok else "审查未通过，已在报告中列出问题。")

    def update_coefficient_preview(self, report: AreaBalancePreflightReport) -> None:
        stats_by_code = {stat.code: stat for stat in report.class_stats}
        for code, name in CROPLAND_CLASSES:
            stat = stats_by_code.get(code)
            if stat is None:
                self.coefficient_vars[code].set(f"{code}{name}: 尚未计算")
                continue
            self.coefficient_vars[code].set(
                f"{code}{name}: 平差系数 {format_float(stat.coefficient, 10)}；"
                f"实体面积 {format_float(stat.entity_area, 6)} 公顷；"
                f"官方面积 {format_float(stat.official_area, 6)} 公顷"
            )

    def show_report(self, report: AreaBalancePreflightReport, ask_continue: bool) -> bool:
        window = Toplevel(self.root)
        window.title("第四步平差前审查报告")
        window.geometry("1020x700")
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
            ttk.Button(button_row, text="确认无误，继续平差", command=confirm).pack(side=LEFT, padx=5)
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
        output_feature_name = membership_tool.validate_gdb_feature_name(feature_name, output_path)
        if output_feature_name != feature_name:
            self.output_feature_var.set(output_feature_name)
            self.log_status(f"输出要素类名已按 FileGDB 规则修正为：{output_feature_name}")
        return "gdb", output_path, output_feature_name

    def submit_job(self) -> None:
        if self.source is None:
            messagebox.showwarning("提示", "请先选择第三步结果。")
            return
        output = self.output_settings()
        if output is None:
            return
        output_kind, output_path, output_feature_name = output
        output_dataset = output_dataset_path(output_kind, output_path, output_feature_name)
        input_dataset = Path(source_dataset_path(self.source)).resolve()
        if output_dataset.resolve() == input_dataset:
            messagebox.showerror("输出错误", "输出结果不能覆盖第三步输入数据，请换一个输出名称。")
            return
        if self.source.kind == "gdb" and output_path.resolve() == self.source.source_path.resolve():
            self.log_status(f"输出将保存到第三步结果所在 GDB 的新图层：{output_feature_name}")
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
                messagebox.showerror("审查未通过", "输入数据或面积参数存在问题，不能平差。请先按报告修正。")
                return
        except Exception as exc:
            messagebox.showerror("提交失败", str(exc))
            return
        if not self.show_report(report, ask_continue=True):
            self.log_status("用户取消平差任务，未提交。")
            return
        job = AreaBalanceJob(
            job_id=uuid.uuid4().hex[:8],
            source=self.source,
            output_path=output_path,
            output_feature_name=output_feature_name,
            output_kind=output_kind,
            bindings=report.bindings,
            class_stats=report.class_stats,
            county_name=report.county_name,
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
                if event_type == "job_done" and payload.get("stats"):
                    stats = payload["stats"]
                    self.result_grade_var.set(f"{stats['weighted_grade_label']} = {format_float(float(stats['weighted_grade']), 6)}")
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
        history_path = self.logs_dir / "area_balance_history.jsonl"
        if not history_path.exists():
            messagebox.showinfo("历史记录", "暂无历史记录。")
            return
        window = Toplevel(self.root)
        window.title("第四步平差历史记录")
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
            stats = record.get("stats") or {}
            text.insert(
                END,
                f"任务 {record.get('job_id')} | {record.get('status')} | {record.get('created_at')}\n"
                f"第三步结果：{record.get('source')}\n"
                f"输出：{record.get('output_path')}\n"
                f"结果：{stats.get('weighted_grade_label', '')}={stats.get('weighted_grade', '')}\n"
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
        if not messagebox.askyesno("确认删除", "确定删除第四步平差日志和历史记录吗？"):
            return
        deleted = 0
        for path in self.logs_dir.glob("area_balance_*.log"):
            path.unlink()
            deleted += 1
        history_path = self.logs_dir / "area_balance_history.jsonl"
        if history_path.exists():
            history_path.unlink()
            deleted += 1
        self.log_status(f"已删除 {deleted} 个第四步日志/历史文件。")

    def reset_inputs(self) -> None:
        self.source_path_var.set("")
        self.source_layer_var.set("")
        self.source = None
        self.gdb_sources = []
        if hasattr(self, "layer_combo"):
            self.layer_combo["values"] = []
        for code, name in CROPLAND_CLASSES:
            self.official_area_vars[code].set("")
            self.coefficient_vars[code].set(f"{code}{name}: 尚未计算")
        self.output_gdb_var.set("")
        self.output_feature_var.set("Step4_面积平差")
        self.result_grade_var.set("尚未计算")
        self.last_report = None
        self.last_report_key = None
        self.log_status("第四步输入和参数已恢复为启动默认值。")


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
    AreaBalanceApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
