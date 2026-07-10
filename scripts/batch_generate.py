# 负责从 JSON/CSV 批量读取编码任务并生成 Codex CLI /goal 指令。
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
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
    check_redaction,
    inspect_path_context,
    lint_goal_text,
    lint_fields_json_data,
    render_goal_text,
)

SUPPORTED_SUFFIXES: tuple[str, ...] = (".json", ".csv")
SLUG_MAX_LENGTH = 50
DEFAULT_NAME_PREFIX = "task"
GOAL_FILE_SUFFIX = ".txt"
TASK_SEPARATOR = "\n\n"
SUMMARY_TEMPLATE = "处理完成：成功 {success_count} 个，跳过 {skipped_count} 个，总耗时 {elapsed_seconds:.2f} 秒。"
DEFAULTS_JSON_ENV = "GOAL_GENERATOR_DEFAULTS_JSON"
CLAUSE_SPLIT_PATTERN = re.compile(r"[，。；;\n]+")
DEPENDENCY_SPLIT_PATTERN = re.compile(r"[,;，；、\n]+")
BATCH_INSPECT_PATH_PATTERN = re.compile(r"(?:^|\s|`)([\w./-]+/[\w./-]*|[\w.-]+\.[A-Za-z0-9]+)")
FALLBACK_HINTS: dict[str, tuple[str, ...]] = {
    "verification": ("验证", "运行", "执行", "确认", "检查", "通过", "跑测试"),
    "constraints": ("不改", "不修改", "不改变", "不引入", "禁止", "不得", "保持", "兼容"),
    "boundaries": ("边界", "范围", "仅", "只", "目录", "排除", "src/", "tests/"),
    "iteration": ("迭代", "每个", "每次", "逐个", "commit", "提交", "预期"),
    "blocked": ("受阻", "阻塞", "停下", "问人", "问我", "跳过", "无法", "缺少"),
}
SUPPLEMENT_TEXT_FIELDS: tuple[str, ...] = ("supplement", "answer", "response", "text", "description")



@dataclass(frozen=True)
class TaskSpec:
    name: str
    description: str
    fields: dict[str, str]
    inspect_path: str = ""
    depends_on: list[str] = field(default_factory=list)
    load_error: str = ""


@dataclass(frozen=True)
class SupplementEntry:
    task_name: str
    text: str
    fields: dict[str, str]


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
        tasks = _load_tasks_from_input(_input_value_from_args(args))
        tasks = _filter_tasks(tasks, args.filter)
        tasks = _sort_tasks(tasks, args.sort_by)
        tasks = _limit_tasks(tasks, args.limit)
        if args.dependency_order:
            tasks = _apply_dependency_order(tasks, args.dedupe)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"读取输入失败：{error}", file=sys.stderr)
        return 1
    if args.lint_output and (
        args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
        or args.dry_run
        or args.check
    ):
        print("--lint-output 只能用于真实批量生成模式，请勿与分析、检查或清单模式同用。", file=sys.stderr)
        return 1
    if args.merge_supplements:
        try:
            return _run_merge_supplements_mode(tasks, args, start_time)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"读取输入失败：{error}", file=sys.stderr)
            return 1
    if args.questions:
        return _run_batch_questions_mode(tasks, args, start_time)
    if args.profile_tasks:
        return _run_batch_profile_mode(tasks, args, start_time)
    if args.redaction_check:
        return _run_batch_redaction_check_mode(tasks, args, start_time)
    if args.lint_fields:
        return _run_lint_fields_mode(tasks, args, start_time)
    if args.inspect_paths:
        return _run_inspect_paths_mode(tasks, args, start_time)
    if args.enrich_from_paths:
        return _run_enrich_from_paths_mode(tasks, args, start_time)
    if args.plan_dependencies:
        return _run_dependency_plan_mode(tasks, args, start_time)
    if args.list_tasks:
        return _run_list_tasks_mode(tasks, args)
    try:
        default_values = _load_default_values(args.defaults_json)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"读取输入失败：{error}", file=sys.stderr)
        return 1
    dry_run = args.dry_run or args.check
    strict = args.strict or args.check
    summary_only = args.summary_only or args.check
    fail_on_skipped = args.fail_on_skipped or args.check
    outputs, skipped_tasks = _process_tasks(tasks, dry_run, strict, args.dedupe, default_values, args.verbose)
    output_lint_report = None
    if args.lint_output:
        if dry_run:
            print("--lint-output 需要真实生成 /goal，请勿与 --dry-run 或 --check 同用。", file=sys.stderr)
            return 1
        output_lint_report = _build_output_lint_report(outputs)
        if not output_lint_report["passed"]:
            elapsed_seconds = time.perf_counter() - start_time
            failed_count = int(output_lint_report["failed_count"])
            stats = BatchStats(len(outputs) - failed_count, len(skipped_tasks) + failed_count, elapsed_seconds)
            if args.report_json:
                _write_report_json(
                    outputs,
                    skipped_tasks,
                    stats,
                    Path(args.report_json),
                    args.output_dir,
                    args.output_file,
                    output_lint_report,
                )
            if not summary_only:
                print(_format_output_lint_report(output_lint_report))
            print(_format_lint_output_summary(output_lint_report))
            print(_format_summary(stats))
            return 1
    _write_outputs(outputs, args.output_dir, args.output_file, summary_only)
    elapsed_seconds = time.perf_counter() - start_time
    stats = BatchStats(len(outputs), len(skipped_tasks), elapsed_seconds)
    if args.report_json:
        _write_report_json(
            outputs,
            skipped_tasks,
            stats,
            Path(args.report_json),
            args.output_dir,
            args.output_file,
            output_lint_report,
        )
    if output_lint_report:
        print(_format_lint_output_summary(output_lint_report))
    print(_format_summary(stats))
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量生成 Codex CLI /goal 指令。")
    parser.add_argument("input_path", nargs="?", help="输入文件路径，可替代 --input。")
    parser.add_argument("--input", help="输入文件路径，支持 .json 或 .csv。")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--output-dir", help="输出目录，每个任务生成一个 .txt 文件。")
    output_group.add_argument("--output-file", help="输出到单个文件。")
    parser.add_argument("--defaults-json", help="JSON 默认值文件，用于覆盖缺失 6 要素的默认填充。")
    parser.add_argument("--report-json", help="把批量处理结果、缺失要素和跳过原因写入 JSON 报告。")
    parser.add_argument("--filter", help="按正则筛选任务名或描述，只处理匹配的任务。")
    parser.add_argument("--sort-by", choices=("input", "name"), default="input", help="批量任务输出顺序，默认保持输入顺序。")
    parser.add_argument("--limit", type=int, help="只处理前 N 个任务，适合大清单试跑。")
    parser.add_argument("--list-tasks", action="store_true", help="只预览将要处理的任务名称，不生成 /goal。")
    parser.add_argument("--plan-dependencies", action="store_true", help="根据 depends_on/dependencies 字段输出批量任务依赖执行计划。")
    parser.add_argument("--dependency-order", action="store_true", help="按 depends_on/dependencies 拓扑顺序处理批量任务。")
    parser.add_argument("--dedupe", action="store_true", help="按任务名和描述跳过重复任务。")
    parser.add_argument("--fail-on-skipped", action="store_true", help="有跳过任务时以退出码 1 结束，适合 CI 门禁。")
    parser.add_argument("--summary-only", action="store_true", help="抑制任务正文 stdout，仅输出最终摘要。")
    parser.add_argument("--redaction-check", action="store_true", help="批量检查任务名称、描述和字段值中的 token、邮箱、URL 等敏感信息。")
    parser.add_argument("--profile-tasks", action="store_true", help="批量识别任务类型、复杂度、风险和 6 要素缺口。")
    parser.add_argument("--questions", action="store_true", help="按任务生成可直接发送的批量缺失要素追问文案。")
    parser.add_argument("--merge-supplements", help="读取按任务名组织的补充回答 JSON/CSV，合并回批量任务 fields 并输出新的任务 JSON。")
    parser.add_argument("--dry-run", action="store_true", help="只分析要素完整度，不生成指令。")
    parser.add_argument("--lint-output", action="store_true", help="真实生成 /goal 后，在写出交付物前检查每个最终文本的结构和语义质量。")
    parser.add_argument("--check", action="store_true", help="校验输入任务文件，等价于 --dry-run --strict --summary-only --fail-on-skipped。")
    parser.add_argument("--lint-fields", action="store_true", help="批量检查任务 6 要素字段的语义质量，不生成 /goal。")
    parser.add_argument("--inspect-paths", action="store_true", help="批量扫描任务 path/inspect_path/target_path 指向的本地路径并输出上下文建议。")
    parser.add_argument("--enrich-from-paths", action="store_true", help="用任务路径扫描的 suggested_fields 回填缺失或启发式 6 要素并输出增强后的任务 JSON。")
    parser.add_argument("--strict", action="store_true", help="缺失 6 要素时跳过任务，不使用默认填充。")
    parser.add_argument("--verbose", action="store_true", help="打印详细处理日志。")
    return parser



def _input_value_from_args(args: argparse.Namespace) -> str:
    input_value = args.input or args.input_path
    if not input_value:
        raise ValueError("必须提供 --input 或位置输入文件路径")
    return input_value


def _load_tasks_from_input(input_value: str) -> list[TaskSpec]:
    if input_value == "-":
        raise ValueError("标准输入任务流已移除，请改用 .json 或 .csv 文件")
    return _load_tasks(Path(input_value))


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


