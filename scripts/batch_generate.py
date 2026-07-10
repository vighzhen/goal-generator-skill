# 负责从 JSON/CSV 批量读取编码任务并生成 Codex CLI /goal 指令。
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generate_goal import (
    ELEMENT_LABELS,
    ELEMENT_ORDER,
    INTERACTIVE_DEFAULTS,
    QUESTION_EXAMPLES,
    _GoalFields,
    _extract_labeled_fields,
    analyze_description,
    build_task_profile,
    build_question_pack,
    build_goal_json_draft,
    check_redaction,
    render_goal_text,
    score_description,
    suggest_goal_fields,
)

SUPPORTED_SUFFIXES: tuple[str, ...] = (
    ".json",
    ".jsonl",
    ".csv",
    ".yaml",
    ".yml",
    ".md",
    ".markdown",
)
SLUG_MAX_LENGTH = 50
DEFAULT_NAME_PREFIX = "task"
GOAL_FILE_SUFFIX = ".txt"
TASK_SEPARATOR = "\n\n"
SUMMARY_TEMPLATE = "处理完成：成功 {success_count} 个，跳过 {skipped_count} 个，总耗时 {elapsed_seconds:.2f} 秒。"
DEFAULTS_JSON_ENV = "GOAL_GENERATOR_DEFAULTS_JSON"
FIELD_STATUS_COLUMNS: tuple[str, ...] = (
    "task_name",
    "status",
    "readiness_score",
    "readiness_level",
    "risk_level",
    "risk_score",
    "missing_before_defaults",
    "defaulted",
    "skip_reason",
    "skip_suggestion",
    *(f"{key}_status" for key in ELEMENT_ORDER),
)
CLAUSE_SPLIT_PATTERN = re.compile(r"[，。；;\n]+")
FALLBACK_HINTS: dict[str, tuple[str, ...]] = {
    "verification": ("验证", "运行", "执行", "确认", "检查", "通过", "跑测试"),
    "constraints": ("不改", "不修改", "不改变", "不引入", "禁止", "不得", "保持", "兼容"),
    "boundaries": ("边界", "范围", "仅", "只", "目录", "排除", "src/", "tests/"),
    "iteration": ("迭代", "每个", "每次", "逐个", "commit", "提交", "预期"),
    "blocked": ("受阻", "阻塞", "停下", "问人", "问我", "跳过", "无法", "缺少"),
}
MARKDOWN_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "任务", "任务名", "名称", "title", "标题"),
    "description": ("description", "描述", "需求", "任务描述", "request", "prompt"),
    "outcome": ("outcome", "目标结果", "目标", "交付"),
    "verification": ("verification", "验证方式", "验证", "验收"),
    "constraints": ("constraints", "约束", "限制", "不能"),
    "boundaries": ("boundaries", "边界", "范围", "目录"),
    "iteration": ("iteration", "迭代策略", "迭代", "提交"),
    "blocked": ("blocked", "受阻停止条件", "阻塞", "停下", "跳过"),
}


@dataclass(frozen=True)
class TaskSpec:
    name: str
    description: str
    fields: dict[str, str]
    load_error: str = ""


@dataclass(frozen=True)
class PreparedTask:
    task: TaskSpec
    goal: _GoalFields
    present_keys: list[str]
    missing_before_defaults: list[str]
    defaulted_keys: list[str]


@dataclass(frozen=True)
class TaskOutput:
    task_name: str
    task_description: str
    content: str
    file_slug: str
    present_keys: list[str]
    missing_before_defaults: list[str]
    defaulted_keys: list[str]


@dataclass(frozen=True)
class SkippedTask:
    task_name: str
    reason: str
    suggestion: str


@dataclass(frozen=True)
class TaskProfileSummary:
    task: TaskSpec
    profile: dict[str, object]


@dataclass(frozen=True)
class TaskScoreSummary:
    task: TaskSpec
    score: dict[str, object]


@dataclass(frozen=True)
class TaskQuestionPack:
    task: TaskSpec
    pack: dict[str, object]


@dataclass(frozen=True)
class FieldsExport:
    task_name: str
    output_path: str
    review_required: list[str]


@dataclass(frozen=True)
class GoalJsonDraft:
    task_name: str
    file_slug: str
    draft: dict[str, object]


@dataclass(frozen=True)
class TaskRedactionSummary:
    task: TaskSpec
    check: dict[str, object]


@dataclass(frozen=True)
class BatchStats:
    success_count: int
    skipped_count: int
    elapsed_seconds: float


