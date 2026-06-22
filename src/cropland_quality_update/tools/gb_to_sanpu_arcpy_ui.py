"""ArcPy UI tool for converting GB-standard high-standard fields to Sanpu memberships."""

from __future__ import annotations

import json
import logging
import queue
import re
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
from cropland_quality_update.tools import membership_arcpy_ui as membership_tool
from cropland_quality_update.tools import vector_common_arcpy as vector_tool


arcpy = membership_tool.arcpy

DEFAULT_OUTPUT_FEATURE = "国标转三普高标隶属度"
ISSUE_DETAIL_LIMIT = membership_tool.ISSUE_DETAIL_LIMIT
NUMERIC_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")

FIELD_ALIASES: dict[str, list[str]] = {
    "地形部位": ["地形部位", "地貌部位", "地貌类型", "F地形部位"],
    "耕层质地": ["耕层质地", "土壤质地", "表层质地", "质地", "F耕层质地"],
    "水资源条件": ["水资源条件", "灌溉能力", "灌溉保证率", "水源条件", "F水资源条件", "F灌溉能力"],
    "排水能力": ["排水能力", "排涝能力", "排水条件", "F排水能力"],
    "海拔高度": ["海拔高度", "海拔", "海拔高程", "平均海拔", "DEM", "F海拔高度"],
    "有机质": ["有机质", "土壤有机质", "有机质含量", "F有机质"],
    "有效土层厚度": ["有效土层厚度", "有效土层", "有效土层厚", "土层厚度", "土体厚度", "F有效土层厚度", "F有效土层"],
    "土壤容重": ["土壤容重", "容重", "F土壤容重"],
    "速效钾": ["速效钾", "速效K", "有效钾", "F速效钾"],
    "有效磷": ["有效磷", "有效P", "速效磷", "F有效磷"],
    "质地构型": ["质地构型", "剖面构型", "土体构型", "F质地构型"],
    "酸碱度": ["酸碱度", "pH", "PH", "土壤pH", "土壤PH", "F酸碱度"],
    "耕层厚度": ["耕层厚度", "耕作层厚度", "耕层厚", "F耕层厚度"],
}

CATEGORY_ALIASES: dict[str, dict[str, str]] = {
    "耕层质地": {
        "中壤": "壤土",
        "中壤土": "壤土",
        "轻壤": "粉（砂）质壤土",
        "轻壤土": "粉（砂）质壤土",
        "粉砂质壤土": "粉（砂）质壤土",
        "砂壤": "砂质壤土",
        "砂壤土": "砂质壤土",
        "重壤": "黏壤土",
        "重壤土": "黏壤土",
        "粘壤土": "黏壤土",
        "壤质粘土": "壤质黏土",
        "粘土": "黏土",
        "重粘土": "重黏土",
        "砂土": "砂土及壤质砂土",
        "壤质砂土": "砂土及壤质砂土",
    },
    "水资源条件": {
        "充分": "充分满足",
        "充足": "充分满足",
        "充足满足": "充分满足",
        "较满足": "满足",
        "一般": "基本满足",
        "基本": "基本满足",
        "不足": "不满足",
    },
    "排水能力": {
        "充分": "充分满足",
        "充足": "充分满足",
        "充足满足": "充分满足",
        "较满足": "满足",
        "一般": "基本满足",
        "基本": "基本满足",
        "不足": "不满足",
    },
}


@dataclass(frozen=True)
class ConverterBinding:
    indicator: str
    source_field: str
    field_type: str
    matched_label: str
    is_f_field: bool


@dataclass(frozen=True)
class ConversionIssue:
    indicator: str
    field_name: str
    value_text: str
    count: int
    sample_oids: list[int]


@dataclass(frozen=True)
class ConversionReport:
    ok: bool
    source: membership_tool.VectorSource
    rule_set: membership_tool.RuleSet
    feature_count: int
    bindings: dict[str, ConverterBinding]
    missing_fields: list[str]
    ambiguous_fields: list[str]
    blank_issues: list[ConversionIssue]
    invalid_issues: list[ConversionIssue]
    text: str