def _apply_dependency_order(tasks: list[TaskSpec], dedupe: bool) -> list[TaskSpec]:
    ordered_tasks, _skipped_tasks = _dedupe_preview_tasks(tasks, dedupe)
    plan = _build_dependency_plan(ordered_tasks)
    if not plan.get("valid", False):
        raise ValueError(f"--dependency-order 依赖无效：{_dependency_order_error(plan)}")
    task_by_name = {task.name: task for task in _unique_tasks_by_name(ordered_tasks)}
    ordered_names = _dependency_ordered_names(plan)
    return [task_by_name[name] for name in ordered_names if name in task_by_name]


def _dependency_ordered_names(plan: dict[str, object]) -> list[str]:
    ordered_names: list[str] = []
    waves = plan.get("waves", [])
    if not isinstance(waves, list):
        return ordered_names
    for wave in waves:
        if not isinstance(wave, dict):
            continue
        tasks = wave.get("tasks", [])
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if isinstance(task, dict) and task.get("name"):
                ordered_names.append(str(task["name"]))
    return ordered_names


def _dependency_order_error(plan: dict[str, object]) -> str:
    issues = plan.get("issues", [])
    if not isinstance(issues, list) or not issues:
        return "未知依赖问题"
    messages: list[str] = []
    for issue in issues[:5]:
        if isinstance(issue, dict):
            messages.append(f"{issue.get('name', '未知任务')}：{issue.get('reason', '')}")
    if len(issues) > 5:
        messages.append(f"其余 {len(issues) - 5} 个问题可用 --plan-dependencies 查看")
    return "；".join(messages) if messages else "未知依赖问题"


def _run_list_tasks_mode(tasks: list[TaskSpec], args: argparse.Namespace) -> int:
    start_time = time.perf_counter()
    listed_tasks, skipped_tasks = _dedupe_preview_tasks(tasks, args.dedupe)
    summary_only = args.summary_only or args.check
    if not summary_only:
        print("\n".join(f"{index}. {task.name}" for index, task in enumerate(listed_tasks, start=1)))
    stats = BatchStats(len(listed_tasks), len(skipped_tasks), time.perf_counter() - start_time)
    if args.report_json:
        _write_task_list_report(listed_tasks, skipped_tasks, stats, Path(args.report_json))
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _run_batch_questions_mode(tasks: list[TaskSpec], args: argparse.Namespace, start_time: float) -> int:
    if args.output_dir:
        print("--questions 不支持 --output-dir，请使用 --output-file 写入追问文案。", file=sys.stderr)
        return 1
    question_tasks, skipped_tasks = _dedupe_preview_tasks(tasks, args.dedupe)
    question_report = _build_batch_question_report(question_tasks)
    question_text = _format_batch_question_report(question_report)
    summary_only = args.summary_only or args.check
    if args.output_file:
        _write_text_file(question_text, Path(args.output_file))
    elif not summary_only:
        print(question_text)
    elapsed_seconds = time.perf_counter() - start_time
    if args.report_json:
        _write_batch_question_report(question_report, skipped_tasks, elapsed_seconds, Path(args.report_json))
    print(_format_questions_summary(question_report, len(skipped_tasks), elapsed_seconds))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and (question_report["needs_input_count"] or skipped_tasks):
        return 1
    return 0


def _run_batch_profile_mode(tasks: list[TaskSpec], args: argparse.Namespace, start_time: float) -> int:
    if args.output_dir:
        print("--profile-tasks 不支持 --output-dir，请使用 --output-file 写入画像文本。", file=sys.stderr)
        return 1
    profile_tasks, skipped_tasks = _dedupe_preview_tasks(tasks, args.dedupe)
    profile_report = _build_batch_profile_report(profile_tasks)
    profile_text = _format_batch_profile_report(profile_report)
    summary_only = args.summary_only or args.check
    if args.output_file:
        _write_text_file(profile_text, Path(args.output_file))
    elif not summary_only:
        print(profile_text)
    elapsed_seconds = time.perf_counter() - start_time
    skipped_count = len(skipped_tasks) + int(profile_report["input_error_count"])
    stats = BatchStats(int(profile_report["profiled_count"]), skipped_count, elapsed_seconds)
    if args.report_json:
        _write_batch_profile_report(profile_report, skipped_tasks, stats, Path(args.report_json))
    print(_format_batch_profile_summary(profile_report, len(skipped_tasks), elapsed_seconds))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _build_batch_profile_report(tasks: list[TaskSpec]) -> dict[str, object]:
    task_reports = [_task_profile_report(task) for task in tasks]
    profiled_reports = [report for report in task_reports if not report["input_error"]]
    return {
        "task_count": len(task_reports),
        "profiled_count": len(profiled_reports),
        "input_error_count": len(task_reports) - len(profiled_reports),
        "high_risk_count": sum(1 for report in profiled_reports if report.get("risk_level") == "high"),
        "task_type_counts": _batch_profile_task_type_counts(profiled_reports),
        "complexity_counts": _batch_profile_value_counts(profiled_reports, "complexity_level"),
        "risk_level_counts": _batch_profile_value_counts(profiled_reports, "risk_level"),
        "tasks": task_reports,
    }


def _task_profile_report(task: TaskSpec) -> dict[str, object]:
    if task.load_error:
        return _failed_task_profile_report(task.name, task.description, task.load_error)
    profile_text, profile_source = _task_profile_text(task)
    if not profile_text:
        return _failed_task_profile_report(task.name, task.description, "缺少 description 或 fields")
    profile = build_task_profile(profile_text)
    values, field_sources = _task_question_field_values(task)
    missing_fields = _missing_keys(values)
    task_type = profile.get("task_type", {})
    complexity = profile.get("complexity", {})
    complexity_level = str(complexity.get("level", "unknown")) if isinstance(complexity, dict) else "unknown"
    risk_level = str(profile.get("risk_level", "unknown"))
    return {
        "name": task.name,
        "description": task.description,
        "profile_source": profile_source,
        "input_error": "",
        "task_type": task_type,
        "complexity": complexity,
        "complexity_level": complexity_level,
        "risk_level": risk_level,
        "risk_score": profile.get("risk_score", 0),
        "risk_factors": profile.get("risk_factors", []),
        "description_missing": profile.get("missing", []),
        "present_fields": [key for key in ELEMENT_ORDER if key not in missing_fields],
        "missing_fields": missing_fields,
        "field_sources": field_sources,
        "ask_strategy": profile.get("ask_strategy", ""),
        "recommended_fields": profile.get("recommended_fields", {}),
        "summary": _task_profile_summary(task_type, complexity_level, risk_level, profile.get("risk_score", 0), missing_fields),
    }


def _task_profile_text(task: TaskSpec) -> tuple[str, str]:
    description = _normalize_description(task.description)
    if description:
        return description, "description"
    field_text = _normalize_description(" ".join(value for value in task.fields.values() if value))
    if field_text:
        return field_text, "fields"
    return "", "missing"


def _failed_task_profile_report(task_name: str, description: str, reason: str) -> dict[str, object]:
    return {
        "name": task_name,
        "description": description,
        "profile_source": "missing",
        "input_error": reason,
        "task_type": {},
        "complexity": {},
        "complexity_level": "unknown",
        "risk_level": "unknown",
        "risk_score": 0,
        "risk_factors": [],
        "description_missing": list(ELEMENT_ORDER),
        "present_fields": [],
        "missing_fields": list(ELEMENT_ORDER),
        "field_sources": {key: "missing" for key in ELEMENT_ORDER},
        "ask_strategy": "",
        "recommended_fields": {},
        "summary": f"任务输入无效：{reason}",
    }


def _task_profile_summary(
    task_type: object,
    complexity_level: str,
    risk_level: str,
    risk_score: object,
    missing_fields: list[str],
) -> str:
    task_label = task_type.get("label", "未知类型") if isinstance(task_type, dict) else "未知类型"
    missing_text = _format_labels(missing_fields)
    return f"类型 {task_label}，复杂度 {complexity_level}，风险 {risk_level}/{risk_score}，缺失要素：{missing_text}。"


def _batch_profile_task_type_counts(task_reports: list[dict[str, object]]) -> list[dict[str, object]]:
    counts: dict[str, dict[str, object]] = {}
    for report in task_reports:
        task_type = report.get("task_type", {})
        if not isinstance(task_type, dict):
            continue
        type_id = str(task_type.get("id", "unknown"))
        if not type_id:
            type_id = "unknown"
        entry = counts.setdefault(type_id, {"id": type_id, "label": str(task_type.get("label", type_id)), "count": 0})
        entry["count"] = int(entry["count"]) + 1
    return sorted(counts.values(), key=lambda item: (-int(item["count"]), str(item["id"])))