def main(argv: list[str] | None = None) -> int:
    """执行批量生成命令行入口。

    Args:
        argv: 可选命令行参数；为空时读取进程参数。

    Returns:
        进程退出码，0 表示成功，1 表示输入文件错误。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    start_time = time.perf_counter()
    try:
        tasks = _load_tasks_from_input(_input_value_from_args(args), args.stdin_format)
        tasks = _filter_tasks(tasks, args.filter)
        tasks = _sort_tasks(tasks, args.sort_by)
        tasks = _limit_tasks(tasks, args.limit)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"读取输入失败：{error}", file=sys.stderr)
        return 1
    if args.draft_jsonl:
        return _run_draft_jsonl_mode(tasks, args)
    if args.redaction_report_md:
        return _run_redaction_report_mode(tasks, args)
    if args.questions_md:
        return _run_questions_markdown_mode(tasks, args)
    if args.review_board_md:
        return _run_review_board_mode(tasks, args)
    if args.risk_report_md:
        return _run_risk_report_mode(tasks, args)
    if args.export_fields_json:
        return _run_export_fields_json_mode(tasks, args)
    if args.score_summary or args.score_report_md or args.fail_below_score is not None:
        return _run_score_summary_mode(tasks, args)
    if args.profile_summary:
        return _run_profile_summary_mode(tasks, args)
    if args.list_tasks:
        return _run_list_tasks_mode(tasks, args)
    try:
        default_values = _load_default_values(args.defaults_json)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"读取输入失败：{error}", file=sys.stderr)
        return 1
    if args.field_status_csv:
        return _run_field_status_csv_mode(tasks, args, default_values, start_time)
    dry_run = args.dry_run or args.check
    strict = args.strict or args.check
    summary_only = args.summary_only or args.check
    fail_on_skipped = args.fail_on_skipped or args.check
    outputs, skipped_tasks = _process_tasks(tasks, dry_run, strict, args.dedupe, default_values, args.verbose)
    _write_outputs(outputs, args.output_dir, args.output_file, summary_only)
    elapsed_seconds = time.perf_counter() - start_time
    stats = BatchStats(len(outputs), len(skipped_tasks), elapsed_seconds)
    if args.index_md:
        _write_output_index_markdown(outputs, stats, Path(args.index_md), args.output_dir, args.output_file)
    if args.report_json:
        _write_report_json(
            outputs,
            skipped_tasks,
            stats,
            Path(args.report_json),
            args.output_dir,
            args.output_file,
            args.include_profile,
        )
    if args.report_md:
        _write_report_markdown(outputs, skipped_tasks, stats, Path(args.report_md), args.output_dir, args.output_file)
    if args.missing_report_md:
        _write_missing_report_markdown(outputs, skipped_tasks, stats, Path(args.missing_report_md))
    print(_format_summary(stats))
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量生成 Codex CLI /goal 指令。")
    parser.add_argument("input_path", nargs="?", help="输入文件路径，可替代 --input。")
    parser.add_argument("--input", help="输入文件路径，支持 .json、.jsonl、.csv、.yaml、.yml、.md 或 .markdown。")
    parser.add_argument("--stdin-format", choices=("json", "jsonl"), default="jsonl", help="当输入为 - 时使用的 stdin 格式。")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--output-dir", help="输出目录，每个任务生成一个 .txt 文件。")
    output_group.add_argument("--output-file", help="输出到单个文件。")
    parser.add_argument("--defaults-json", help="JSON 默认值文件，用于覆盖缺失 6 要素的默认填充。")
    parser.add_argument("--report-json", help="把批量处理结果、缺失要素和跳过原因写入 JSON 报告。")
    parser.add_argument("--report-md", help="把批量处理结果写入便于人工评审的 Markdown 报告。")
    parser.add_argument("--missing-report-md", help="把每个任务的缺失要素、风险和补全建议写入 Markdown 报告。")
    parser.add_argument("--field-status-csv", help="把每个任务 6 要素的 present/defaulted/missing/skipped 状态写入 CSV。")
    parser.add_argument("--index-md", help="为批量输出产物写入 Markdown 导航索引。")
    parser.add_argument("--include-profile", action="store_true", help="在 JSON 报告中附加任务类型、风险评分和追问策略画像。")
    parser.add_argument("--filter", help="按正则筛选任务名或描述，只处理匹配的任务。")
    parser.add_argument("--sort-by", choices=("input", "name"), default="input", help="批量任务输出顺序，默认保持输入顺序。")
    parser.add_argument("--limit", type=int, help="只处理前 N 个任务，适合大清单试跑。")
    parser.add_argument("--list-tasks", action="store_true", help="只预览将要处理的任务名称，不生成 /goal。")
    parser.add_argument("--profile-summary", action="store_true", help="只输出批量任务类型、风险和缺失要素摘要，不生成 /goal。")
    parser.add_argument("--score-summary", action="store_true", help="只输出批量任务 /goal 可执行度评分摘要，不生成 /goal。")
    parser.add_argument("--score-report-md", help="把批量任务 /goal 可执行度评分写入 Markdown 报告。")
    parser.add_argument("--review-board-md", help="按可执行度等级分组写出批量任务 Markdown 评审看板。")
    parser.add_argument("--risk-report-md", help="把批量任务风险等级、风险因素和缓解建议写入 Markdown 报告。")
    parser.add_argument("--questions-md", help="把每个任务可直接发送给需求方的缺失要素追问写入 Markdown 包。")
    parser.add_argument("--redaction-report-md", help="把批量任务中的敏感信息发现、脱敏预览和处理建议写入 Markdown 报告。")
    parser.add_argument("--draft-jsonl", help="把每个任务的 Goal JSON 草稿写入单个 JSONL 文件，适合流水线逐行消费。")
    parser.add_argument("--fail-below-score", type=int, help="当任一任务可执行度分数低于阈值时返回退出码 1，适合 CI 门禁。")
    parser.add_argument("--export-fields-json", help="为每个任务导出可编辑的 6 要素字段建议 JSON 到指定目录。")
    parser.add_argument("--dedupe", action="store_true", help="按任务名和描述跳过重复任务。")
    parser.add_argument("--fail-on-skipped", action="store_true", help="有跳过任务时以退出码 1 结束，适合 CI 门禁。")
    parser.add_argument("--summary-only", action="store_true", help="抑制任务正文 stdout，仅输出最终摘要。")
    parser.add_argument("--dry-run", action="store_true", help="只分析要素完整度，不生成指令。")
    parser.add_argument("--check", action="store_true", help="校验输入任务文件，等价于 --dry-run --strict --summary-only --fail-on-skipped。")
    parser.add_argument("--strict", action="store_true", help="缺失 6 要素时跳过任务，不使用默认填充。")
    parser.add_argument("--verbose", action="store_true", help="打印详细处理日志。")
    return parser



def _input_value_from_args(args: argparse.Namespace) -> str:
    input_value = args.input or args.input_path
    if not input_value:
        raise ValueError("必须提供 --input 或位置输入文件路径")
    return input_value


def _load_tasks_from_input(input_value: str, stdin_format: str) -> list[TaskSpec]:
    if input_value == "-":
        return _load_stdin_tasks(sys.stdin.read(), stdin_format)
    return _load_tasks(Path(input_value))


def _load_stdin_tasks(text: str, stdin_format: str) -> list[TaskSpec]:
    if stdin_format == "json":
        return _tasks_from_json_data(json.loads(text))
    if stdin_format == "jsonl":
        return _load_jsonl_lines(text.splitlines())
    raise ValueError(f"不支持的 stdin 格式：{stdin_format}")


def _filter_tasks(tasks: list[TaskSpec], pattern: str | None) -> list[TaskSpec]:
    if not pattern:
        return tasks
    try:
        matcher = re.compile(pattern)
    except re.error as error:
        raise ValueError(f"--filter 不是有效正则：{error}") from error
    return [task for task in tasks if _task_matches_filter(task, matcher)]


def _task_matches_filter(task: TaskSpec, matcher: re.Pattern[str]) -> bool:
    return bool(matcher.search(task.name) or matcher.search(task.description))


def _sort_tasks(tasks: list[TaskSpec], sort_by: str) -> list[TaskSpec]:
    if sort_by == "input":
        return tasks
    if sort_by == "name":
        return sorted(tasks, key=lambda task: task.name)
    raise ValueError(f"不支持的排序方式：{sort_by}")


def _limit_tasks(tasks: list[TaskSpec], limit: int | None) -> list[TaskSpec]:
    if limit is None:
        return tasks
    if limit < 1:
        raise ValueError("--limit 必须是大于 0 的整数")
    return tasks[:limit]


def _run_list_tasks_mode(tasks: list[TaskSpec], args: argparse.Namespace) -> int:
    start_time = time.perf_counter()
    listed_tasks, skipped_tasks = _dedupe_preview_tasks(tasks, args.dedupe)
    summary_only = args.summary_only or args.check
    if not summary_only:
        print("\n".join(f"{index}. {task.name}" for index, task in enumerate(listed_tasks, start=1)))
    stats = BatchStats(len(listed_tasks), len(skipped_tasks), time.perf_counter() - start_time)
    if args.report_json:
        _write_task_list_report(listed_tasks, skipped_tasks, stats, Path(args.report_json), args.include_profile)
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _dedupe_preview_tasks(tasks: list[TaskSpec], dedupe: bool) -> tuple[list[TaskSpec], list[SkippedTask]]:
    if not dedupe:
        return tasks, []
    listed_tasks: list[TaskSpec] = []
    skipped_tasks: list[SkippedTask] = []
    seen_task_keys: set[str] = set()
    for task in tasks:
        if _is_duplicate_task(task, seen_task_keys):
            reason = "重复任务：任务名和描述已出现过"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        listed_tasks.append(task)
    return listed_tasks, skipped_tasks


def _run_profile_summary_mode(tasks: list[TaskSpec], args: argparse.Namespace) -> int:
    start_time = time.perf_counter()
    profiled_tasks, skipped_tasks = _build_profile_summaries(tasks, args.dedupe)
    summary_only = args.summary_only or args.check
    if not summary_only:
        _print_profile_summary(profiled_tasks)
    stats = BatchStats(len(profiled_tasks), len(skipped_tasks), time.perf_counter() - start_time)
    if args.report_json:
        _write_profile_summary_report(profiled_tasks, skipped_tasks, stats, Path(args.report_json))
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _run_score_summary_mode(tasks: list[TaskSpec], args: argparse.Namespace) -> int:
    try:
        score_gate_threshold = _score_gate_threshold(args.fail_below_score)
    except ValueError as error:
        print(f"参数错误：{error}", file=sys.stderr)
        return 1
    start_time = time.perf_counter()
    scored_tasks, skipped_tasks = _build_score_summaries(tasks, args.dedupe)
    summary_only = args.summary_only or args.check
    if not summary_only:
        _print_score_summary(scored_tasks)
    stats = BatchStats(len(scored_tasks), len(skipped_tasks), time.perf_counter() - start_time)
    if args.report_json:
        _write_score_summary_report(scored_tasks, skipped_tasks, stats, Path(args.report_json), score_gate_threshold)
    if args.score_report_md:
        _write_score_report_markdown(scored_tasks, skipped_tasks, stats, Path(args.score_report_md), score_gate_threshold)
    print(_format_summary(stats))
    score_gate_failures = _score_gate_failures(scored_tasks, score_gate_threshold)
    if score_gate_failures:
        _print_score_gate_failures(score_gate_failures, score_gate_threshold)
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    if score_gate_failures:
        return 1
    return 0


def _score_gate_threshold(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 0 or value > 100:
        raise ValueError("--fail-below-score 必须是 0 到 100 之间的整数")
    return value


def _score_gate_failures(scored_tasks: list[TaskScoreSummary], threshold: int | None) -> list[TaskScoreSummary]:
    if threshold is None:
        return []
    return [scored_task for scored_task in scored_tasks if _score_value(scored_task.score) < threshold]


def _score_value(score: dict[str, object]) -> int:
    value = score.get("readiness_score", 0)
    return value if isinstance(value, int) else 0


def _print_score_gate_failures(failures: list[TaskScoreSummary], threshold: int | None) -> None:
    if threshold is None:
        return
    print(f"可执行度门禁失败：{len(failures)} 个任务低于 {threshold} 分。", file=sys.stderr)
    for failure in failures:
        score = failure.score
        print(
            f"- {failure.task.name}: {score.get('readiness_score', '')} "
            f"({score.get('readiness_level', 'unknown')})",
            file=sys.stderr,
        )


def _run_review_board_mode(tasks: list[TaskSpec], args: argparse.Namespace) -> int:
    start_time = time.perf_counter()
    scored_tasks, skipped_tasks = _build_score_summaries(tasks, args.dedupe)
    stats = BatchStats(len(scored_tasks), len(skipped_tasks), time.perf_counter() - start_time)
    board_path = Path(args.review_board_md)
    _write_review_board_markdown(scored_tasks, skipped_tasks, stats, board_path)
    if args.report_json:
        _write_score_summary_report(scored_tasks, skipped_tasks, stats, Path(args.report_json))
    if not args.summary_only:
        print(f"已写入评审看板：{board_path}")
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _run_questions_markdown_mode(tasks: list[TaskSpec], args: argparse.Namespace) -> int:
    start_time = time.perf_counter()
    question_packs, skipped_tasks = _build_question_packs(tasks, args.dedupe)
    stats = BatchStats(len(question_packs), len(skipped_tasks), time.perf_counter() - start_time)
    questions_path = Path(args.questions_md)
    _write_questions_markdown(question_packs, skipped_tasks, stats, questions_path)
    if args.report_json:
        _write_question_pack_report(question_packs, skipped_tasks, stats, Path(args.report_json))
    if not args.summary_only:
        print(f"已写入追问 Markdown 包：{questions_path}")
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _run_risk_report_mode(tasks: list[TaskSpec], args: argparse.Namespace) -> int:
    start_time = time.perf_counter()
    profiled_tasks, skipped_tasks = _build_profile_summaries(tasks, args.dedupe)
    stats = BatchStats(len(profiled_tasks), len(skipped_tasks), time.perf_counter() - start_time)
    report_path = Path(args.risk_report_md)
    _write_risk_report_markdown(profiled_tasks, skipped_tasks, stats, report_path)
    if args.report_json:
        _write_profile_summary_report(profiled_tasks, skipped_tasks, stats, Path(args.report_json))
    if not args.summary_only:
        print(f"已写入风险报告：{report_path}")
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _run_draft_jsonl_mode(tasks: list[TaskSpec], args: argparse.Namespace) -> int:
    start_time = time.perf_counter()
    drafts, skipped_tasks = _write_draft_jsonl(tasks, Path(args.draft_jsonl), args.dedupe)
    stats = BatchStats(len(drafts), len(skipped_tasks), time.perf_counter() - start_time)
    if args.report_json:
        _write_draft_jsonl_report(drafts, skipped_tasks, stats, Path(args.report_json))
    if not (args.summary_only or args.check):
        print(f"已写入 Goal JSONL 草稿：{args.draft_jsonl}")
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _run_redaction_report_mode(tasks: list[TaskSpec], args: argparse.Namespace) -> int:
    start_time = time.perf_counter()
    redaction_summaries, skipped_tasks = _build_redaction_summaries(tasks, args.dedupe)
    stats = BatchStats(len(redaction_summaries), len(skipped_tasks), time.perf_counter() - start_time)
    report_path = Path(args.redaction_report_md)
    _write_redaction_report_markdown(redaction_summaries, skipped_tasks, stats, report_path)
    if args.report_json:
        _write_redaction_summary_report(redaction_summaries, skipped_tasks, stats, Path(args.report_json))
    if not args.summary_only:
        print(f"已写入敏感信息报告：{report_path}")
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _build_redaction_summaries(
    tasks: list[TaskSpec],
    dedupe: bool,
) -> tuple[list[TaskRedactionSummary], list[SkippedTask]]:
    summaries: list[TaskRedactionSummary] = []
    skipped_tasks: list[SkippedTask] = []
    seen_task_keys: set[str] = set()
    for task in tasks:
        if dedupe and _is_duplicate_task(task, seen_task_keys):
            reason = "重复任务：任务名和描述已出现过"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        if task.load_error:
            skipped_tasks.append(SkippedTask(task.name, task.load_error, _skip_suggestion(task.load_error)))
            continue
        if not task.description:
            reason = "缺少 description"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        summaries.append(TaskRedactionSummary(task, check_redaction(task.description)))
    return summaries, skipped_tasks


def _write_draft_jsonl(
    tasks: list[TaskSpec],
    output_path: Path,
    dedupe: bool,
) -> tuple[list[GoalJsonDraft], list[SkippedTask]]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    drafts: list[GoalJsonDraft] = []
    skipped_tasks: list[SkippedTask] = []
    seen_task_keys: set[str] = set()
    used_slugs: set[str] = set()
    with output_path.open("w", encoding="utf-8") as output_file:
        for index, task in enumerate(tasks, start=1):
            if dedupe and _is_duplicate_task(task, seen_task_keys):
                reason = "重复任务：任务名和描述已出现过"
                skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
                continue
            if task.load_error:
                skipped_tasks.append(SkippedTask(task.name, task.load_error, _skip_suggestion(task.load_error)))
                continue
            if not task.description:
                reason = "缺少 description"
                skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
                continue
            slug = _unique_slug(task.name, index, used_slugs)
            draft = _goal_json_draft_payload(task, slug)
            output_file.write(json.dumps(draft, ensure_ascii=False, separators=(",", ":")) + "\n")
            drafts.append(GoalJsonDraft(task.name, slug, draft))
    return drafts, skipped_tasks


def _goal_json_draft_payload(task: TaskSpec, slug: str) -> dict[str, object]:
    draft = build_goal_json_draft(task.description, task.fields, "input_fields")
    return {
        "name": task.name,
        "file_slug": slug,
        "description": task.description,
        **draft,
    }


def _run_export_fields_json_mode(tasks: list[TaskSpec], args: argparse.Namespace) -> int:
    start_time = time.perf_counter()
    exports, skipped_tasks = _export_fields_json(tasks, Path(args.export_fields_json), args.dedupe, args.summary_only)
    stats = BatchStats(len(exports), len(skipped_tasks), time.perf_counter() - start_time)
    if args.report_json:
        _write_fields_export_report(exports, skipped_tasks, stats, Path(args.report_json))
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _run_field_status_csv_mode(
    tasks: list[TaskSpec],
    args: argparse.Namespace,
    default_values: dict[str, str],
    start_time: float,
) -> int:
    strict = args.strict or args.check
    outputs, skipped_tasks = _process_tasks(tasks, True, strict, args.dedupe, default_values, args.verbose)
    stats = BatchStats(len(outputs), len(skipped_tasks), time.perf_counter() - start_time)
    csv_path = Path(args.field_status_csv)
    _write_field_status_csv(outputs, skipped_tasks, csv_path)
    if args.report_json:
        _write_report_json(outputs, skipped_tasks, stats, Path(args.report_json), None, None, args.include_profile)
    if not args.summary_only:
        print(f"已写入字段状态 CSV：{csv_path}")
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _export_fields_json(
    tasks: list[TaskSpec],
    output_dir: Path,
    dedupe: bool,
    summary_only: bool = False,
) -> tuple[list[FieldsExport], list[SkippedTask]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    exports: list[FieldsExport] = []
    skipped_tasks: list[SkippedTask] = []
    seen_task_keys: set[str] = set()
    used_slugs: set[str] = set()
    for index, task in enumerate(tasks, start=1):
        if dedupe and _is_duplicate_task(task, seen_task_keys):
            reason = "重复任务：任务名和描述已出现过"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        if task.load_error:
            skipped_tasks.append(SkippedTask(task.name, task.load_error, _skip_suggestion(task.load_error)))
            continue
        if not task.description:
            reason = "缺少 description"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        slug = _unique_slug(task.name, index, used_slugs)
        export_path = output_dir / f"{slug}.json"
        export_payload = _fields_export_payload(task, slug)
        export_path.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        review_required = _review_required_fields(export_payload)
        exports.append(FieldsExport(task.name, str(export_path), review_required))
        if not summary_only:
            print(f"已导出字段建议：{task.name} -> {export_path}")
    return exports, skipped_tasks


def _fields_export_payload(task: TaskSpec, slug: str) -> dict[str, object]:
    suggestion = suggest_goal_fields(task.description)
    fields = _object_string_mapping(suggestion.get("fields", {}))
    sources = _object_string_mapping(suggestion.get("sources", {}))
    for key, value in task.fields.items():
        fields[key] = value
        sources[key] = "input_fields"
    review_required = [key for key in ELEMENT_ORDER if sources.get(key) == "recommended_direction"]
    return {
        "name": task.name,
        "description": task.description,
        "file_slug": slug,
        "fields": {key: fields.get(key, "") for key in ELEMENT_ORDER},
        "sources": {key: sources.get(key, "unknown") for key in ELEMENT_ORDER},
        "missing": suggestion.get("missing", []),
        "present": suggestion.get("present", {}),
        "task_type": suggestion.get("task_type", {}),
        "score": suggestion.get("score", {}),
        "review_required": review_required,
        "note": "review_required 字段来自推荐方向，执行 --generate --from-json 前请按真实项目情况复核；input_fields 表示批量输入中显式提供的字段。",
    }


def _object_string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(raw_value) for key, raw_value in value.items() if raw_value is not None}


def _review_required_fields(export_payload: dict[str, object]) -> list[str]:
    review_required = export_payload.get("review_required", [])
    if not isinstance(review_required, list):
        return []
    return [key for key in review_required if isinstance(key, str) and key in ELEMENT_ORDER]


def _build_profile_summaries(
    tasks: list[TaskSpec],
    dedupe: bool,
) -> tuple[list[TaskProfileSummary], list[SkippedTask]]:
    profiled_tasks: list[TaskProfileSummary] = []
    skipped_tasks: list[SkippedTask] = []
    seen_task_keys: set[str] = set()
    for task in tasks:
        if dedupe and _is_duplicate_task(task, seen_task_keys):
            reason = "重复任务：任务名和描述已出现过"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        if task.load_error:
            skipped_tasks.append(SkippedTask(task.name, task.load_error, _skip_suggestion(task.load_error)))
            continue
        if not task.description:
            reason = "缺少 description"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        profiled_tasks.append(TaskProfileSummary(task, build_task_profile(task.description)))
    return profiled_tasks, skipped_tasks


def _build_score_summaries(
    tasks: list[TaskSpec],
    dedupe: bool,
) -> tuple[list[TaskScoreSummary], list[SkippedTask]]:
    scored_tasks: list[TaskScoreSummary] = []
    skipped_tasks: list[SkippedTask] = []
    seen_task_keys: set[str] = set()
    for task in tasks:
        if dedupe and _is_duplicate_task(task, seen_task_keys):
            reason = "重复任务：任务名和描述已出现过"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        if task.load_error:
            skipped_tasks.append(SkippedTask(task.name, task.load_error, _skip_suggestion(task.load_error)))
            continue
        if not task.description:
            reason = "缺少 description"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        scored_tasks.append(TaskScoreSummary(task, score_description(task.description)))
    return scored_tasks, skipped_tasks


def _build_question_packs(
    tasks: list[TaskSpec],
    dedupe: bool,
) -> tuple[list[TaskQuestionPack], list[SkippedTask]]:
    question_packs: list[TaskQuestionPack] = []
    skipped_tasks: list[SkippedTask] = []
    seen_task_keys: set[str] = set()
    for task in tasks:
        if dedupe and _is_duplicate_task(task, seen_task_keys):
            reason = "重复任务：任务名和描述已出现过"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        if task.load_error:
            skipped_tasks.append(SkippedTask(task.name, task.load_error, _skip_suggestion(task.load_error)))
            continue
        if not task.description:
            reason = "缺少 description"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        question_packs.append(TaskQuestionPack(task, build_question_pack(task.description)))
    return question_packs, skipped_tasks


def _print_profile_summary(profiled_tasks: list[TaskProfileSummary]) -> None:
    print("序号\t任务\t类型\t风险\t缺失要素")
    for index, profiled_task in enumerate(profiled_tasks, start=1):
        print(_profile_summary_line(index, profiled_task))


def _print_score_summary(scored_tasks: list[TaskScoreSummary]) -> None:
    print("序号\t任务\t分数\t等级\t风险\t缺失要素")
    for index, scored_task in enumerate(scored_tasks, start=1):
        print(_score_summary_line(index, scored_task))


def _profile_summary_line(index: int, profiled_task: TaskProfileSummary) -> str:
    profile = profiled_task.profile
    task_type = profile.get("task_type", {})
    task_label = task_type.get("label", "未知") if isinstance(task_type, dict) else "未知"
    risk_level = str(profile.get("risk_level", "unknown"))
    risk_score = str(profile.get("risk_score", ""))
    missing = profile.get("missing", [])
    missing_keys = [key for key in missing if isinstance(key, str) and key in ELEMENT_ORDER] if isinstance(missing, list) else []
    missing_labels = _format_labels(missing_keys)
    return f"{index}\t{profiled_task.task.name}\t{task_label}\t{risk_level}({risk_score})\t{missing_labels}"


def _score_summary_line(index: int, scored_task: TaskScoreSummary) -> str:
    score = scored_task.score
    readiness_score = str(score.get("readiness_score", ""))
    readiness_level = str(score.get("readiness_level", "unknown"))
    risk_level = str(score.get("risk_level", "unknown"))
    risk_score = str(score.get("risk_score", ""))
    missing = score.get("missing", [])
    missing_keys = [key for key in missing if isinstance(key, str) and key in ELEMENT_ORDER] if isinstance(missing, list) else []
    missing_labels = _format_labels(missing_keys)
    return f"{index}\t{scored_task.task.name}\t{readiness_score}\t{readiness_level}\t{risk_level}({risk_score})\t{missing_labels}"


def _load_default_values(defaults_json: str | None) -> dict[str, str]:
    defaults = dict(INTERACTIVE_DEFAULTS)
    defaults_path = defaults_json or os.environ.get(DEFAULTS_JSON_ENV)
    if not defaults_path:
        return defaults
    data = json.loads(Path(defaults_path).read_text(encoding="utf-8"))
    raw_defaults = data.get("fields", data) if isinstance(data, dict) else data
    overrides = _fields_from_mapping(raw_defaults)
    if not overrides:
        raise ValueError("--defaults-json 必须包含至少一个 6 要素默认值")
    defaults.update(overrides)
    return defaults


def _load_tasks(input_path: Path) -> list[TaskSpec]:
    suffix = input_path.suffix.lower()
    if suffix == ".json":
        return _load_json_tasks(input_path)
    if suffix == ".jsonl":
        return _load_jsonl_tasks(input_path)
    if suffix == ".csv":
        return _load_csv_tasks(input_path)
    if suffix in {".yaml", ".yml"}:
        return _load_yaml_tasks(input_path)
    if suffix in {".md", ".markdown"}:
        return _load_markdown_tasks(input_path)
    supported = "、".join(SUPPORTED_SUFFIXES)
    raise ValueError(f"不支持的输入格式：{suffix}，仅支持 {supported}")


def _load_json_tasks(input_path: Path) -> list[TaskSpec]:
    return _tasks_from_json_data(json.loads(input_path.read_text(encoding="utf-8")))


def _tasks_from_json_data(data: Any) -> list[TaskSpec]:
    if not isinstance(data, list):
        raise ValueError("JSON 顶层必须是任务数组")
    tasks: list[TaskSpec] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            tasks.append(_invalid_task(index, "", "JSON 任务必须是对象"))
            continue
        tasks.append(_task_from_json_item(item, index))
    return tasks


def _task_from_json_item(item: dict[str, Any], index: int) -> TaskSpec:
    name = _string_value(item.get("name")) or _default_task_name(index)
    description = _string_value(item.get("description"))
    fields = _fields_from_mapping(item.get("fields"))
    return TaskSpec(name=name, description=description, fields=fields)


def _load_jsonl_tasks(input_path: Path) -> list[TaskSpec]:
    return _load_jsonl_lines(input_path.read_text(encoding="utf-8").splitlines())


def _load_jsonl_lines(lines: list[str]) -> list[TaskSpec]:
    tasks: list[TaskSpec] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        tasks.append(_task_from_jsonl_line(stripped, line_number, len(tasks) + 1))
    if not tasks:
        raise ValueError("JSONL 文件没有可读取的任务行")
    return tasks


def _task_from_jsonl_line(line: str, line_number: int, task_index: int) -> TaskSpec:
    try:
        item = json.loads(line)
    except json.JSONDecodeError as error:
        return _invalid_task(task_index, f"line-{line_number}", f"JSONL 第 {line_number} 行解析失败：{error.msg}")
    if not isinstance(item, dict):
        return _invalid_task(task_index, f"line-{line_number}", f"JSONL 第 {line_number} 行必须是对象")
    return _task_from_json_item(item, task_index)


def _load_csv_tasks(input_path: Path) -> list[TaskSpec]:
    with input_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError("CSV 文件缺少表头")
        return [_task_from_csv_row(row, index) for index, row in enumerate(reader, start=1)]


def _load_yaml_tasks(input_path: Path) -> list[TaskSpec]:
    items = _parse_yaml_task_items(input_path.read_text(encoding="utf-8").splitlines())
    if not items:
        raise ValueError("YAML 文件没有可读取的任务项")
    return [_task_from_yaml_item(item, index) for index, item in enumerate(items, start=1)]


def _parse_yaml_task_items(lines: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_fields = False
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped.startswith("- "):
            if current is not None:
                items.append(current)
            current = {"fields": {}}
            in_fields = _consume_yaml_entry(current, stripped[2:].strip(), False, line_number)
            continue
        if current is None:
            raise ValueError("YAML 顶层必须是任务列表")
        if indent <= 2:
            in_fields = False
        in_fields = _consume_yaml_entry(current, stripped, in_fields and indent >= 4, line_number) or in_fields
    if current is not None:
        items.append(current)
    return items


def _consume_yaml_entry(item: dict[str, Any], entry: str, in_fields: bool, line_number: int) -> bool:
    if not entry:
        return in_fields
    key, value = _yaml_key_value(entry, line_number)
    canonical_key = _canonical_yaml_key(key)
    if canonical_key == "fields" and not value:
        item.setdefault("fields", {})
        return True
    _assign_yaml_value(item, canonical_key, value, in_fields)
    return False


def _yaml_key_value(entry: str, line_number: int) -> tuple[str, str]:
    if ":" not in entry:
        raise ValueError(f"YAML 第 {line_number} 行缺少 key: value 结构")
    key, value = entry.split(":", 1)
    return key.strip(), _yaml_scalar(value.strip())


def _yaml_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _canonical_yaml_key(key: str) -> str:
    normalized = re.sub(r"[`*_\\s-]+", "", key.lower())
    for canonical, aliases in MARKDOWN_COLUMN_ALIASES.items():
        alias_set = {re.sub(r"[`*_\\s-]+", "", alias.lower()) for alias in aliases}
        if normalized in alias_set:
            return canonical
    return normalized


def _assign_yaml_value(item: dict[str, Any], key: str, value: str, in_fields: bool) -> None:
    if in_fields or key in ELEMENT_ORDER:
        if key in ELEMENT_ORDER:
            item.setdefault("fields", {})[key] = value
        return
    if key in {"name", "description"}:
        item[key] = value


def _task_from_yaml_item(item: dict[str, Any], index: int) -> TaskSpec:
    name = _string_value(item.get("name")) or _default_task_name(index)
    description = _string_value(item.get("description"))
    fields = _fields_from_mapping(item.get("fields"))
    return TaskSpec(name=name, description=description, fields=fields)


def _load_markdown_tasks(input_path: Path) -> list[TaskSpec]:
    lines = input_path.read_text(encoding="utf-8").splitlines()
    for index in range(len(lines) - 1):
        headers = _markdown_cells(lines[index])
        separators = _markdown_cells(lines[index + 1])
        if not _is_markdown_table_header(headers, separators):
            continue
        columns = [_canonical_markdown_column(header) for header in headers]
        if "description" not in columns:
            continue
        rows = _markdown_table_rows(lines[index + 2 :], len(columns))
        return [_task_from_markdown_row(row, columns, row_index) for row_index, row in enumerate(rows, start=1)]
    raise ValueError("未找到包含 description/描述 列的 Markdown 任务表格")


def _markdown_table_rows(lines: list[str], width: int) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        cells = _markdown_cells(line)
        if not cells:
            break
        rows.append(_normalize_markdown_row(cells, width))
    return rows


def _task_from_markdown_row(row: list[str], columns: list[str], index: int) -> TaskSpec:
    values = {column: row[position] for position, column in enumerate(columns) if column}
    name = _string_value(values.get("name")) or _default_task_name(index)
    description = _string_value(values.get("description"))
    fields = {key: _string_value(values.get(key)) for key in ELEMENT_ORDER}
    return TaskSpec(name=name, description=description, fields=_remove_empty_fields(fields))


def _markdown_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or "|" not in stripped[1:]:
        return []
    content = stripped.strip("|")
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in content:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "|":
            cells.append(_clean_markdown_cell("".join(current)))
            current = []
            continue
        current.append(character)
    cells.append(_clean_markdown_cell("".join(current)))
    return cells


def _clean_markdown_cell(value: str) -> str:
    return value.replace("<br>", "\n").replace("<br/>", "\n").strip()


def _is_markdown_table_header(headers: list[str], separators: list[str]) -> bool:
    return bool(headers) and len(headers) == len(separators) and all(_is_separator_cell(cell) for cell in separators)


def _is_separator_cell(cell: str) -> bool:
    return bool(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")))


def _canonical_markdown_column(header: str) -> str:
    normalized = re.sub(r"[`*_\\s-]+", "", header.lower())
    for key, aliases in MARKDOWN_COLUMN_ALIASES.items():
        if normalized in {re.sub(r"[`*_\\s-]+", "", alias.lower()) for alias in aliases}:
            return key
    return ""


def _normalize_markdown_row(cells: list[str], width: int) -> list[str]:
    normalized = cells[:width]
    return normalized + [""] * (width - len(normalized))


def _task_from_csv_row(row: dict[str, str | None], index: int) -> TaskSpec:
    name = _string_value(row.get("name")) or _default_task_name(index)
    description = _string_value(row.get("description"))
    fields = {key: _string_value(row.get(key)) for key in ELEMENT_ORDER}
    return TaskSpec(name=name, description=description, fields=_remove_empty_fields(fields))


def _fields_from_mapping(raw_fields: Any) -> dict[str, str]:
    if not isinstance(raw_fields, dict):
        return {}
    fields = {key: _string_value(raw_fields.get(key)) for key in ELEMENT_ORDER}
    return _remove_empty_fields(fields)


def _remove_empty_fields(fields: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in fields.items() if value}


def _invalid_task(index: int, name: str, description: str) -> TaskSpec:
    task_name = name or _default_task_name(index)
    return TaskSpec(name=task_name, description="", fields={}, load_error=description)


def _process_tasks(
    tasks: list[TaskSpec],
    dry_run: bool,
    strict: bool,
    dedupe: bool,
    default_values: dict[str, str],
    verbose: bool,
) -> tuple[list[TaskOutput], list[SkippedTask]]:
    outputs: list[TaskOutput] = []
    skipped_tasks: list[SkippedTask] = []
    used_slugs: set[str] = set()
    seen_task_keys: set[str] = set()
    for index, task in enumerate(tasks, start=1):
        if dedupe and _is_duplicate_task(task, seen_task_keys):
            reason = "重复任务：任务名和描述已出现过"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        try:
            output = _process_one_task(task, index, dry_run, strict, default_values, used_slugs, verbose)
            outputs.append(output)
        except (ValueError, OSError) as error:
            reason = str(error)
            suggestion = _skip_suggestion(reason)
            skipped_tasks.append(SkippedTask(task.name, reason, suggestion))
            print(f"跳过任务 {task.name}：{reason}；建议：{suggestion}", file=sys.stderr)
    return outputs, skipped_tasks


def _is_duplicate_task(task: TaskSpec, seen_task_keys: set[str]) -> bool:
    task_key = _dedupe_key(task)
    if task_key in seen_task_keys:
        return True
    seen_task_keys.add(task_key)
    return False


def _dedupe_key(task: TaskSpec) -> str:
    return _normalize_description(f"{task.name}\n{task.description}")


def _skip_suggestion(reason: str) -> str:
    if "缺少 description" in reason:
        return "为该任务补充 description 字段，说明编码目标和上下文。"
    if "strict 模式缺失要素" in reason:
        return "补齐提示中的 6 要素，或移除 --strict 允许脚本使用默认值。"
    if "JSONL" in reason and "解析失败" in reason:
        return "检查对应行是否是单行合法 JSON 对象，字符串内部双引号需要转义。"
    if "必须是对象" in reason:
        return "把该任务改成包含 name、description、fields 的对象。"
    if "不支持的输入格式" in reason:
        return f"改用 {'、'.join(SUPPORTED_SUFFIXES)} 输入文件。"
    if "重复任务" in reason:
        return "确认是否确实重复；如需全部保留，请移除 --dedupe。"
    return "检查输入文件格式和任务字段后重试。"


def _process_one_task(
    task: TaskSpec,
    index: int,
    dry_run: bool,
    strict: bool,
    default_values: dict[str, str],
    used_slugs: set[str],
    verbose: bool,
) -> TaskOutput:
    if task.load_error:
        raise ValueError(task.load_error)
    if not task.description:
        raise ValueError("缺少 description")
    prepared = _prepare_task(task, strict, default_values)
    content = _format_dry_run(prepared) if dry_run else _format_goal_output(prepared)
    slug = _unique_slug(task.name, index, used_slugs)
    if verbose:
        _print_verbose(prepared, slug)
    return TaskOutput(
        task_name=task.name,
        task_description=task.description,
        content=content,
        file_slug=slug,
        present_keys=prepared.present_keys,
        missing_before_defaults=prepared.missing_before_defaults,
        defaulted_keys=prepared.defaulted_keys,
    )


def _prepare_task(
    task: TaskSpec,
    strict: bool = False,
    default_values: dict[str, str] | None = None,
) -> PreparedTask:
    analysis = analyze_description(task.description)
    values = _extract_labeled_fields(task.description)
    values.update(task.fields)
    _merge_description_present(values, task.description, list(analysis["present"].keys()))
    missing_before_defaults = _missing_keys(values)
    if strict and missing_before_defaults:
        raise ValueError(f"strict 模式缺失要素：{_format_labels(missing_before_defaults)}")
    defaulted_keys = _apply_defaults(values, default_values or INTERACTIVE_DEFAULTS)
    goal = _goal_from_values(values)
    present_keys = [key for key in ELEMENT_ORDER if key not in missing_before_defaults]
    return PreparedTask(task, goal, present_keys, missing_before_defaults, defaulted_keys)


def _merge_description_present(
    values: dict[str, str],
    description: str,
    present_keys: list[str],
) -> None:
    for key in present_keys:
        if key not in values:
            values[key] = _description_fallback(key, description)


def _description_fallback(key: str, description: str) -> str:
    clauses = _description_clauses(description)
    if key == "outcome":
        return clauses[0] if clauses else _normalize_description(description)
    return _matching_clause(clauses, FALLBACK_HINTS.get(key, ())) or _normalize_description(description)


def _description_clauses(description: str) -> list[str]:
    return [clause.strip() for clause in CLAUSE_SPLIT_PATTERN.split(description) if clause.strip()]


def _matching_clause(clauses: list[str], hints: tuple[str, ...]) -> str:
    for clause in clauses:
        if any(hint.lower() in clause.lower() for hint in hints):
            return _strip_field_label(clause)
    return ""


def _strip_field_label(clause: str) -> str:
    if "：" in clause:
        return clause.split("：", 1)[1].strip()
    if ":" in clause:
        return clause.split(":", 1)[1].strip()
    return clause


def _normalize_description(description: str) -> str:
    return " ".join(description.strip().split())


def _missing_keys(values: dict[str, str]) -> list[str]:
    return [key for key in ELEMENT_ORDER if not values.get(key)]


def _apply_defaults(values: dict[str, str], default_values: dict[str, str]) -> list[str]:
    defaulted_keys: list[str] = []
    for key in ELEMENT_ORDER:
        if not values.get(key):
            values[key] = default_values[key]
            defaulted_keys.append(key)
    return defaulted_keys


def _goal_from_values(values: dict[str, str]) -> _GoalFields:
    return _GoalFields(
        outcome=values["outcome"],
        verification=values["verification"],
        constraints=values["constraints"],
        boundaries=values["boundaries"],
        iteration=values["iteration"],
        blocked=values["blocked"],
    )


def _format_dry_run(prepared: PreparedTask) -> str:
    return "\n".join(
        [
            f"任务：{prepared.task.name}",
            f"已具备要素：{_format_labels(prepared.present_keys)}",
            f"缺失要素：{_format_labels(prepared.missing_before_defaults)}",
            f"默认填充：{_format_labels(prepared.defaulted_keys)}",
        ]
    )


def _format_goal_output(prepared: PreparedTask) -> str:
    default_notice = _format_default_notice(prepared.defaulted_keys)
    parts = [f"任务：{prepared.task.name}"]
    if default_notice:
        parts.append(default_notice)
    parts.append(render_goal_text(prepared.goal))
    return "\n".join(parts)


def _format_default_notice(defaulted_keys: list[str]) -> str:
    if not defaulted_keys:
        return ""
    return f"默认填充：{_format_labels(defaulted_keys)}"


def _format_labels(keys: list[str]) -> str:
    if not keys:
        return "无"
    return "、".join(ELEMENT_LABELS[key] for key in keys)


def _write_outputs(
    outputs: list[TaskOutput],
    output_dir: str | None,
    output_file: str | None,
    summary_only: bool = False,
) -> None:
    if output_dir:
        _write_output_dir(outputs, Path(output_dir))
        return
    if output_file:
        _write_output_file(outputs, Path(output_file))
        return
    if summary_only:
        return
    print(TASK_SEPARATOR.join(output.content for output in outputs))


def _write_output_dir(outputs: list[TaskOutput], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for output in outputs:
        file_path = output_dir / f"{output.file_slug}{GOAL_FILE_SUFFIX}"
        file_path.write_text(f"{output.content}\n", encoding="utf-8")


def _write_output_file(outputs: list[TaskOutput], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(f"{TASK_SEPARATOR.join(output.content for output in outputs)}\n", encoding="utf-8")


def _write_report_json(
    outputs: list[TaskOutput],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
    output_dir: str | None,
    output_file: str | None,
    include_profile: bool = False,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "tasks": [_task_report(output, output_dir, output_file, include_profile) for output in outputs],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_output_index_markdown(
    outputs: list[TaskOutput],
    stats: BatchStats,
    index_path: Path,
    output_dir: str | None,
    output_file: str | None,
) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Batch Goal Output Index",
        "",
        f"- 产物数量：{stats.success_count}",
        f"- 跳过任务：{stats.skipped_count}",
        f"- 总耗时：{stats.elapsed_seconds:.2f} 秒",
        "",
    ]
    if not outputs:
        lines.append("无输出产物。")
    else:
        lines.extend(
            [
                "| 任务 | 输出路径 | 缺失要素 | 默认填充 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for output in outputs:
            output_path = _report_output_path(output, output_dir, output_file) or "-"
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(output.task_name),
                        _markdown_cell(output_path),
                        _markdown_cell(_format_labels(output.missing_before_defaults)),
                        _markdown_cell(_format_labels(output.defaulted_keys)),
                    ]
                )
                + " |"
            )
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_report_markdown(
    outputs: list[TaskOutput],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
    output_dir: str | None,
    output_file: str | None,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Batch Goal Report",
        "",
        f"- 成功任务：{stats.success_count}",
        f"- 跳过任务：{stats.skipped_count}",
        f"- 总耗时：{stats.elapsed_seconds:.2f} 秒",
        "",
        "## 成功任务",
        "",
    ]
    lines.extend(_markdown_output_table(outputs, output_dir, output_file))
    lines.extend(["", "## 跳过任务", ""])
    lines.extend(_markdown_skipped_table(skipped_tasks))
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_missing_report_markdown(
    outputs: list[TaskOutput],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    missing_outputs = [output for output in outputs if output.missing_before_defaults]
    lines = [
        "# Batch Missing Elements Report",
        "",
        f"- 成功任务：{stats.success_count}",
        f"- 需补全任务：{len(missing_outputs)}",
        f"- 跳过任务：{stats.skipped_count}",
        f"- 总耗时：{stats.elapsed_seconds:.2f} 秒",
        "",
        "## 需补全任务",
        "",
    ]
    lines.extend(_markdown_missing_table(missing_outputs))
    lines.extend(["", "## 已完整任务", ""])
    lines.extend(_markdown_complete_task_list(outputs))
    lines.extend(["", "## 跳过任务", ""])
    lines.extend(_markdown_skipped_table(skipped_tasks))
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_field_status_csv(outputs: list[TaskOutput], skipped_tasks: list[SkippedTask], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(FIELD_STATUS_COLUMNS))
        writer.writeheader()
        for output in outputs:
            writer.writerow(_field_status_output_row(output))
        for skipped in skipped_tasks:
            writer.writerow(_field_status_skipped_row(skipped))


def _field_status_output_row(output: TaskOutput) -> dict[str, str]:
    score = score_description(output.task_description)
    row = {
        "task_name": output.task_name,
        "status": "processed",
        "readiness_score": str(score.get("readiness_score", "")),
        "readiness_level": str(score.get("readiness_level", "unknown")),
        "risk_level": str(score.get("risk_level", "unknown")),
        "risk_score": str(score.get("risk_score", "")),
        "missing_before_defaults": _csv_join(output.missing_before_defaults),
        "defaulted": _csv_join(output.defaulted_keys),
        "skip_reason": "",
        "skip_suggestion": "",
    }
    for key in ELEMENT_ORDER:
        row[f"{key}_status"] = _field_status_for_key(key, output)
    return row


def _field_status_for_key(key: str, output: TaskOutput) -> str:
    if key in output.present_keys:
        return "present"
    if key in output.defaulted_keys:
        return "defaulted"
    if key in output.missing_before_defaults:
        return "missing"
    return "unknown"


def _field_status_skipped_row(skipped: SkippedTask) -> dict[str, str]:
    row = {
        "task_name": skipped.task_name,
        "status": "skipped",
        "readiness_score": "",
        "readiness_level": "",
        "risk_level": "",
        "risk_score": "",
        "missing_before_defaults": "",
        "defaulted": "",
        "skip_reason": skipped.reason,
        "skip_suggestion": skipped.suggestion,
    }
    for key in ELEMENT_ORDER:
        row[f"{key}_status"] = "skipped"
    return row


def _csv_join(values: list[str]) -> str:
    return ",".join(values)


def _markdown_missing_table(outputs: list[TaskOutput]) -> list[str]:
    if not outputs:
        return ["无需要补全的成功任务。"]
    lines = [
        "| 任务 | 风险 | 缺失要素 | 默认填充 | 补全建议 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for output in outputs:
        profile = build_task_profile(output.task_description)
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(output.task_name),
                    _markdown_cell(_profile_risk_cell(profile)),
                    _markdown_cell(_format_labels(output.missing_before_defaults)),
                    _markdown_cell(_format_labels(output.defaulted_keys)),
                    _markdown_cell(_missing_fill_suggestions(output.missing_before_defaults, profile)),
                ]
            )
            + " |"
        )
    return lines


def _profile_risk_cell(profile: dict[str, object]) -> str:
    return f"{profile.get('risk_level', 'unknown')}({profile.get('risk_score', '')})"


def _missing_fill_suggestions(missing_keys: list[str], profile: dict[str, object]) -> str:
    recommended_fields = profile.get("recommended_fields", {})
    recommendations = recommended_fields if isinstance(recommended_fields, dict) else {}
    suggestions: list[str] = []
    for key in missing_keys:
        fallback = QUESTION_EXAMPLES[key]
        suggestions.append(f"{ELEMENT_LABELS[key]}：{recommendations.get(key, fallback)}")
    return "\n".join(suggestions)


def _markdown_complete_task_list(outputs: list[TaskOutput]) -> list[str]:
    complete_outputs = [output for output in outputs if not output.missing_before_defaults]
    if not complete_outputs:
        return ["无已完整任务。"]
    return [f"- {_markdown_cell(output.task_name)}" for output in complete_outputs]


def _markdown_output_table(
    outputs: list[TaskOutput],
    output_dir: str | None,
    output_file: str | None,
) -> list[str]:
    if not outputs:
        return ["无成功任务。"]
    lines = [
        "| 任务 | 输出路径 | 已具备要素 | 缺失要素 | 默认填充 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for output in outputs:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(output.task_name),
                    _markdown_cell(_report_output_path(output, output_dir, output_file) or "-"),
                    _markdown_cell(_format_labels(output.present_keys)),
                    _markdown_cell(_format_labels(output.missing_before_defaults)),
                    _markdown_cell(_format_labels(output.defaulted_keys)),
                ]
            )
            + " |"
        )
    return lines


def _markdown_skipped_table(skipped_tasks: list[SkippedTask]) -> list[str]:
    if not skipped_tasks:
        return ["无跳过任务。"]
    lines = [
        "| 任务 | 原因 | 建议 |",
        "| --- | --- | --- |",
    ]
    for skipped in skipped_tasks:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(skipped.task_name),
                    _markdown_cell(skipped.reason),
                    _markdown_cell(skipped.suggestion),
                ]
            )
            + " |"
        )
    return lines


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>").strip()


def _write_task_list_report(
    tasks: list[TaskSpec],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
    include_profile: bool = False,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "tasks": [_task_list_report(task, include_profile) for task in tasks],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_profile_summary_report(
    profiled_tasks: list[TaskProfileSummary],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "tasks": [_task_profile_summary_report(profiled_task) for profiled_task in profiled_tasks],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_score_summary_report(
    scored_tasks: list[TaskScoreSummary],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
    score_gate_threshold: int | None = None,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "score_gate": _score_gate_report(scored_tasks, score_gate_threshold),
        "tasks": [_task_score_summary_report(scored_task) for scored_task in scored_tasks],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _score_gate_report(scored_tasks: list[TaskScoreSummary], threshold: int | None) -> dict[str, object]:
    if threshold is None:
        return {"enabled": False}
    failures = _score_gate_failures(scored_tasks, threshold)
    return {
        "enabled": True,
        "threshold": threshold,
        "failed_count": len(failures),
        "failed_tasks": [
            {
                "name": failure.task.name,
                "readiness_score": failure.score.get("readiness_score", 0),
                "readiness_level": failure.score.get("readiness_level", "unknown"),
                "missing": failure.score.get("missing", []),
            }
            for failure in failures
        ],
    }


def _write_fields_export_report(
    exports: list[FieldsExport],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "exports": [
            {
                "name": export.task_name,
                "output_path": export.output_path,
                "review_required": export.review_required,
            }
            for export in exports
        ],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_draft_jsonl_report(
    drafts: list[GoalJsonDraft],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "drafts": [_draft_jsonl_report_item(draft) for draft in drafts],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_redaction_summary_report(
    redaction_summaries: list[TaskRedactionSummary],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "tasks": [
            {
                "name": summary.task.name,
                "redaction": summary.check,
            }
            for summary in redaction_summaries
        ],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _draft_jsonl_report_item(draft: GoalJsonDraft) -> dict[str, object]:
    payload = draft.draft
    return {
        "name": draft.task_name,
        "file_slug": draft.file_slug,
        "ready_to_generate": payload.get("ready_to_generate", False),
        "review_required": payload.get("review_required", []),
        "readiness_score": payload.get("readiness_score", 0),
        "readiness_level": payload.get("readiness_level", "unknown"),
        "risk_level": payload.get("risk_level", "unknown"),
        "risk_score": payload.get("risk_score", 0),
    }


def _write_question_pack_report(
    question_packs: list[TaskQuestionPack],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "tasks": [
            {
                "name": question_pack.task.name,
                "description": question_pack.task.description,
                "question_pack": question_pack.pack,
            }
            for question_pack in question_packs
        ],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_score_report_markdown(
    scored_tasks: list[TaskScoreSummary],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
    score_gate_threshold: int | None = None,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Batch Readiness Score Report",
        "",
        f"- 成功任务：{stats.success_count}",
        f"- 跳过任务：{stats.skipped_count}",
        f"- 总耗时：{stats.elapsed_seconds:.2f} 秒",
        "",
    ]
    if score_gate_threshold is not None:
        failures = _score_gate_failures(scored_tasks, score_gate_threshold)
        lines.extend(
            [
                "## 阈值门禁",
                "",
                f"- 最低分：{score_gate_threshold}",
                f"- 低于阈值：{len(failures)}",
                "",
            ]
        )
    lines.extend(["## 评分摘要", ""])
    lines.extend(_markdown_score_table(scored_tasks))
    lines.extend(["", "## 跳过任务", ""])
    lines.extend(_markdown_skipped_table(skipped_tasks))
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_questions_markdown(
    question_packs: list[TaskQuestionPack],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    questions_path: Path,
) -> None:
    questions_path.parent.mkdir(parents=True, exist_ok=True)
    needs_questions = [question_pack for question_pack in question_packs if _question_pack_missing(question_pack.pack)]
    lines = [
        "# Batch Goal Question Pack",
        "",
        f"- 可评审任务：{stats.success_count}",
        f"- 需要补信息：{len(needs_questions)}",
        f"- 跳过任务：{stats.skipped_count}",
        f"- 总耗时：{stats.elapsed_seconds:.2f} 秒",
        "",
    ]
    if not question_packs:
        lines.append("无可追问任务。")
    for index, question_pack in enumerate(question_packs, start=1):
        lines.extend(_question_pack_markdown_section(index, question_pack))
        lines.append("")
    lines.extend(["## 跳过任务", ""])
    lines.extend(_markdown_skipped_table(skipped_tasks))
    questions_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_risk_report_markdown(
    profiled_tasks: list[TaskProfileSummary],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_tasks = sorted(profiled_tasks, key=lambda item: _profile_int(item.profile, "risk_score"), reverse=True)
    high_risk_count = sum(1 for task in profiled_tasks if task.profile.get("risk_level") == "high")
    lines = [
        "# Batch Goal Risk Report",
        "",
        f"- 可评审任务：{stats.success_count}",
        f"- 高风险任务：{high_risk_count}",
        f"- 跳过任务：{stats.skipped_count}",
        f"- 总耗时：{stats.elapsed_seconds:.2f} 秒",
        "",
        "## 风险明细",
        "",
    ]
    lines.extend(_risk_report_table(sorted_tasks))
    lines.extend(["", "## 跳过任务", ""])
    lines.extend(_markdown_skipped_table(skipped_tasks))
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_redaction_report_markdown(
    redaction_summaries: list[TaskRedactionSummary],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    needs_redaction = [summary for summary in redaction_summaries if _redaction_finding_count(summary.check)]
    high_risk_count = sum(1 for summary in redaction_summaries if summary.check.get("risk_level") == "high")
    lines = [
        "# Batch Goal Redaction Report",
        "",
        f"- 可审计任务：{stats.success_count}",
        f"- 需脱敏任务：{len(needs_redaction)}",
        f"- 高风险任务：{high_risk_count}",
        f"- 跳过任务：{stats.skipped_count}",
        f"- 总耗时：{stats.elapsed_seconds:.2f} 秒",
        "",
        "## 敏感信息明细",
        "",
    ]
    lines.extend(_redaction_report_table(redaction_summaries))
    lines.extend(["", "## 跳过任务", ""])
    lines.extend(_markdown_skipped_table(skipped_tasks))
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _redaction_report_table(redaction_summaries: list[TaskRedactionSummary]) -> list[str]:
    if not redaction_summaries:
        return ["无可审计任务。"]
    lines = [
        "| 任务 | 风险 | 发现类型 | 数量 | 脱敏预览 | 建议 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for summary in redaction_summaries:
        check = summary.check
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(summary.task.name),
                    _markdown_cell(str(check.get("risk_level", "none"))),
                    _markdown_cell(_redaction_types_cell(check)),
                    _markdown_cell(str(check.get("finding_count", 0))),
                    _markdown_cell(str(check.get("redacted_preview", ""))),
                    _markdown_cell(str(check.get("recommended_action", ""))),
                ]
            )
            + " |"
        )
    return lines


def _redaction_finding_count(check: dict[str, object]) -> int:
    value = check.get("finding_count", 0)
    return value if isinstance(value, int) else 0


def _redaction_types_cell(check: dict[str, object]) -> str:
    types = check.get("finding_types", [])
    type_values = [str(item) for item in types] if isinstance(types, list) else []
    return ",".join(type_values) if type_values else "无"


def _risk_report_table(profiled_tasks: list[TaskProfileSummary]) -> list[str]:
    if not profiled_tasks:
        return ["无可评审任务。"]
    lines = [
        "| 任务 | 类型 | 风险 | 复杂度 | 缺失要素 | 风险因素 | 缓解动作 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for profiled_task in profiled_tasks:
        profile = profiled_task.profile
        task_type = profile.get("task_type", {})
        complexity = profile.get("complexity", {})
        task_label = task_type.get("label", "未知") if isinstance(task_type, dict) else "未知"
        complexity_level = complexity.get("level", "unknown") if isinstance(complexity, dict) else "unknown"
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(profiled_task.task.name),
                    _markdown_cell(str(task_label)),
                    _markdown_cell(_profile_risk_cell(profile)),
                    _markdown_cell(str(complexity_level)),
                    _markdown_cell(_profile_missing_labels(profile)),
                    _markdown_cell(_profile_risk_factors(profile)),
                    _markdown_cell(_profile_mitigation_action(profile)),
                ]
            )
            + " |"
        )
    return lines


def _profile_int(profile: dict[str, object], key: str) -> int:
    value = profile.get(key, 0)
    return value if isinstance(value, int) else 0


def _profile_missing_keys(profile: dict[str, object]) -> list[str]:
    missing = profile.get("missing", [])
    return [key for key in missing if isinstance(key, str) and key in ELEMENT_ORDER] if isinstance(missing, list) else []


def _profile_missing_labels(profile: dict[str, object]) -> str:
    return _format_labels(_profile_missing_keys(profile))


def _profile_risk_factors(profile: dict[str, object]) -> str:
    factors = profile.get("risk_factors", [])
    factor_values = [str(factor) for factor in factors] if isinstance(factors, list) else []
    return "\n".join(factor_values) if factor_values else "未发现明显高风险信号"


def _profile_mitigation_action(profile: dict[str, object]) -> str:
    missing_keys = _profile_missing_keys(profile)
    if missing_keys:
        return f"优先补齐：{_format_labels(missing_keys)}"
    if profile.get("risk_level") == "high":
        return "先复核迁移、兼容、回滚和验证面后再生成 /goal"
    return "可进入生成前复核边界、验证命令和受阻条件"


def _question_pack_markdown_section(index: int, question_pack: TaskQuestionPack) -> list[str]:
    pack = question_pack.pack
    lines = [
        f"## {index}. {_markdown_cell(question_pack.task.name)}",
        "",
        f"- 可执行度：{pack.get('readiness_score', '')}/100（{pack.get('readiness_level', 'unknown')}）",
        f"- 风险：{pack.get('risk_level', 'unknown')}（{pack.get('risk_score', '')}）",
        f"- 缺失要素：{_format_labels(_question_pack_missing(pack))}",
        "",
        "### 可直接发送的追问",
        "",
    ]
    lines.extend(_markdown_quote_block(str(pack.get("next_prompt", ""))))
    lines.extend(["", "### 结构化问题", ""])
    lines.extend(_question_pack_markdown_table(pack))
    return lines


def _question_pack_missing(pack: dict[str, object]) -> list[str]:
    missing = pack.get("missing", [])
    return [key for key in missing if isinstance(key, str) and key in ELEMENT_ORDER] if isinstance(missing, list) else []


def _markdown_quote_block(value: str) -> list[str]:
    stripped = value.strip()
    if not stripped:
        return ["> 无追问内容。"]
    return [f"> {line}" if line else ">" for line in stripped.splitlines()]


def _question_pack_markdown_table(pack: dict[str, object]) -> list[str]:
    questions = pack.get("questions", [])
    if not isinstance(questions, list) or not questions:
        return ["无缺失问题；仍建议人工复核边界、验证命令和受阻条件。"]
    lines = [
        "| 优先级 | 要素 | 推荐补法 | 示例 |",
        "| --- | --- | --- | --- |",
    ]
    for question in questions:
        if not isinstance(question, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(str(question.get("priority", ""))),
                    _markdown_cell(str(question.get("label", ""))),
                    _markdown_cell(str(question.get("recommended_fill", ""))),
                    _markdown_cell(str(question.get("example", ""))),
                ]
            )
            + " |"
        )
    return lines


def _write_review_board_markdown(
    scored_tasks: list[TaskScoreSummary],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    board_path: Path,
) -> None:
    board_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Batch Goal Review Board",
        "",
        f"- 可评审任务：{stats.success_count}",
        f"- 跳过任务：{stats.skipped_count}",
        f"- 总耗时：{stats.elapsed_seconds:.2f} 秒",
        "",
    ]
    for level in ("high_risk", "incomplete", "needs_review", "ready"):
        group = [scored_task for scored_task in scored_tasks if scored_task.score.get("readiness_level") == level]
        lines.extend(_review_board_group(level, group))
        lines.append("")
    lines.extend(["## 跳过任务", ""])
    lines.extend(_markdown_skipped_table(skipped_tasks))
    board_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _review_board_group(level: str, scored_tasks: list[TaskScoreSummary]) -> list[str]:
    lines = [f"## {_review_level_title(level)}", ""]
    if not scored_tasks:
        lines.append("无任务。")
        return lines
    lines.extend(
        [
            "| 任务 | 分数 | 风险 | 缺失要素 | 下一步 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for scored_task in sorted(scored_tasks, key=lambda item: _score_value(item.score)):
        score = scored_task.score
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(scored_task.task.name),
                    _markdown_cell(str(score.get("readiness_score", ""))),
                    _markdown_cell(f"{score.get('risk_level', 'unknown')}({score.get('risk_score', '')})"),
                    _markdown_cell(_score_missing_labels(score)),
                    _markdown_cell(str(score.get("next_action", ""))),
                ]
            )
            + " |"
        )
    return lines


def _review_level_title(level: str) -> str:
    titles = {
        "high_risk": "High Risk（高风险，优先补信息）",
        "incomplete": "Incomplete（信息不足）",
        "needs_review": "Needs Review（可复核后生成）",
        "ready": "Ready（基本可生成）",
    }
    return titles.get(level, level)


def _markdown_score_table(scored_tasks: list[TaskScoreSummary]) -> list[str]:
    if not scored_tasks:
        return ["无可评分任务。"]
    lines = [
        "| 任务 | 分数 | 等级 | 风险 | 缺失要素 | 下一步建议 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for scored_task in scored_tasks:
        score = scored_task.score
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(scored_task.task.name),
                    _markdown_cell(str(score.get("readiness_score", ""))),
                    _markdown_cell(str(score.get("readiness_level", "unknown"))),
                    _markdown_cell(f"{score.get('risk_level', 'unknown')}({score.get('risk_score', '')})"),
                    _markdown_cell(_score_missing_labels(score)),
                    _markdown_cell(str(score.get("next_action", ""))),
                ]
            )
            + " |"
        )
    return lines


def _score_missing_labels(score: dict[str, object]) -> str:
    missing = score.get("missing", [])
    missing_keys = [key for key in missing if isinstance(key, str) and key in ELEMENT_ORDER] if isinstance(missing, list) else []
    return _format_labels(missing_keys)


def _task_list_report(task: TaskSpec, include_profile: bool = False) -> dict[str, object]:
    report: dict[str, object] = {"name": task.name, "description": task.description}
    if include_profile:
        report["profile"] = build_task_profile(task.description)
    return report


def _task_profile_summary_report(profiled_task: TaskProfileSummary) -> dict[str, object]:
    return {
        "name": profiled_task.task.name,
        "description": profiled_task.task.description,
        "profile": profiled_task.profile,
    }


def _task_score_summary_report(scored_task: TaskScoreSummary) -> dict[str, object]:
    return {
        "name": scored_task.task.name,
        "description": scored_task.task.description,
        "score": scored_task.score,
    }


def _task_report(
    output: TaskOutput,
    output_dir: str | None,
    output_file: str | None,
    include_profile: bool = False,
) -> dict[str, object]:
    report: dict[str, object] = {
        "name": output.task_name,
        "file_slug": output.file_slug,
        "output_path": _report_output_path(output, output_dir, output_file),
        "present": output.present_keys,
        "missing_before_defaults": output.missing_before_defaults,
        "defaulted": output.defaulted_keys,
    }
    if include_profile:
        report["profile"] = build_task_profile(output.task_description)
    return report


def _report_output_path(output: TaskOutput, output_dir: str | None, output_file: str | None) -> str:
    if output_dir:
        return str(Path(output_dir) / f"{output.file_slug}{GOAL_FILE_SUFFIX}")
    if output_file:
        return output_file
    return ""


def _skipped_report(skipped: SkippedTask) -> dict[str, str]:
    return {"name": skipped.task_name, "reason": skipped.reason, "suggestion": skipped.suggestion}


def _unique_slug(name: str, index: int, used_slugs: set[str]) -> str:
    base_slug = _slugify(name) or f"{DEFAULT_NAME_PREFIX}-{index}"
    slug = base_slug
    suffix_index = 2
    while slug in used_slugs:
        suffix = f"-{suffix_index}"
        slug = f"{base_slug[:SLUG_MAX_LENGTH - len(suffix)]}{suffix}"
        suffix_index += 1
    used_slugs.add(slug)
    return slug


def _slugify(value: str) -> str:
    normalized = "".join(_slug_char(character) for character in value.lower())
    collapsed = re.sub(r"-+", "-", normalized).strip("-")
    return collapsed[:SLUG_MAX_LENGTH].strip("-")


def _slug_char(character: str) -> str:
    if character.isalnum() or character == "-":
        return character
    return "-"


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _default_task_name(index: int) -> str:
    return f"{DEFAULT_NAME_PREFIX}-{index}"


def _print_verbose(prepared: PreparedTask, slug: str) -> None:
    defaulted = _format_labels(prepared.defaulted_keys)
    print(f"处理任务：{prepared.task.name}，文件名：{slug}，默认填充：{defaulted}")


def _format_summary(stats: BatchStats) -> str:
    return SUMMARY_TEMPLATE.format(
        success_count=stats.success_count,
        skipped_count=stats.skipped_count,
        elapsed_seconds=stats.elapsed_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