@dataclass(frozen=True)
class ConversionJob:
    job_id: str
    source: membership_tool.VectorSource
    output_gdb: Path
    output_feature_name: str
    rule_set: membership_tool.RuleSet
    bindings: dict[str, ConverterBinding]
    created_at: str
    validation_report: str


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_for_file() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def require_runtime() -> None:
    membership_tool.require_runtime()


def compact_key(value: object) -> str:
    text = str(value or "").strip().lower()
    replacements = {
        " ": "",
        "\t": "",
        "\r": "",
        "\n": "",
        "_": "",
        "-": "",
        "　": "",
        "（": "(",
        "）": ")",
        "粘": "黏",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def category_key(value: object) -> str:
    text = compact_key(value)
    for token in ("(", ")", "、", "/", "\\"):
        text = text.replace(token, "")
    return text


def candidate_field_names(indicator: str, rule_set: membership_tool.RuleSet) -> list[str]:
    aliases = FIELD_ALIASES.get(indicator, [indicator])
    if indicator not in rule_set.concept_indicators:
        aliases = [alias for alias in aliases if not compact_key(alias).startswith("f")]
    if indicator in rule_set.concept_indicators:
        aliases = [*aliases, f"F{indicator}", *[f"F{alias}" for alias in aliases if not str(alias).startswith("F")]]
    seen: set[str] = set()
    names: list[str] = []
    for alias in aliases:
        key = compact_key(alias)
        if key and key not in seen:
            seen.add(key)
            names.append(alias)
    return names


def field_alias_labels(source: membership_tool.VectorSource, fields: dict[str, object]) -> dict[str, set[str]]:
    return membership_tool.field_labels_by_actual(source, fields)


def choose_indicator_bindings(
    source: membership_tool.VectorSource,
    rule_set: membership_tool.RuleSet,
) -> tuple[dict[str, ConverterBinding], list[str], list[str]]:
    dataset = membership_tool.source_dataset_path(source)
    fields = {field.name: field for field in arcpy.ListFields(dataset) if membership_tool.is_data_field(field)}
    labels_by_actual = field_alias_labels(source, fields)
    optional_indicators = membership_tool.optional_indicators_for_area(rule_set.area_name)
    bindings: dict[str, ConverterBinding] = {}
    missing: list[str] = []
    ambiguous: list[str] = []

    for indicator in membership_tool.STANDARD_INDICATOR_FIELDS:
        if indicator not in rule_set.indicators:
            continue
        wanted = {compact_key(name) for name in candidate_field_names(indicator, rule_set)}
        candidates: list[tuple[int, str, str]] = []
        for actual_name, labels in labels_by_actual.items():
            for label in labels:
                key = compact_key(label)
                if key not in wanted:
                    continue
                is_f = key.startswith("f")
                exact_raw = key == compact_key(indicator)
                score = 0
                if exact_raw:
                    score -= 100
                if is_f:
                    score += 20
                if fields[actual_name].type in membership_tool.NUMERIC_FIELD_TYPES and indicator in rule_set.concept_indicators:
                    score += 200
                candidates.append((score, actual_name, label))
        if not candidates:
            if indicator not in optional_indicators:
                missing.append(indicator)
            continue
        candidates.sort(key=lambda item: (item[0], item[1]))
        best_score = candidates[0][0]
        best = [item for item in candidates if item[0] == best_score]
        if len({item[1] for item in best}) > 1:
            ambiguous.append(f"{indicator}: " + "、".join(f"{field}({label})" for _score, field, label in best))
            continue
        _, field_name, label = best[0]
        bindings[indicator] = ConverterBinding(
            indicator=indicator,
            source_field=field_name,
            field_type=fields[field_name].type,
            matched_label=label,
            is_f_field=compact_key(label).startswith("f") or compact_key(field_name).startswith("f"),
        )
    return bindings, missing, ambiguous


def category_lookup(rule_set: membership_tool.RuleSet, indicator: str) -> dict[str, str]:
    allowed = rule_set.concept_memberships.get(indicator, {})
    lookup: dict[str, str] = {}
    for category in allowed:
        keys = {category_key(category)}
        if "（砂）" in category or "(砂)" in category:
            keys.add(category_key(str(category).replace("（砂）", "砂").replace("(砂)", "砂")))
            keys.add(category_key(str(category).replace("（砂）", "").replace("(砂)", "")))
        if str(category).endswith("型"):
            keys.add(category_key(str(category)[:-1]))
        for key in keys:
            lookup.setdefault(key, category)
    for alias, target in CATEGORY_ALIASES.get(indicator, {}).items():
        target_key = category_key(target)
        resolved = lookup.get(target_key)
        if resolved:
            lookup[category_key(alias)] = resolved
    return lookup


def normalize_concept_value(rule_set: membership_tool.RuleSet, indicator: str, value: object) -> str | None:
    if membership_tool.is_blank_value(value):
        return None
    lookup = category_lookup(rule_set, indicator)
    return lookup.get(category_key(value))


def parse_numeric_value(value: object) -> float | None:
    if membership_tool.is_blank_value(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        match = NUMERIC_PATTERN.search(text)
        return float(match.group(0)) if match else None


def scan_conversion_issues(
    source: membership_tool.VectorSource,
    rule_set: membership_tool.RuleSet,
    bindings: dict[str, ConverterBinding],
) -> tuple[list[ConversionIssue], list[ConversionIssue]]:
    dataset = membership_tool.source_dataset_path(source)
    oid_name = membership_tool.oid_field_name(dataset)
    fields = [oid_name, *[binding.source_field for binding in bindings.values()]]
    indicators = list(bindings.keys())
    blanks: dict[tuple[str, str], list[int]] = defaultdict(list)
    invalids: dict[tuple[str, str, str], list[int]] = defaultdict(list)

    with arcpy.da.SearchCursor(dataset, fields) as cursor:
        for row in cursor:
            oid = int(row[0])
            for indicator, value in zip(indicators, row[1:]):
                binding = bindings[indicator]
                if indicator in rule_set.concept_memberships:
                    normalized = normalize_concept_value(rule_set, indicator, value)
                    if normalized is None:
                        if membership_tool.is_blank_value(value):
                            blanks[(indicator, binding.source_field)].append(oid)
                        else:
                            invalids[(indicator, binding.source_field, membership_tool.field_value_text(value))].append(oid)
                    continue
                numeric_value = parse_numeric_value(value)
                if numeric_value is None:
                    if membership_tool.is_blank_value(value):
                        blanks[(indicator, binding.source_field)].append(oid)
                    else:
                        invalids[(indicator, binding.source_field, membership_tool.field_value_text(value))].append(oid)

    blank_issues = [
        ConversionIssue(indicator, field_name, "", len(oids), oids[:ISSUE_DETAIL_LIMIT])
        for (indicator, field_name), oids in sorted(blanks.items())
    ]
    invalid_issues = [
        ConversionIssue(indicator, field_name, value_text, len(oids), oids[:ISSUE_DETAIL_LIMIT])
        for (indicator, field_name, value_text), oids in sorted(invalids.items())
    ]
    return blank_issues, invalid_issues


def build_report_text(report: ConversionReport) -> str:
    lines: list[str] = []
    lines.append("国标字段转三普法隶属度审查报告")
    lines.append("=" * 60)
    lines.append(f"输入数据：{membership_tool.source_label(report.source)}")
    lines.append(f"国标二级农业区：{report.rule_set.area_name}")
    lines.append(f"规则文件：{report.rule_set.rule_path}")
    lines.append(f"面要素数：{report.feature_count}")
    lines.append("")
    lines.append("一、字段匹配")
    if report.missing_fields:
        lines.append("缺少以下三普法计算必需字段：")
        lines.extend(f"   - {name}" for name in report.missing_fields)
    if report.ambiguous_fields:
        lines.append("存在字段匹配歧义，请删掉重复字段或修改字段别名：")
        lines.extend(f"   - {item}" for item in report.ambiguous_fields)
    if not report.missing_fields and not report.ambiguous_fields:
        for indicator in membership_tool.STANDARD_INDICATOR_FIELDS:
            binding = report.bindings.get(indicator)
            if binding:
                source_note = "F字段中文值" if binding.is_f_field else "原始/别名字段"
                lines.append(
                    f"   - {indicator} -> {binding.source_field}（{binding.field_type}；{source_note}；匹配标签：{binding.matched_label}）"
                )
            elif indicator not in report.rule_set.indicators:
                lines.append(f"   - {indicator}: 当前农业区规则不参与计算")
            else:
                lines.append(f"   - {indicator}: 未提供（当前农业区允许缺省）")
    lines.append("")
    lines.append("二、空值检查")
    blank_total = sum(issue.count for issue in report.blank_issues)
    if blank_total:
        lines.append(f"发现空值 {blank_total} 个，不能转换。")
        for issue in report.blank_issues:
            sample = "、".join(str(oid) for oid in issue.sample_oids)
            lines.append(f"   - {issue.indicator} -> {issue.field_name}: {issue.count} 个；样例 OID：{sample}")
    else:
        lines.append("未发现必需字段空值。")
    lines.append("")
    lines.append("三、类别/数值合法性检查")
    invalid_total = sum(issue.count for issue in report.invalid_issues)
    if invalid_total:
        lines.append(f"发现无法转换的值 {invalid_total} 个，不能转换。")
        for issue in report.invalid_issues:
            sample = "、".join(str(oid) for oid in issue.sample_oids)
            lines.append(
                f"   - {issue.indicator} -> {issue.field_name}: 值“{issue.value_text}”共 {issue.count} 个；样例 OID：{sample}"
            )
            if issue.indicator in report.rule_set.concept_memberships:
                allowed = "、".join(sorted(report.rule_set.concept_memberships[issue.indicator]))
                lines.append(f"     允许值：{allowed}")
    else:
        lines.append("类别值和数值字段均可转换。")
    lines.append("")
    lines.append("四、输出说明")
    lines.append("输出为 GDB 面要素类，字段结构与第一步高标隶属度结果一致：13 个指标原值、13 个 F 隶属度、评价得分、质量等级。")
    lines.append("")
    lines.append("审查通过，可以提交转换。" if report.ok else "审查未通过，请先修正输入字段或值。")
    return "\n".join(lines)


def validate_conversion_source(
    source: membership_tool.VectorSource,
    rule_set: membership_tool.RuleSet,
) -> ConversionReport:
    require_runtime()
    dataset = membership_tool.source_dataset_path(source)
    if not arcpy.Exists(dataset):
        raise RuntimeError(f"输入数据不存在：{membership_tool.source_label(source)}")
    desc = arcpy.Describe(dataset)
    if getattr(desc, "shapeType", "") != "Polygon":
        raise RuntimeError("输入数据必须是面矢量。")
    feature_count = int(arcpy.management.GetCount(dataset)[0])
    bindings, missing_fields, ambiguous_fields = choose_indicator_bindings(source, rule_set)
    blank_issues: list[ConversionIssue] = []
    invalid_issues: list[ConversionIssue] = []
    if not missing_fields and not ambiguous_fields:
        blank_issues, invalid_issues = scan_conversion_issues(source, rule_set, bindings)
    ok = not missing_fields and not ambiguous_fields and not blank_issues and not invalid_issues
    draft = ConversionReport(
        ok=ok,
        source=source,
        rule_set=rule_set,
        feature_count=feature_count,
        bindings=bindings,
        missing_fields=missing_fields,
        ambiguous_fields=ambiguous_fields,
        blank_issues=blank_issues,
        invalid_issues=invalid_issues,
        text="",
    )
    return ConversionReport(
        ok=draft.ok,
        source=draft.source,
        rule_set=draft.rule_set,
        feature_count=draft.feature_count,
        bindings=draft.bindings,
        missing_fields=draft.missing_fields,
        ambiguous_fields=draft.ambiguous_fields,
        blank_issues=draft.blank_issues,
        invalid_issues=draft.invalid_issues,
        text=build_report_text(draft),
    )


def create_output_feature_class(job: ConversionJob) -> tuple[str, dict[str, str]]:
    membership_tool.delete_output_dataset(job.output_gdb, job.output_feature_name)
    job.output_gdb.parent.mkdir(parents=True, exist_ok=True)
    if not job.output_gdb.exists():
        arcpy.management.CreateFileGDB(str(job.output_gdb.parent), job.output_gdb.name)
    source_dataset = membership_tool.source_dataset_path(job.source)
    spatial_reference = arcpy.Describe(source_dataset).spatialReference
    output_fc = str(job.output_gdb / job.output_feature_name)
    arcpy.management.CreateFeatureclass(str(job.output_gdb), job.output_feature_name, "POLYGON", spatial_reference=spatial_reference)
    field_map = membership_tool.add_high_standard_result_fields(output_fc, job.rule_set, include_overlap_field=False)
    return output_fc, field_map


def converted_values_for_row(
    job: ConversionJob,
    values_by_indicator: dict[str, object],
) -> tuple[list[object], list[float | None], float, int]:
    raw_values: list[object] = []
    membership_values: list[float | None] = []
    score = 0.0
    for indicator in membership_tool.STANDARD_INDICATOR_FIELDS:
        source_value = values_by_indicator.get(indicator)
        if indicator not in job.rule_set.indicators or indicator not in job.bindings:
            raw_values.append(None)
            membership_values.append(None)
            continue
        if indicator in job.rule_set.concept_memberships:
            category = normalize_concept_value(job.rule_set, indicator, source_value)
            if category is None:
                raise RuntimeError(f"{indicator} 存在无法转换的类别值：{source_value}")
            membership = job.rule_set.concept_memberships[indicator][category]
            raw_values.append(category)
        else:
            numeric_value = parse_numeric_value(source_value)
            if numeric_value is None:
                raise RuntimeError(f"{indicator} 存在无法转换的数值：{source_value}")
            membership = membership_tool.membership_for_numeric(job.rule_set.numeric_rules[indicator], numeric_value)
            raw_values.append(numeric_value)
        membership_values.append(membership)
        score += membership * job.rule_set.weights[indicator]
    grade = membership_tool.grade_for_score(score, job.rule_set.grade_rules)
    return raw_values, membership_values, score, grade


def calculate_conversion_output(job: ConversionJob, logger: logging.Logger) -> tuple[str, int, dict[str, int]]:
    require_runtime()
    arcpy.env.overwriteOutput = True
    output_fc, field_map = create_output_feature_class(job)
    source_dataset = membership_tool.source_dataset_path(job.source)
    read_indicators = list(job.bindings.keys())
    read_fields = ["SHAPE@", *[job.bindings[indicator].source_field for indicator in read_indicators]]
    insert_fields = [
        "SHAPE@",
        *[field_map[indicator] for indicator in membership_tool.STANDARD_INDICATOR_FIELDS],
        *[field_map[f"F{indicator}"] for indicator in membership_tool.STANDARD_INDICATOR_FIELDS],
        field_map[membership_tool.RESULT_SCORE_FIELD],
        field_map[membership_tool.RESULT_GRADE_FIELD],
    ]
    converted = 0
    with arcpy.da.SearchCursor(source_dataset, read_fields) as search_cursor, arcpy.da.InsertCursor(output_fc, insert_fields) as insert_cursor:
        for row in search_cursor:
            values_by_indicator = {indicator: value for indicator, value in zip(read_indicators, row[1:])}
            raw_values, membership_values, score, grade = converted_values_for_row(job, values_by_indicator)
            insert_cursor.insertRow([row[0], *raw_values, *membership_values, score, grade])
            converted += 1

    required = [indicator for indicator in membership_tool.STANDARD_INDICATOR_FIELDS if indicator in job.rule_set.indicators and indicator in job.bindings]
    check_fields = [field_map[indicator] for indicator in required]
    check_fields += [field_map[f"F{indicator}"] for indicator in required]
    check_fields += [field_map[membership_tool.RESULT_SCORE_FIELD], field_map[membership_tool.RESULT_GRADE_FIELD]]
    ok, audit_text, audit_stats = membership_tool.build_blank_result_report(output_fc, check_fields, "国标转三普输出完整性审查")
    logger.info("输出完整性审计：\n%s", audit_text)
    if not ok:
        raise RuntimeError(f"转换后仍有结果字段空值：{audit_stats['missing_values']} 个。详情见日志。")
    logger.info("转换完成：%s；要素数：%s", output_fc, converted)
    return output_fc, converted, audit_stats


def setup_job_logger(logs_dir: Path, job_id: str) -> tuple[logging.Logger, Path]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"gb_to_sanpu_{timestamp_for_file()}_{job_id}.log"
    logger = logging.getLogger(f"gb_to_sanpu_arcpy.{job_id}")
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


class ConversionWorker(threading.Thread):
    def __init__(self, job_queue: queue.Queue, event_queue: queue.Queue, logs_dir: Path):
        super().__init__(daemon=True)
        self.job_queue = job_queue
        self.event_queue = event_queue
        self.logs_dir = logs_dir
        self.history_path = logs_dir / "gb_to_sanpu_history.jsonl"

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

    def process_job(self, job: ConversionJob) -> None:
        logger, log_path = setup_job_logger(self.logs_dir, job.job_id)
        record = {
            "job_id": job.job_id,
            "created_at": job.created_at,
            "input_source": membership_tool.source_label(job.source),
            "output_path": str(job.output_gdb / job.output_feature_name),
            "rule_path": str(job.rule_set.rule_path),
            "status": "running",
            "log_path": str(log_path),
        }
        try:
            logger.info("任务开始：%s", job.job_id)
            logger.info("输入：%s", membership_tool.source_label(job.source))
            logger.info("输出：%s", job.output_gdb / job.output_feature_name)
            logger.info("规则：%s", job.rule_set.rule_path)
            logger.info("审查报告：\n%s", job.validation_report)
            output_fc, converted, audit_stats = calculate_conversion_output(job, logger)
            record.update({"status": "success", "converted_count": converted, "audit_stats": audit_stats, "output_path": output_fc})
            append_history(self.history_path, record)
            self.send(
                "job_done",
                {
                    "job_id": job.job_id,
                    "message": f"转换完成：{output_fc}；要素数 {converted}",
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
                {
                    "job_id": job.job_id,
                    "message": f"转换失败：{exc}",
                    "log_path": str(log_path),
                },
            )
        finally:
            close_job_logger(logger)


class GbToSanpuApp:
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
        self.gdb_sources: list[membership_tool.VectorSource] = []
        self.source: membership_tool.VectorSource | None = None
        self.last_report: ConversionReport | None = None
        self.last_report_key: tuple | None = None

        self.job_queue: queue.Queue = queue.Queue()
        self.event_queue: queue.Queue = queue.Queue()
        self.worker = ConversionWorker(self.job_queue, self.event_queue, self.logs_dir)
        self.worker.start()

        if not self.embedded:
            self.root.title("辅助工具：国标字段转三普法隶属度")
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
        ttk.Label(source_row, text="输入面数据").pack(side=LEFT)
        ttk.Entry(source_row, textvariable=self.source_path_var).pack(side=LEFT, fill="x", expand=True, padx=5)
        ttk.Button(source_row, text="选择 shp", command=self.choose_shp).pack(side=LEFT, padx=3)
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

        report_frame = ttk.LabelFrame(container, text="3. 审查、转换和日志")
        report_frame.pack(fill="both", expand=True, pady=5)
        action_row = ttk.Frame(report_frame)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="开始审查", command=self.validate_current_input).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="提交转换任务", command=self.submit_job).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="查看历史记录", command=self.show_history).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="打开日志文件夹", command=self.open_logs_folder).pack(side=LEFT, padx=5, pady=4)
        ttk.Button(action_row, text="删除日志", command=self.delete_logs).pack(side=LEFT, padx=5, pady=4)
        if self.shared_status_text is None:
            self.status_text = Text(report_frame, height=24, wrap="word")
            self.status_text.pack(fill="both", expand=True, padx=5, pady=5)
        else:
            self.status_text = self.shared_status_text
            ttk.Label(report_frame, text="运行详细信息显示在窗口底部“详细信息”区域。").pack(anchor="w", padx=5, pady=5)
        self.log_status("国标转三普辅助工具已启动。请选择高标单元图、农业区和输出 GDB 后先审查。")

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

    def choose_shp(self) -> None:
        path = filedialog.askopenfilename(title="选择输入 shp", filetypes=[("Shapefile", "*.shp")])
        if not path:
            return
        self.source = membership_tool.make_vector_source("shp", Path(path))
        self.source_path_var.set(str(self.source.source_path))
        self.source_layer_var.set("")
        self.layer_combo["values"] = []
        self.gdb_sources = []
        self.last_report = None
        self.log_status(f"已选择输入 shp：{self.source.display_name}")

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
            membership_tool.source_label(self.source) if self.source else "",
            self.area_var.get(),
            self.output_gdb_var.get(),
            self.output_feature_var.get(),
        )

    def validate_current_input(self) -> None:
        if self.source is None:
            messagebox.showwarning("提示", "请先选择输入面数据。")
            return
        if not self.area_var.get().strip():
            messagebox.showwarning("提示", "请先选择国标二级农业区。")
            return
        try:
            rule_set = membership_tool.load_rule_set(self.rules_dir, self.area_var.get())
            self.log_status("开始审查字段匹配、空值、类别值和数值。")
            report = validate_conversion_source(self.source, rule_set)
        except Exception as exc:
            messagebox.showerror("审查失败", str(exc))
            return
        self.last_report = report
        self.last_report_key = self.report_key()
        self.show_report(report, ask_continue=False)
        if report.ok:
            self.log_status("审查通过，可以提交转换任务。")
        else:
            self.log_status("审查未通过，已在报告中列出问题。")

    def show_report(self, report: ConversionReport, ask_continue: bool) -> bool:
        window = Toplevel(self.root)
        window.title("国标转三普审查报告")
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
            ttk.Button(button_row, text="确认无误，继续转换", command=confirm).pack(side=LEFT, padx=5)
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
            messagebox.showwarning("提示", "请先选择输入面数据。")
            return
        output = self.output_settings()
        if output is None:
            return
        output_gdb, output_feature_name = output
        output_dataset = output_gdb / output_feature_name
        if output_dataset.resolve() == Path(membership_tool.source_dataset_path(self.source)).resolve():
            messagebox.showerror("输出错误", "输出结果不能覆盖输入数据。")
            return
        try:
            report = self.last_report
            if report is None or self.last_report_key != self.report_key():
                self.log_status("当前输入没有最新审查报告，正在重新审查。")
                self.validate_current_input()
                report = self.last_report
            if report is None:
                return
            if not report.ok:
                self.show_report(report, ask_continue=False)
                messagebox.showerror("检查未通过", "输入数据存在问题，不能转换。请先按报告修正。")
                return
        except Exception as exc:
            messagebox.showerror("提交失败", str(exc))
            return
        if not self.show_report(report, ask_continue=True):
            self.log_status("用户取消转换任务，未提交。")
            return
        job = ConversionJob(
            job_id=uuid.uuid4().hex[:8],
            source=self.source,
            output_gdb=output_gdb,
            output_feature_name=output_feature_name,
            rule_set=report.rule_set,
            bindings=report.bindings,
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
        history_path = self.logs_dir / "gb_to_sanpu_history.jsonl"
        if not history_path.exists():
            messagebox.showinfo("历史记录", "暂无历史记录。")
            return
        window = Toplevel(self.root)
        window.title("国标转三普历史记录")
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
                f"统计：{record.get('converted_count')}\n"
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
        if not messagebox.askyesno("确认删除", "确定删除国标转三普转换日志和历史记录吗？"):
            return
        deleted = 0
        for path in self.logs_dir.glob("gb_to_sanpu_*.log"):
            path.unlink()
            deleted += 1
        history_path = self.logs_dir / "gb_to_sanpu_history.jsonl"
        if history_path.exists():
            history_path.unlink()
            deleted += 1
        self.log_status(f"已删除 {deleted} 个国标转三普日志/历史文件。")

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
        self.log_status("国标转三普工具输入和参数已恢复为启动默认值。")


def main() -> int:
    try:
        require_runtime()
    except Exception as exc:
        root = Tk()
        root.withdraw()
        messagebox.showerror("缺少运行环境", str(exc))
        return 1
    root = Tk()
    GbToSanpuApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