def _batch_profile_value_counts(task_reports: list[dict[str, object]], key: str) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for report in task_reports:
        value = str(report.get(key, "unknown"))
        counts[value] = counts.get(value, 0) + 1
    return [
        {"id": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _format_batch_profile_report(profile_report: dict[str, object]) -> str:
    task_reports = profile_report.get("tasks", [])
    if not isinstance(task_reports, list) or not task_reports:
        return "批量任务画像：\n无任务。"
    lines = [
        "批量任务画像：",
        f"任务类型分布：{_format_profile_count_entries(profile_report.get('task_type_counts', []), 'label')}",
        f"复杂度分布：{_format_profile_count_entries(profile_report.get('complexity_counts', []), 'id')}",
        f"风险分布：{_format_profile_count_entries(profile_report.get('risk_level_counts', []), 'id')}",
        "",
    ]
    for task_report in task_reports:
        if not isinstance(task_report, dict):
            continue
        lines.extend(_format_task_profile_lines(task_report))
    return "\n".join(lines).rstrip()


def _format_profile_count_entries(entries: object, label_key: str) -> str:
    if not isinstance(entries, list) or not entries:
        return "无"
    parts: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get(label_key, entry.get("id", "unknown")))
        parts.append(f"{label} {entry.get('count', 0)} 个")
    return "、".join(parts) if parts else "无"


def _format_task_profile_lines(task_report: dict[str, object]) -> list[str]:
    name = task_report.get("name", "未知任务")
    if task_report.get("input_error"):
        return [f"- {name}：输入错误：{task_report.get('input_error')}"]
    return [
        (
            f"- {name}：{task_report.get('summary', '')}"
            f"画像来源：{task_report.get('profile_source', 'unknown')}。"
        )
    ]


def _write_batch_profile_report(
    profile_report: dict[str, object],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "task_profile": profile_report,
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _format_batch_profile_summary(profile_report: dict[str, object], skipped_count: int, elapsed_seconds: float) -> str:
    return (
        "画像生成完成："
        f"已画像 {profile_report.get('profiled_count', 0)} 个，"
        f"输入错误 {profile_report.get('input_error_count', 0)} 个，"
        f"高风险 {profile_report.get('high_risk_count', 0)} 个，"
        f"跳过 {skipped_count} 个，总耗时 {elapsed_seconds:.2f} 秒。"
    )


def _run_merge_supplements_mode(tasks: list[TaskSpec], args: argparse.Namespace, start_time: float) -> int:
    if args.output_dir:
        print("--merge-supplements 不支持 --output-dir，请使用 --output-file 写入合并后的 JSON。", file=sys.stderr)
        return 1
    supplements = _load_supplements(Path(args.merge_supplements))
    merge_report = _build_supplement_merge_report(tasks, supplements)
    merged_json = json.dumps(merge_report["merged_tasks"], ensure_ascii=False, indent=2)
    summary_only = args.summary_only or args.check
    if args.output_file:
        _write_text_file(merged_json, Path(args.output_file))
    elif not summary_only:
        print(merged_json)
    elapsed_seconds = time.perf_counter() - start_time
    if args.report_json:
        _write_supplement_merge_report(merge_report, elapsed_seconds, Path(args.report_json))
    print(_format_supplement_merge_summary(merge_report, elapsed_seconds))
    fail_on_skipped = args.fail_on_skipped or args.check
    if fail_on_skipped and not merge_report["valid"]:
        return 1
    return 0


def _load_supplements(supplements_path: Path) -> list[SupplementEntry]:
    suffix = supplements_path.suffix.lower()
    if suffix == ".json":
        return _supplements_from_json_data(json.loads(supplements_path.read_text(encoding="utf-8")))
    if suffix == ".csv":
        return _load_csv_supplements(supplements_path)
    supported = "、".join(SUPPORTED_SUFFIXES)
    raise ValueError(f"--merge-supplements 不支持的补充文件格式：{suffix}，仅支持 {supported}")


def _supplements_from_json_data(data: Any) -> list[SupplementEntry]:
    if isinstance(data, dict):
        entries: list[SupplementEntry] = []
        for task_name, raw_value in data.items():
            entries.extend(_supplement_entries_from_value(_string_value(task_name), raw_value))
        return entries
    if isinstance(data, list):
        return [_supplement_entry_from_json_item(item, index) for index, item in enumerate(data, start=1)]
    raise ValueError("JSON 补充回答必须是任务名到回答的对象映射，或包含 name/supplement/fields 的对象数组")


def _supplement_entries_from_value(task_name: str, raw_value: Any) -> list[SupplementEntry]:
    if not task_name:
        raise ValueError("补充回答映射中存在空任务名")
    if isinstance(raw_value, list):
        return [_supplement_entry_from_value(task_name, item) for item in raw_value]
    return [_supplement_entry_from_value(task_name, raw_value)]


def _supplement_entry_from_json_item(item: Any, index: int) -> SupplementEntry:
    if not isinstance(item, dict):
        raise ValueError(f"第 {index} 个 JSON 补充回答必须是对象")
    task_name = _string_value(item.get("name") or item.get("task_name") or item.get("task"))
    if not task_name:
        raise ValueError(f"第 {index} 个 JSON 补充回答缺少 name/task_name/task")
    return _supplement_entry_from_mapping(task_name, item)


def _supplement_entry_from_value(task_name: str, raw_value: Any) -> SupplementEntry:
    if isinstance(raw_value, dict):
        return _supplement_entry_from_mapping(task_name, raw_value)
    return SupplementEntry(task_name=task_name, text=_string_value(raw_value), fields={})


def _supplement_entry_from_mapping(task_name: str, item: dict[str, Any]) -> SupplementEntry:
    nested_fields = _fields_from_mapping(item.get("fields"))
    direct_fields = _fields_from_mapping(item)
    nested_fields.update(direct_fields)
    return SupplementEntry(
        task_name=task_name,
        text=_supplement_text_from_mapping(item),
        fields=nested_fields,
    )


def _supplement_text_from_mapping(item: dict[str, Any]) -> str:
    parts: list[str] = []
    seen_parts: set[str] = set()
    for field_name in SUPPLEMENT_TEXT_FIELDS:
        value = _string_value(item.get(field_name))
        if not value or value in seen_parts:
            continue
        parts.append(value)
        seen_parts.add(value)
    return "\n".join(parts)


def _load_csv_supplements(supplements_path: Path) -> list[SupplementEntry]:
    with supplements_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError("CSV 补充文件缺少表头")
        return [_supplement_entry_from_csv_row(row, index) for index, row in enumerate(reader, start=1)]


def _supplement_entry_from_csv_row(row: dict[str, str | None], index: int) -> SupplementEntry:
    task_name = _string_value(row.get("name") or row.get("task_name") or row.get("task"))
    if not task_name:
        raise ValueError(f"第 {index} 行 CSV 补充回答缺少 name/task_name/task")
    return SupplementEntry(
        task_name=task_name,
        text=_supplement_text_from_mapping(row),
        fields=_fields_from_mapping(row),
    )


def _build_supplement_merge_report(tasks: list[TaskSpec], supplements: list[SupplementEntry]) -> dict[str, object]:
    supplements_by_name = _supplements_by_task_name(supplements)
    known_names = {task.name for task in tasks}
    task_results = [_merge_task_supplements(task, supplements_by_name.get(task.name, [])) for task in tasks]
    task_reports = [result["report"] for result in task_results]
    merged_tasks = [result["task"] for result in task_results]
    unknown_supplements = _unknown_supplement_reports(supplements, known_names)
    ready_count = sum(1 for report in task_reports if report["ready_to_generate"])
    needs_input_count = sum(1 for report in task_reports if report["missing_fields"])
    input_error_count = sum(1 for report in task_reports if report["input_error"])
    applied_task_count = sum(1 for report in task_reports if report["applied_supplement_count"])
    return {
        "valid": ready_count == len(task_reports) and not unknown_supplements,
        "task_count": len(task_reports),
        "ready_count": ready_count,
        "needs_input_count": needs_input_count,
        "input_error_count": input_error_count,
        "supplement_count": len(supplements),
        "applied_task_count": applied_task_count,
        "unknown_supplement_count": len(unknown_supplements),
        "unknown_supplements": unknown_supplements,
        "tasks": task_reports,
        "merged_tasks": merged_tasks,
    }


def _supplements_by_task_name(supplements: list[SupplementEntry]) -> dict[str, list[SupplementEntry]]:
    grouped: dict[str, list[SupplementEntry]] = {}
    for supplement in supplements:
        grouped.setdefault(supplement.task_name, []).append(supplement)
    return grouped


def _unknown_supplement_reports(supplements: list[SupplementEntry], known_names: set[str]) -> list[dict[str, object]]:
    return [
        {
            "name": supplement.task_name,
            "text_preview": _normalize_description(supplement.text)[:160],
            "fields": sorted(supplement.fields),
            "suggestion": "确认任务名是否与输入清单的 name 完全一致，或把该补充回答移除。",
        }
        for supplement in supplements
        if supplement.task_name not in known_names
    ]


def _merge_task_supplements(task: TaskSpec, supplements: list[SupplementEntry]) -> dict[str, object]:
    merged_description = _merged_task_description(task, supplements)
    field_values, field_sources = _base_merge_field_values(task, merged_description)
    combined_text = _merge_combined_text(merged_description, supplements)
    _apply_supplement_label_fields(field_values, field_sources, supplements)
    _merge_supplement_inferred_fields(field_values, field_sources, combined_text)
    _apply_supplement_direct_fields(field_values, field_sources, supplements)
    missing_fields = _missing_keys(field_values)
    input_error = _merge_input_error(task, merged_description)
    ready_to_generate = not input_error and not missing_fields
    return {
        "task": _merged_task_json(task, merged_description, field_values),
        "report": {
            "name": task.name,
            "ready_to_generate": ready_to_generate,
            "input_error": input_error,
            "applied_supplement_count": len(supplements),
            "present_fields": [key for key in ELEMENT_ORDER if key not in missing_fields],
            "missing_fields": missing_fields,
            "field_sources": field_sources,
            "summary": _supplement_merge_task_summary(input_error, missing_fields, len(supplements)),
        },
    }


def _merged_task_description(task: TaskSpec, supplements: list[SupplementEntry]) -> str:
    if task.description:
        return task.description
    for supplement in supplements:
        if supplement.text:
            return _normalize_description(supplement.text)
    return ""


def _base_merge_field_values(task: TaskSpec, merged_description: str) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, str] = {}
    sources: dict[str, str] = {key: "missing" for key in ELEMENT_ORDER}
    if merged_description:
        analysis = analyze_description(merged_description)
        description_fields = _extract_labeled_fields(merged_description)
        for key, value in description_fields.items():
            values[key] = value
            sources[key] = "description_label" if task.description else "supplement_description_label"
        source = "description_inferred" if task.description else "supplement_description_inferred"
        _merge_present_fields_with_source(values, sources, merged_description, list(analysis["present"].keys()), source)
    for key, value in task.fields.items():
        if value:
            values[key] = value
            sources[key] = "task_fields"
    return values, sources


def _merge_present_fields_with_source(
    values: dict[str, str],
    sources: dict[str, str],
    description: str,
    present_keys: list[str],
    source: str,
) -> None:
    for key in present_keys:
        if key in values:
            continue
        values[key] = _description_fallback(key, description)
        sources[key] = source


def _merge_combined_text(merged_description: str, supplements: list[SupplementEntry]) -> str:
    parts = [merged_description, *(supplement.text for supplement in supplements)]
    return "\n".join(part for part in parts if part)


def _apply_supplement_label_fields(
    field_values: dict[str, str],
    field_sources: dict[str, str],
    supplements: list[SupplementEntry],
) -> None:
    for supplement in supplements:
        for key, value in _extract_labeled_fields(supplement.text).items():
            if value:
                field_values[key] = value
                field_sources[key] = "supplement_label"


def _merge_supplement_inferred_fields(
    field_values: dict[str, str],
    field_sources: dict[str, str],
    combined_text: str,
) -> None:
    if not combined_text.strip():
        return
    analysis = analyze_description(combined_text)
    _merge_present_fields_with_source(
        field_values,
        field_sources,
        combined_text,
        list(analysis["present"].keys()),
        "supplement_inferred",
    )


def _apply_supplement_direct_fields(
    field_values: dict[str, str],
    field_sources: dict[str, str],
    supplements: list[SupplementEntry],
) -> None:
    for supplement in supplements:
        for key, value in supplement.fields.items():
            if value:
                field_values[key] = value
                field_sources[key] = "supplement_fields"


def _merge_input_error(task: TaskSpec, merged_description: str) -> str:
    if task.load_error:
        return task.load_error
    if not merged_description:
        return "缺少 description"
    return ""


def _merged_task_json(task: TaskSpec, merged_description: str, field_values: dict[str, str]) -> dict[str, object]:
    item: dict[str, object] = {"name": task.name, "description": merged_description}
    if task.inspect_path:
        item["inspect_path"] = task.inspect_path
    if task.depends_on:
        item["depends_on"] = task.depends_on
    fields = {key: field_values[key] for key in ELEMENT_ORDER if field_values.get(key)}
    if fields:
        item["fields"] = fields
    return item


def _supplement_merge_task_summary(input_error: str, missing_fields: list[str], applied_count: int) -> str:
    if input_error:
        return f"任务输入需修复：{input_error}"
    prefix = f"已应用 {applied_count} 条补充回答；" if applied_count else "未匹配到补充回答；"
    if not missing_fields:
        return f"{prefix}6 要素已具备，可继续生成。"
    return f"{prefix}仍需补充 {len(missing_fields)} 个要素：{_format_labels(missing_fields)}。"


def _write_supplement_merge_report(
    merge_report: dict[str, object],
    elapsed_seconds: float,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    unready_count = int(merge_report["task_count"]) - int(merge_report["ready_count"])
    report = {
        "success_count": merge_report["ready_count"],
        "skipped_count": unready_count + int(merge_report["unknown_supplement_count"]),
        "elapsed_seconds": round(elapsed_seconds, 4),
        "supplement_merge": merge_report,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _format_supplement_merge_summary(merge_report: dict[str, object], elapsed_seconds: float) -> str:
    return (
        "补充合并完成："
        f"任务 {merge_report.get('task_count', 0)} 个，"
        f"可生成 {merge_report.get('ready_count', 0)} 个，"
        f"仍需补充 {merge_report.get('needs_input_count', 0)} 个，"
        f"输入错误 {merge_report.get('input_error_count', 0)} 个，"
        f"已应用补充 {merge_report.get('applied_task_count', 0)} 个任务/"
        f"{merge_report.get('supplement_count', 0)} 条，"
        f"未匹配补充 {merge_report.get('unknown_supplement_count', 0)} 条，"
        f"总耗时 {elapsed_seconds:.2f} 秒。"
    )


def _build_batch_question_report(tasks: list[TaskSpec]) -> dict[str, object]:
    task_reports = [_task_question_report(task) for task in tasks]
    ready_count = sum(1 for report in task_reports if report["ready_to_generate"])
    input_error_count = sum(1 for report in task_reports if report["input_error"])
    question_count = sum(len(report["questions"]) for report in task_reports)
    return {
        "all_ready": ready_count == len(task_reports),
        "task_count": len(task_reports),
        "ready_count": ready_count,
        "needs_input_count": len(task_reports) - ready_count,
        "input_error_count": input_error_count,
        "question_count": question_count,
        "tasks": task_reports,
    }


def _task_question_report(task: TaskSpec) -> dict[str, object]:
    if task.load_error:
        return _input_error_question_report(task.name, task.description, task.load_error)
    values, field_sources = _task_question_field_values(task)
    missing_fields = _missing_keys(values)
    input_error = "" if task.description else "缺少 description"
    questions = _task_input_questions(input_error) + [_field_question_entry(key) for key in missing_fields]
    return {
        "name": task.name,
        "description": task.description,
        "ready_to_generate": not input_error and not missing_fields,
        "input_error": input_error,
        "present_fields": [key for key in ELEMENT_ORDER if key not in missing_fields],
        "missing_fields": missing_fields,
        "field_sources": field_sources,
        "questions": questions,
        "summary": _task_question_summary(input_error, missing_fields),
    }


def _input_error_question_report(task_name: str, description: str, reason: str) -> dict[str, object]:
    return {
        "name": task_name,
        "description": description,
        "ready_to_generate": False,
        "input_error": reason,
        "present_fields": [],
        "missing_fields": list(ELEMENT_ORDER),
        "field_sources": {key: "missing" for key in ELEMENT_ORDER},
        "questions": [
            {
                "element": "task",
                "label": "任务输入",
                "question": f"请先修复任务输入：{reason}",
                "example": _skip_suggestion(reason),
            }
        ],
        "summary": f"任务输入无效：{reason}",
    }


def _task_question_field_values(task: TaskSpec) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, str] = {}
    sources: dict[str, str] = {key: "missing" for key in ELEMENT_ORDER}
    if task.description:
        analysis = analyze_description(task.description)
        description_fields = _extract_labeled_fields(task.description)
        values.update(description_fields)
        for key in description_fields:
            sources[key] = "description_label"
        _merge_lint_description_present(values, sources, task.description, list(analysis["present"].keys()))
    for key, value in task.fields.items():
        if value:
            values[key] = value
            sources[key] = "fields"
    return values, sources


def _task_input_questions(input_error: str) -> list[dict[str, str]]:
    if not input_error:
        return []
    return [
        {
            "element": "description",
            "label": "任务描述（description）",
            "question": "请补充 description 字段，说明编码目标和上下文。",
            "example": "示例：为 src/services/payment.py 补齐 pytest 单元测试，并说明验证命令和约束。",
        }
    ]


def _field_question_entry(key: str) -> dict[str, str]:
    return {
        "element": key,
        "label": ELEMENT_LABELS[key],
        "question": f"请补充 {ELEMENT_LABELS[key]}。",
        "example": QUESTION_EXAMPLES[key],
    }


def _task_question_summary(input_error: str, missing_fields: list[str]) -> str:
    if input_error:
        return f"任务输入需修复：{input_error}"
    if not missing_fields:
        return "6 要素已具备，无需追问。"
    return f"需补充 {len(missing_fields)} 个要素：{_format_labels(missing_fields)}。"


def _format_batch_question_report(question_report: dict[str, object]) -> str:
    task_reports = question_report.get("tasks", [])
    if not isinstance(task_reports, list) or not task_reports:
        return "批量缺失信息追问：\n无可追问任务。"
    if question_report.get("all_ready"):
        return "批量缺失信息追问：\n所有任务都已具备 6 要素，无需追问。"
    lines = ["批量缺失信息追问：", ""]
    for task_report in task_reports:
        if not isinstance(task_report, dict) or task_report.get("ready_to_generate"):
            continue
        lines.extend(_format_task_question_block(task_report))
    lines.append("你可以让需求方按任务名逐条简短回答，我会把补充内容合并成完整 /goal 指令。")
    return "\n".join(lines)


def _format_task_question_block(task_report: dict[str, object]) -> list[str]:
    name = task_report.get("name", "未知任务")
    lines = [f"任务：{name}"]
    description = str(task_report.get("description", "")).strip()
    if description:
        lines.append(f"当前描述：{description[:160]}")
    lines.append("请补充以下信息：")
    questions = task_report.get("questions", [])
    if isinstance(questions, list):
        for index, question in enumerate(questions, start=1):
            if isinstance(question, dict):
                lines.append(f"{index}. {question.get('label', '缺失信息')}：{question.get('example', '')}")
    lines.append("")
    return lines


def _write_text_file(content: str, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(f"{content}\n", encoding="utf-8")
    print(f"已写入：{output_file}")


def _write_batch_question_report(
    question_report: dict[str, object],
    skipped_tasks: list[SkippedTask],
    elapsed_seconds: float,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": question_report["ready_count"],
        "skipped_count": question_report["needs_input_count"] + len(skipped_tasks),
        "elapsed_seconds": round(elapsed_seconds, 4),
        "questions": question_report,
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _format_questions_summary(question_report: dict[str, object], skipped_count: int, elapsed_seconds: float) -> str:
    return (
        "追问文案生成完成："
        f"无需追问 {question_report.get('ready_count', 0)} 个，"
        f"需补充 {question_report.get('needs_input_count', 0)} 个，"
        f"问题 {question_report.get('question_count', 0)} 条，"
        f"输入错误 {question_report.get('input_error_count', 0)} 个，"
        f"跳过 {skipped_count} 个，总耗时 {elapsed_seconds:.2f} 秒。"
    )


def _run_dependency_plan_mode(tasks: list[TaskSpec], args: argparse.Namespace, start_time: float) -> int:
    planned_tasks, skipped_tasks = _dedupe_preview_tasks(tasks, args.dedupe)
    plan = _build_dependency_plan(planned_tasks)
    summary_only = args.summary_only or args.check
    if not summary_only:
        print(_format_dependency_plan(plan))
    elapsed_seconds = time.perf_counter() - start_time
    issue_count = len(plan["issues"]) if isinstance(plan.get("issues"), list) else 0
    stats = BatchStats(len(planned_tasks), len(skipped_tasks) + issue_count, elapsed_seconds)
    if args.report_json:
        _write_dependency_plan_report(plan, skipped_tasks, stats, Path(args.report_json))
    print(_format_summary(stats))
    fail_on_skipped = args.fail_on_skipped or args.check
    if not plan.get("valid", False):
        return 1
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _run_batch_redaction_check_mode(tasks: list[TaskSpec], args: argparse.Namespace, start_time: float) -> int:
    checked_tasks, skipped_tasks = _dedupe_preview_tasks(tasks, args.dedupe)
    redaction_report = _build_batch_redaction_report(checked_tasks)
    summary_only = args.summary_only or args.check
    if not summary_only:
        print(_format_batch_redaction_report(redaction_report))
    elapsed_seconds = time.perf_counter() - start_time
    risky_count = int(redaction_report["risky_count"])
    stats = BatchStats(int(redaction_report["safe_count"]), risky_count + len(skipped_tasks), elapsed_seconds)
    if args.report_json:
        _write_batch_redaction_report(redaction_report, skipped_tasks, stats, Path(args.report_json))
    print(_format_redaction_summary(redaction_report, len(skipped_tasks), elapsed_seconds))
    fail_on_skipped = args.fail_on_skipped or args.check
    if risky_count:
        return 1
    if fail_on_skipped and skipped_tasks:
        return 1
    return 0


def _build_batch_redaction_report(tasks: list[TaskSpec]) -> dict[str, object]:
    task_reports = [_task_redaction_report(task) for task in tasks]
    risky_count = sum(1 for report in task_reports if not report["safe_to_share"])
    finding_count = sum(int(report.get("finding_count", 0)) for report in task_reports)
    return {
        "safe_to_share": risky_count == 0,
        "task_count": len(task_reports),
        "safe_count": len(task_reports) - risky_count,
        "risky_count": risky_count,
        "finding_count": finding_count,
        "tasks": task_reports,
    }


def _task_redaction_report(task: TaskSpec) -> dict[str, object]:
    if task.load_error:
        return {
            "name": task.name,
            "safe_to_share": False,
            "risk_level": "input_error",
            "risk_score": 100,
            "finding_count": 0,
            "finding_types": [],
            "findings": [],
            "redacted_preview": "",
            "recommended_action": f"修复任务输入错误后再审计：{task.load_error}",
        }
    text = _task_redaction_text(task)
    if not text.strip():
        return {
            "name": task.name,
            "safe_to_share": True,
            "risk_level": "none",
            "risk_score": 0,
            "finding_count": 0,
            "finding_types": [],
            "findings": [],
            "redacted_preview": "",
            "recommended_action": "任务没有可审计文本。",
        }
    report = check_redaction(text)
    return {
        "name": task.name,
        "safe_to_share": report["safe_to_share"],
        "risk_level": report["risk_level"],
        "risk_score": report["risk_score"],
        "finding_count": report["finding_count"],
        "finding_types": report["finding_types"],
        "findings": report["findings"],
        "redacted_preview": report["redacted_preview"],
        "recommended_action": report["recommended_action"],
    }


def _task_redaction_text(task: TaskSpec) -> str:
    parts = [f"name: {task.name}"]
    if task.description:
        parts.append(f"description: {task.description}")
    if task.inspect_path:
        parts.append(f"inspect_path: {task.inspect_path}")
    for key in ELEMENT_ORDER:
        value = task.fields.get(key)
        if value:
            parts.append(f"{key}: {value}")
    return "\n".join(parts)


def _format_batch_redaction_report(redaction_report: dict[str, object]) -> str:
    lines = ["批量敏感信息审计："]
    task_reports = redaction_report.get("tasks", [])
    if not isinstance(task_reports, list) or not task_reports:
        lines.append("无可审计任务。")
        return "\n".join(lines)
    for task_report in task_reports:
        if isinstance(task_report, dict):
            lines.extend(_format_task_redaction_lines(task_report))
    return "\n".join(lines)


def _format_task_redaction_lines(task_report: dict[str, object]) -> list[str]:
    name = task_report.get("name", "未知任务")
    status = "安全" if task_report.get("safe_to_share") else "有风险"
    lines = [
        (
            f"- {name}：{status}，风险级别 {task_report.get('risk_level', 'unknown')}，"
            f"发现 {task_report.get('finding_count', 0)} 个。"
        )
    ]
    findings = task_report.get("findings", [])
    if not isinstance(findings, list) or not findings:
        return lines
    for finding in findings[:3]:
        if not isinstance(finding, dict):
            continue
        lines.append(
            f"  - {finding.get('type', 'unknown')} / {finding.get('severity', 'unknown')}："
            f"{finding.get('preview', '')}；建议：{finding.get('recommendation', '')}"
        )
    if len(findings) > 3:
        lines.append(f"  - 其余 {len(findings) - 3} 个发现请查看 --report-json。")
    return lines


def _format_redaction_summary(redaction_report: dict[str, object], skipped_count: int, elapsed_seconds: float) -> str:
    return (
        "敏感信息审计完成："
        f"安全 {redaction_report.get('safe_count', 0)} 个，"
        f"有风险 {redaction_report.get('risky_count', 0)} 个，"
        f"发现 {redaction_report.get('finding_count', 0)} 个，"
        f"跳过 {skipped_count} 个，总耗时 {elapsed_seconds:.2f} 秒。"
    )


def _run_lint_fields_mode(tasks: list[TaskSpec], args: argparse.Namespace, start_time: float) -> int:
    lint_tasks, skipped_tasks = _dedupe_preview_tasks(tasks, args.dedupe)
    lint_report = _build_batch_field_lint_report(lint_tasks)
    summary_only = args.summary_only or args.check
    if not summary_only:
        print(_format_batch_field_lint_report(lint_report))
    elapsed_seconds = time.perf_counter() - start_time
    failed_count = int(lint_report["failed_count"])
    stats = BatchStats(int(lint_report["passed_count"]), failed_count + len(skipped_tasks), elapsed_seconds)
    if args.report_json:
        _write_batch_field_lint_report(lint_report, skipped_tasks, stats, Path(args.report_json))
    print(_format_lint_fields_summary(lint_report, len(skipped_tasks), elapsed_seconds))
    fail_on_skipped = args.fail_on_skipped or args.check
    if failed_count:
        return 1
    if fail_on_skipped and skipped_tasks:
        return 1
    return 0


def _build_batch_field_lint_report(tasks: list[TaskSpec]) -> dict[str, object]:
    task_reports = [_task_field_lint_report(task) for task in tasks]
    failed_count = sum(1 for report in task_reports if not report["passed"])
    return {
        "valid": failed_count == 0,
        "task_count": len(task_reports),
        "passed_count": len(task_reports) - failed_count,
        "failed_count": failed_count,
        "tasks": task_reports,
    }


def _task_field_lint_report(task: TaskSpec) -> dict[str, object]:
    if task.load_error:
        return _failed_task_field_lint_report(task.name, task.description, task.load_error)
    field_values, field_sources = _task_lint_field_values(task)
    lint_report = lint_fields_json_data({"fields": field_values}, task.name)
    validation = lint_report.get("validation", {})
    return {
        "name": task.name,
        "description": task.description,
        "passed": lint_report["passed"],
        "score": lint_report["score"],
        "issue_count": lint_report["issue_count"],
        "high_issue_count": lint_report["high_issue_count"],
        "field_sources": field_sources,
        "missing_fields": validation.get("missing_fields", []) if isinstance(validation, dict) else [],
        "issues": lint_report["issues"],
        "summary": lint_report["summary"],
    }


def _failed_task_field_lint_report(task_name: str, description: str, reason: str) -> dict[str, object]:
    return {
        "name": task_name,
        "description": description,
        "passed": False,
        "score": 0,
        "issue_count": 1,
        "high_issue_count": 1,
        "field_sources": {key: "missing" for key in ELEMENT_ORDER},
        "missing_fields": list(ELEMENT_ORDER),
        "issues": [
            {
                "element": "task",
                "label": "任务输入",
                "severity": "high",
                "message": reason,
                "suggestion": _skip_suggestion(reason),
            }
        ],
        "summary": f"任务输入无效：{reason}",
    }


def _task_lint_field_values(task: TaskSpec) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, str] = {}
    sources: dict[str, str] = {key: "missing" for key in ELEMENT_ORDER}
    if task.description:
        analysis = analyze_description(task.description)
        description_fields = _extract_labeled_fields(task.description)
        values.update(description_fields)
        for key in description_fields:
            sources[key] = "description_label"
        _merge_lint_description_present(values, sources, task.description, list(analysis["present"].keys()))
    for key, value in task.fields.items():
        if value:
            values[key] = value
            sources[key] = "fields"
    return values, sources


def _merge_lint_description_present(
    values: dict[str, str],
    sources: dict[str, str],
    description: str,
    present_keys: list[str],
) -> None:
    for key in present_keys:
        if key in values:
            continue
        values[key] = _description_fallback(key, description)
        sources[key] = "description_inferred"


def _format_batch_field_lint_report(lint_report: dict[str, object]) -> str:
    lines = ["批量字段语义质量检查："]
    task_reports = lint_report.get("tasks", [])
    if not isinstance(task_reports, list) or not task_reports:
        lines.append("无可检查任务。")
        return "\n".join(lines)
    for task_report in task_reports:
        if isinstance(task_report, dict):
            lines.extend(_format_task_field_lint_lines(task_report))
    return "\n".join(lines)


def _format_task_field_lint_lines(task_report: dict[str, object]) -> list[str]:
    name = task_report.get("name", "未知任务")
    status = "通过" if task_report.get("passed") else "未通过"
    lines = [
        (
            f"- {name}：{status}，得分 {task_report.get('score', 0)}，"
            f"问题 {task_report.get('issue_count', 0)} 个，高优先级 {task_report.get('high_issue_count', 0)} 个。"
        )
    ]
    if task_report.get("passed"):
        return lines
    issues = task_report.get("issues", [])
    if not isinstance(issues, list):
        return lines
    for issue in issues[:3]:
        if not isinstance(issue, dict):
            continue
        lines.append(
            f"  - {issue.get('label', issue.get('element', '未知要素'))}："
            f"{issue.get('message', '')}；建议：{issue.get('suggestion', '')}"
        )
    if len(issues) > 3:
        lines.append(f"  - 其余 {len(issues) - 3} 个问题请查看 --report-json。")
    return lines


def _format_lint_fields_summary(lint_report: dict[str, object], skipped_count: int, elapsed_seconds: float) -> str:
    return (
        "字段语义质量检查完成："
        f"通过 {lint_report.get('passed_count', 0)} 个，"
        f"失败 {lint_report.get('failed_count', 0)} 个，"
        f"跳过 {skipped_count} 个，总耗时 {elapsed_seconds:.2f} 秒。"
    )


def _run_inspect_paths_mode(tasks: list[TaskSpec], args: argparse.Namespace, start_time: float) -> int:
    if args.output_dir:
        print("--inspect-paths 不支持 --output-dir，请使用 --output-file 写入扫描报告。", file=sys.stderr)
        return 1
    inspect_tasks, skipped_tasks = _dedupe_preview_tasks(tasks, args.dedupe)
    inspection_report = _build_batch_inspection_report(inspect_tasks)
    report_text = _format_batch_inspection_report(inspection_report)
    summary_only = args.summary_only or args.check
    if args.output_file:
        _write_text_file(report_text, Path(args.output_file))
    elif not summary_only:
        print(report_text)
    elapsed_seconds = time.perf_counter() - start_time
    failed_count = int(inspection_report["failed_count"])
    stats = BatchStats(int(inspection_report["passed_count"]), failed_count + len(skipped_tasks), elapsed_seconds)
    if args.report_json:
        _write_batch_inspection_report(inspection_report, skipped_tasks, stats, Path(args.report_json))
    print(_format_inspect_paths_summary(inspection_report, len(skipped_tasks), elapsed_seconds))
    fail_on_skipped = args.fail_on_skipped or args.check
    if failed_count:
        return 1
    if fail_on_skipped and skipped_tasks:
        return 1
    return 0


def _build_batch_inspection_report(tasks: list[TaskSpec]) -> dict[str, object]:
    task_reports = [_task_inspection_report(task) for task in tasks]
    failed_count = sum(1 for report in task_reports if not report["passed"])
    return {
        "valid": failed_count == 0,
        "task_count": len(task_reports),
        "passed_count": len(task_reports) - failed_count,
        "failed_count": failed_count,
        "tasks": task_reports,
    }


def _task_inspection_report(task: TaskSpec) -> dict[str, object]:
    if task.load_error:
        return _failed_task_inspection_report(task, task.load_error, "修复任务输入格式后再扫描路径。")
    path_value, path_source = _task_inspection_path(task)
    if not path_value:
        return _failed_task_inspection_report(
            task,
            "缺少 path/inspect_path/target_path，且 description/fields 中未发现可扫描路径",
            "为该任务补充 path、inspect_path 或 target_path 字段，或在描述中写明真实文件/目录路径。",
        )
    try:
        context = inspect_path_context(path_value, task.description)
    except (OSError, ValueError) as error:
        return _failed_task_inspection_report(task, str(error), "确认路径存在且当前进程有权限读取，必要时收窄到具体文件或目录。", path_value, path_source)
    return {
        "name": task.name,
        "passed": True,
        "inspect_path": path_value,
        "path_source": path_source,
        "kind": context.get("kind", "unknown"),
        "file_count": context.get("file_count", 0),
        "language_counts": context.get("language_counts", {}),
        "verification_hints": context.get("verification_hints", []),
        "risk_flags": context.get("risk_flags", []),
        "suggested_fields": context.get("suggested_fields", {}),
        "context": context,
        "summary": _inspection_task_summary(context),
    }


def _failed_task_inspection_report(
    task: TaskSpec,
    reason: str,
    suggestion: str,
    path_value: str = "",
    path_source: str = "missing",
) -> dict[str, object]:
    return {
        "name": task.name,
        "passed": False,
        "inspect_path": path_value,
        "path_source": path_source,
        "kind": "error",
        "file_count": 0,
        "language_counts": {},
        "verification_hints": [],
        "risk_flags": [],
        "suggested_fields": {},
        "error": reason,
        "suggestion": suggestion,
        "summary": f"路径扫描失败：{reason}",
    }


def _task_inspection_path(task: TaskSpec) -> tuple[str, str]:
    if task.inspect_path:
        return task.inspect_path, "task.path_alias"
    combined_text = " ".join(value for value in [task.description, *task.fields.values()] if value)
    match = BATCH_INSPECT_PATH_PATTERN.search(combined_text)
    if match:
        return match.group(1).strip("`"), "description_or_fields"
    return "", "missing"


def _inspection_task_summary(context: dict[str, object]) -> str:
    path_value = context.get("path", "")
    file_count = context.get("file_count", 0)
    verification_count = len(context.get("verification_hints", [])) if isinstance(context.get("verification_hints"), list) else 0
    return f"路径 {path_value} 扫描通过，发现 {file_count} 个文件，验证建议 {verification_count} 条。"


def _format_batch_inspection_report(inspection_report: dict[str, object]) -> str:
    lines = ["批量路径上下文画像："]
    task_reports = inspection_report.get("tasks", [])
    if not isinstance(task_reports, list) or not task_reports:
        lines.append("无可扫描任务。")
        return "\n".join(lines)
    for task_report in task_reports:
        if isinstance(task_report, dict):
            lines.extend(_format_task_inspection_lines(task_report))
    return "\n".join(lines)


def _format_task_inspection_lines(task_report: dict[str, object]) -> list[str]:
    name = task_report.get("name", "未知任务")
    status = "通过" if task_report.get("passed") else "失败"
    lines = [f"- {name}：{status}，路径 {task_report.get('inspect_path') or '未提供'}。"]
    if not task_report.get("passed"):
        lines.append(f"  - 原因：{task_report.get('error', '')}")
        lines.append(f"  - 建议：{task_report.get('suggestion', '')}")
        return lines
    lines.append(f"  - 类型：{task_report.get('kind', 'unknown')}；文件数：{task_report.get('file_count', 0)}。")
    language_counts = task_report.get("language_counts", {})
    if isinstance(language_counts, dict) and language_counts:
        language_label = "、".join(f"{language} {count}" for language, count in list(language_counts.items())[:5])
        lines.append(f"  - 语言分布：{language_label}。")
    verification_hints = task_report.get("verification_hints", [])
    if isinstance(verification_hints, list) and verification_hints:
        lines.append(f"  - 验证建议：{'；'.join(str(hint) for hint in verification_hints[:3])}")
    risk_flags = task_report.get("risk_flags", [])
    if isinstance(risk_flags, list) and risk_flags:
        lines.append(f"  - 风险提示：{'；'.join(str(flag) for flag in risk_flags[:3])}")
    suggested_fields = task_report.get("suggested_fields", {})
    if isinstance(suggested_fields, dict) and suggested_fields:
        outcome = _normalize_description(str(suggested_fields.get("outcome", "")))
        boundaries = _normalize_description(str(suggested_fields.get("boundaries", "")))
        if outcome:
            lines.append(f"  - 建议目标：{outcome[:140]}")
        if boundaries:
            lines.append(f"  - 建议边界：{boundaries[:140]}")
    return lines


def _write_batch_inspection_report(
    inspection_report: dict[str, object],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "path_inspection": inspection_report,
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _format_inspect_paths_summary(inspection_report: dict[str, object], skipped_count: int, elapsed_seconds: float) -> str:
    return (
        "路径上下文画像完成："
        f"通过 {inspection_report.get('passed_count', 0)} 个，"
        f"失败 {inspection_report.get('failed_count', 0)} 个，"
        f"跳过 {skipped_count} 个，总耗时 {elapsed_seconds:.2f} 秒。"
    )


def _run_enrich_from_paths_mode(tasks: list[TaskSpec], args: argparse.Namespace, start_time: float) -> int:
    if args.output_dir:
        print("--enrich-from-paths 不支持 --output-dir，请使用 --output-file 写入增强后的任务 JSON。", file=sys.stderr)
        return 1
    enrich_tasks, skipped_tasks = _dedupe_preview_tasks(tasks, args.dedupe)
    enrichment_report = _build_path_enrichment_report(enrich_tasks)
    enriched_json = json.dumps(enrichment_report["enriched_tasks"], ensure_ascii=False, indent=2)
    summary_only = args.summary_only or args.check
    if args.output_file:
        _write_text_file(enriched_json, Path(args.output_file))
    elif not summary_only:
        print(enriched_json)
    elapsed_seconds = time.perf_counter() - start_time
    unready_count = int(enrichment_report["task_count"]) - int(enrichment_report["ready_count"])
    stats = BatchStats(int(enrichment_report["ready_count"]), unready_count + len(skipped_tasks), elapsed_seconds)
    if args.report_json:
        _write_path_enrichment_report(enrichment_report, skipped_tasks, stats, Path(args.report_json))
    print(_format_path_enrichment_summary(enrichment_report, len(skipped_tasks), elapsed_seconds))
    fail_on_skipped = args.fail_on_skipped or args.check
    if int(enrichment_report["path_error_count"]):
        return 1
    if fail_on_skipped and (not enrichment_report["valid"] or skipped_tasks):
        return 1
    return 0


def _build_path_enrichment_report(tasks: list[TaskSpec]) -> dict[str, object]:
    task_results = [_path_enrichment_task_result(task) for task in tasks]
    task_reports = [result["report"] for result in task_results]
    enriched_tasks = [result["task"] for result in task_results]
    ready_count = sum(1 for report in task_reports if report["ready_to_generate"])
    enriched_count = sum(1 for report in task_reports if report["filled_fields"])
    path_error_count = sum(1 for report in task_reports if report["path_error"])
    missing_field_count = sum(len(report["missing_after"]) for report in task_reports)
    input_error_count = sum(1 for report in task_reports if report["input_error"])
    return {
        "valid": ready_count == len(task_reports) and path_error_count == 0,
        "task_count": len(task_reports),
        "ready_count": ready_count,
        "unready_count": len(task_reports) - ready_count,
        "enriched_count": enriched_count,
        "path_error_count": path_error_count,
        "input_error_count": input_error_count,
        "missing_field_count": missing_field_count,
        "tasks": task_reports,
        "enriched_tasks": enriched_tasks,
    }


def _path_enrichment_task_result(task: TaskSpec) -> dict[str, object]:
    values, field_sources = _task_lint_field_values(task)
    input_error = _path_enrichment_input_error(task)
    missing_before = _missing_keys(values)
    fill_candidates = _path_enrichment_fill_candidates(values, field_sources)
    path_value = task.inspect_path
    path_source = "task.path_alias" if task.inspect_path else ""
    path_error = ""
    inspection_summary: dict[str, object] = {}
    filled_fields: list[str] = []
    if not input_error and fill_candidates:
        path_value, path_source = _task_inspection_path(task)
        if not path_value:
            path_error = "缺少 path/inspect_path/target_path，且 description/fields 中未发现可扫描路径"
        else:
            try:
                context = inspect_path_context(path_value, task.description)
            except (OSError, ValueError) as error:
                path_error = str(error)
            else:
                inspection_summary = _path_enrichment_inspection_summary(context)
                suggested_fields = context.get("suggested_fields", {})
                if isinstance(suggested_fields, dict):
                    filled_fields = _apply_path_suggested_fields(values, field_sources, fill_candidates, suggested_fields)
    missing_after = _missing_keys(values)
    ready_to_generate = not input_error and not missing_after
    return {
        "task": _path_enriched_task_json(task, path_value, values),
        "report": {
            "name": task.name,
            "ready_to_generate": ready_to_generate,
            "input_error": input_error,
            "inspect_path": path_value,
            "path_source": path_source or "not_needed",
            "path_error": path_error,
            "missing_before": missing_before,
            "missing_after": missing_after,
            "filled_fields": filled_fields,
            "present_fields": [key for key in ELEMENT_ORDER if key not in missing_after],
            "field_sources": field_sources,
            "inspection": inspection_summary,
            "summary": _path_enrichment_task_summary(input_error, path_error, filled_fields, missing_after),
        },
    }


def _path_enrichment_input_error(task: TaskSpec) -> str:
    if task.load_error:
        return task.load_error
    if not task.description:
        return "缺少 description"
    return ""


def _path_enrichment_inspection_summary(context: dict[str, object]) -> dict[str, object]:
    return {
        "kind": context.get("kind", "unknown"),
        "file_count": context.get("file_count", 0),
        "language_counts": context.get("language_counts", {}),
        "verification_hints": context.get("verification_hints", []),
        "risk_flags": context.get("risk_flags", []),
        "suggested_fields": context.get("suggested_fields", {}),
    }


def _path_enrichment_fill_candidates(values: dict[str, str], field_sources: dict[str, str]) -> list[str]:
    return [
        key
        for key in ELEMENT_ORDER
        if not values.get(key) or field_sources.get(key) == "description_inferred"
    ]


def _apply_path_suggested_fields(
    values: dict[str, str],
    field_sources: dict[str, str],
    fill_candidates: list[str],
    suggested_fields: dict[object, object],
) -> list[str]:
    filled_fields: list[str] = []
    for key in fill_candidates:
        value = _string_value(suggested_fields.get(key))
        if not value:
            continue
        values[key] = value
        field_sources[key] = "path_suggested"
        filled_fields.append(key)
    return filled_fields


def _path_enriched_task_json(task: TaskSpec, path_value: str, values: dict[str, str]) -> dict[str, object]:
    item: dict[str, object] = {"name": task.name, "description": task.description}
    if path_value:
        item["inspect_path"] = path_value
    if task.depends_on:
        item["depends_on"] = task.depends_on
    fields = {key: values[key] for key in ELEMENT_ORDER if values.get(key)}
    if fields:
        item["fields"] = fields
    return item


def _path_enrichment_task_summary(
    input_error: str,
    path_error: str,
    filled_fields: list[str],
    missing_after: list[str],
) -> str:
    if input_error:
        return f"任务输入需修复：{input_error}"
    if path_error:
        return f"路径回填失败：{path_error}"
    prefix = f"已从路径画像回填 {len(filled_fields)} 个要素" if filled_fields else "无需路径回填或未发现可回填要素"
    if missing_after:
        return f"{prefix}；仍缺 {_format_labels(missing_after)}。"
    return f"{prefix}；6 要素已具备，可继续生成。"


def _write_path_enrichment_report(
    enrichment_report: dict[str, object],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "path_enrichment": enrichment_report,
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _format_path_enrichment_summary(enrichment_report: dict[str, object], skipped_count: int, elapsed_seconds: float) -> str:
    return (
        "路径建议字段回填完成："
        f"任务 {enrichment_report.get('task_count', 0)} 个，"
        f"可生成 {enrichment_report.get('ready_count', 0)} 个，"
        f"已回填 {enrichment_report.get('enriched_count', 0)} 个，"
        f"路径错误 {enrichment_report.get('path_error_count', 0)} 个，"
        f"剩余缺失 {enrichment_report.get('missing_field_count', 0)} 个，"
        f"输入错误 {enrichment_report.get('input_error_count', 0)} 个，"
        f"跳过 {skipped_count} 个，总耗时 {elapsed_seconds:.2f} 秒。"
    )


def _build_output_lint_report(outputs: list[TaskOutput]) -> dict[str, object]:
    task_reports = [_output_lint_task_report(output) for output in outputs]
    failed_count = sum(1 for report in task_reports if not report["passed"])
    return {
        "passed": failed_count == 0,
        "task_count": len(task_reports),
        "passed_count": len(task_reports) - failed_count,
        "failed_count": failed_count,
        "tasks": task_reports,
    }


def _output_lint_task_report(output: TaskOutput) -> dict[str, object]:
    lint_report = lint_goal_text(output.content, output.task_name)
    validation = lint_report.get("validation", {})
    field_lint = lint_report.get("field_lint", {})
    issues = field_lint.get("issues", []) if isinstance(field_lint, dict) else []
    return {
        "name": output.task_name,
        "file_slug": output.file_slug,
        "passed": lint_report["passed"],
        "validation_valid": validation.get("valid", False) if isinstance(validation, dict) else False,
        "score": field_lint.get("score", 0) if isinstance(field_lint, dict) else 0,
        "issue_count": field_lint.get("issue_count", 0) if isinstance(field_lint, dict) else 0,
        "high_issue_count": field_lint.get("high_issue_count", 0) if isinstance(field_lint, dict) else 0,
        "missing": validation.get("missing", []) if isinstance(validation, dict) else [],
        "issues": issues if isinstance(issues, list) else [],
        "summary": lint_report["summary"],
    }


def _format_output_lint_report(lint_report: dict[str, object]) -> str:
    lines = ["批量生成输出 /goal 自检："]
    task_reports = lint_report.get("tasks", [])
    if not isinstance(task_reports, list) or not task_reports:
        lines.append("无可检查输出。")
        return "\n".join(lines)
    for task_report in task_reports:
        if isinstance(task_report, dict):
            lines.extend(_format_output_lint_task_lines(task_report))
    return "\n".join(lines)


def _format_output_lint_task_lines(task_report: dict[str, object]) -> list[str]:
    name = task_report.get("name", "未知任务")
    status = "通过" if task_report.get("passed") else "未通过"
    lines = [
        (
            f"- {name}：{status}，得分 {task_report.get('score', 0)}，"
            f"问题 {task_report.get('issue_count', 0)} 个，高优先级 {task_report.get('high_issue_count', 0)} 个。"
        )
    ]
    if task_report.get("passed"):
        return lines
    issues = task_report.get("issues", [])
    if isinstance(issues, list):
        for issue in issues[:3]:
            if not isinstance(issue, dict):
                continue
            lines.append(
                f"  - {issue.get('label', issue.get('element', '未知要素'))}："
                f"{issue.get('message', '')}；建议：{issue.get('suggestion', '')}"
            )
        if len(issues) > 3:
            lines.append(f"  - 其余 {len(issues) - 3} 个问题请查看 --report-json。")
    missing = task_report.get("missing", [])
    if isinstance(missing, list) and missing:
        lines.append(f"  - 结构缺失项：{'、'.join(str(item) for item in missing[:5])}")
    return lines


def _format_lint_output_summary(lint_report: dict[str, object]) -> str:
    return (
        "输出 /goal 自检完成："
        f"通过 {lint_report.get('passed_count', 0)} 个，"
        f"失败 {lint_report.get('failed_count', 0)} 个。"
    )


def _build_dependency_plan(tasks: list[TaskSpec]) -> dict[str, object]:
    ordered_tasks = _unique_tasks_by_name(tasks)
    known_names = {task.name for task in ordered_tasks}
    duplicate_names = _duplicate_task_names(tasks)
    issues = _dependency_duplicate_issues(duplicate_names)
    dependencies_by_task = {
        task.name: [dependency for dependency in task.depends_on if dependency in known_names and dependency != task.name]
        for task in ordered_tasks
    }
    issues.extend(_dependency_reference_issues(ordered_tasks, known_names))
    waves, cycle_names = _dependency_waves(ordered_tasks, dependencies_by_task)
    issues.extend(_dependency_cycle_issues(cycle_names))
    wave_by_task = _wave_index_by_task(waves)
    return {
        "valid": not issues,
        "task_count": len(ordered_tasks),
        "waves": [
            {"wave": index, "tasks": [_dependency_task_entry(task, dependencies_by_task, wave_by_task) for task in wave_tasks]}
            for index, wave_tasks in enumerate(waves, start=1)
        ],
        "tasks": [_dependency_task_entry(task, dependencies_by_task, wave_by_task) for task in ordered_tasks],
        "issues": issues,
    }


def _unique_tasks_by_name(tasks: list[TaskSpec]) -> list[TaskSpec]:
    unique_tasks: list[TaskSpec] = []
    seen: set[str] = set()
    for task in tasks:
        if task.name in seen:
            continue
        unique_tasks.append(task)
        seen.add(task.name)
    return unique_tasks


def _duplicate_task_names(tasks: list[TaskSpec]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for task in tasks:
        if task.name in seen:
            duplicates.add(task.name)
        seen.add(task.name)
    return sorted(duplicates)


def _dependency_duplicate_issues(duplicate_names: list[str]) -> list[dict[str, str]]:
    return [
        _dependency_issue(
            name,
            "重复任务名会导致依赖引用不唯一",
            "为重复任务改名，或先使用 --dedupe 只保留一份任务。",
        )
        for name in duplicate_names
    ]


def _dependency_reference_issues(tasks: list[TaskSpec], known_names: set[str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for task in tasks:
        for dependency in task.depends_on:
            if dependency == task.name:
                issues.append(
                    _dependency_issue(task.name, f"任务依赖自身：{dependency}", "移除自身依赖，或拆成两个命名不同的任务。")
                )
            elif dependency not in known_names:
                issues.append(
                    _dependency_issue(
                        task.name,
                        f"依赖不存在：{dependency}",
                        "确认依赖任务名称是否拼写一致，或把被依赖任务加入同一批输入文件。",
                    )
                )
    return issues


def _dependency_waves(
    tasks: list[TaskSpec],
    dependencies_by_task: dict[str, list[str]],
) -> tuple[list[list[TaskSpec]], list[str]]:
    task_by_name = {task.name: task for task in tasks}
    order_index = {task.name: index for index, task in enumerate(tasks)}
    dependents: dict[str, list[str]] = {task.name: [] for task in tasks}
    indegree: dict[str, int] = {task.name: 0 for task in tasks}
    for task_name, dependencies in dependencies_by_task.items():
        for dependency in dependencies:
            dependents[dependency].append(task_name)
            indegree[task_name] += 1

    ready = [task.name for task in tasks if indegree[task.name] == 0]
    waves: list[list[TaskSpec]] = []
    processed: set[str] = set()
    while ready:
        current_names = sorted(ready, key=lambda name: order_index[name])
        waves.append([task_by_name[name] for name in current_names])
        processed.update(current_names)
        next_ready: list[str] = []
        for name in current_names:
            for dependent in sorted(dependents[name], key=lambda item: order_index[item]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    next_ready.append(dependent)
        ready = next_ready
    cycle_names = [task.name for task in tasks if task.name not in processed]
    return waves, cycle_names


def _dependency_cycle_issues(cycle_names: list[str]) -> list[dict[str, str]]:
    return [
        _dependency_issue(
            name,
            "存在循环依赖或被循环依赖阻塞",
            "拆分或改写 depends_on，确保依赖方向形成无环图。",
        )
        for name in cycle_names
    ]


def _wave_index_by_task(waves: list[list[TaskSpec]]) -> dict[str, int]:
    wave_by_task: dict[str, int] = {}
    for wave_index, tasks in enumerate(waves, start=1):
        for task in tasks:
            wave_by_task[task.name] = wave_index
    return wave_by_task


def _dependency_task_entry(
    task: TaskSpec,
    dependencies_by_task: dict[str, list[str]],
    wave_by_task: dict[str, int],
) -> dict[str, object]:
    return {
        "name": task.name,
        "depends_on": task.depends_on,
        "known_depends_on": dependencies_by_task.get(task.name, []),
        "wave": wave_by_task.get(task.name),
    }


def _dependency_issue(task_name: str, reason: str, suggestion: str) -> dict[str, str]:
    return {"name": task_name, "reason": reason, "suggestion": suggestion}


def _format_dependency_plan(plan: dict[str, object]) -> str:
    lines = ["依赖执行计划："]
    waves = plan.get("waves", [])
    if not isinstance(waves, list) or not waves:
        lines.append("无可执行任务。")
    else:
        for wave in waves:
            if not isinstance(wave, dict):
                continue
            tasks = wave.get("tasks", [])
            task_labels = _format_dependency_wave_tasks(tasks if isinstance(tasks, list) else [])
            lines.append(f"第 {wave.get('wave')} 批：{task_labels}")
    issues = plan.get("issues", [])
    if isinstance(issues, list) and issues:
        lines.append("")
        lines.append("依赖问题：")
        for issue in issues:
            if isinstance(issue, dict):
                lines.append(f"- {issue.get('name', '未知任务')}：{issue.get('reason', '')}；建议：{issue.get('suggestion', '')}")
    else:
        lines.append("")
        lines.append("依赖检查：通过。")
    return "\n".join(lines)


def _format_dependency_wave_tasks(tasks: list[object]) -> str:
    if not tasks:
        return "无"
    labels: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        dependencies = task.get("known_depends_on", [])
        dependency_label = "无" if not dependencies else "、".join(str(dependency) for dependency in dependencies)
        labels.append(f"{task.get('name', '未知任务')}（依赖：{dependency_label}）")
    return "；".join(labels) if labels else "无"


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
    if suffix == ".csv":
        return _load_csv_tasks(input_path)
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
    inspect_path = _string_value(item.get("inspect_path") or item.get("path") or item.get("target_path"))
    depends_on = _dependency_names(item.get("depends_on", item.get("dependencies")))
    return TaskSpec(name=name, description=description, fields=fields, inspect_path=inspect_path, depends_on=depends_on)



def _load_csv_tasks(input_path: Path) -> list[TaskSpec]:
    with input_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError("CSV 文件缺少表头")
        return [_task_from_csv_row(row, index) for index, row in enumerate(reader, start=1)]

def _task_from_csv_row(row: dict[str, str | None], index: int) -> TaskSpec:
    name = _string_value(row.get("name")) or _default_task_name(index)
    description = _string_value(row.get("description"))
    fields = {key: _string_value(row.get(key)) for key in ELEMENT_ORDER}
    inspect_path = _string_value(row.get("inspect_path") or row.get("path") or row.get("target_path"))
    depends_on = _dependency_names(row.get("depends_on") or row.get("dependencies"))
    return TaskSpec(
        name=name,
        description=description,
        fields=_remove_empty_fields(fields),
        inspect_path=inspect_path,
        depends_on=depends_on,
    )


def _dependency_names(raw_dependencies: Any) -> list[str]:
    if raw_dependencies is None:
        return []
    if isinstance(raw_dependencies, list):
        return _dedupe_dependency_names(_string_value(item) for item in raw_dependencies)
    if isinstance(raw_dependencies, str):
        return _dedupe_dependency_names(DEPENDENCY_SPLIT_PATTERN.split(raw_dependencies))
    return _dedupe_dependency_names([_string_value(raw_dependencies)])


def _dedupe_dependency_names(raw_names: Any) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        name = _string_value(raw_name)
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


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
    if "必须是对象" in reason:
        return "把该任务改成包含 name、description、fields 的对象。"
    if "不支持的输入格式" in reason:
        return f"改用 {'、'.join(SUPPORTED_SUFFIXES)} 输入文件；JSON/CSV 之外的格式已在清理中移除。"
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
    output_lint_report: dict[str, object] | None = None,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "tasks": [_task_report(output, output_dir, output_file) for output in outputs],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    if output_lint_report is not None:
        report["output_lint"] = output_lint_report
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_task_list_report(
    tasks: list[TaskSpec],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "tasks": [_task_list_report(task) for task in tasks],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_dependency_plan_report(
    plan: dict[str, object],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "dependency_plan": plan,
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_batch_redaction_report(
    redaction_report: dict[str, object],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "redaction": redaction_report,
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_batch_field_lint_report(
    lint_report: dict[str, object],
    skipped_tasks: list[SkippedTask],
    stats: BatchStats,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "field_lint": lint_report,
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _task_list_report(task: TaskSpec) -> dict[str, object]:
    report: dict[str, object] = {"name": task.name, "description": task.description, "depends_on": task.depends_on}
    if task.inspect_path:
        report["inspect_path"] = task.inspect_path
    return report


def _task_report(
    output: TaskOutput,
    output_dir: str | None,
    output_file: str | None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "name": output.task_name,
        "file_slug": output.file_slug,
        "output_path": _report_output_path(output, output_dir, output_file),
        "present": output.present_keys,
        "missing_before_defaults": output.missing_before_defaults,
        "defaulted": output.defaulted_keys,
    }
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
