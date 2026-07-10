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
    check_redaction,
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
FALLBACK_HINTS: dict[str, tuple[str, ...]] = {
    "verification": ("验证", "运行", "执行", "确认", "检查", "通过", "跑测试"),
    "constraints": ("不改", "不修改", "不改变", "不引入", "禁止", "不得", "保持", "兼容"),
    "boundaries": ("边界", "范围", "仅", "只", "目录", "排除", "src/", "tests/"),
    "iteration": ("迭代", "每个", "每次", "逐个", "commit", "提交", "预期"),
    "blocked": ("受阻", "阻塞", "停下", "问人", "问我", "跳过", "无法", "缺少"),
}



@dataclass(frozen=True)
class TaskSpec:
    name: str
    description: str
    fields: dict[str, str]
    depends_on: list[str] = field(default_factory=list)
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
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"读取输入失败：{error}", file=sys.stderr)
        return 1
    if args.redaction_check:
        return _run_batch_redaction_check_mode(tasks, args, start_time)
    if args.lint_fields:
        return _run_lint_fields_mode(tasks, args, start_time)
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
        )
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
    parser.add_argument("--dedupe", action="store_true", help="按任务名和描述跳过重复任务。")
    parser.add_argument("--fail-on-skipped", action="store_true", help="有跳过任务时以退出码 1 结束，适合 CI 门禁。")
    parser.add_argument("--summary-only", action="store_true", help="抑制任务正文 stdout，仅输出最终摘要。")
    parser.add_argument("--redaction-check", action="store_true", help="批量检查任务名称、描述和字段值中的 token、邮箱、URL 等敏感信息。")
    parser.add_argument("--dry-run", action="store_true", help="只分析要素完整度，不生成指令。")
    parser.add_argument("--check", action="store_true", help="校验输入任务文件，等价于 --dry-run --strict --summary-only --fail-on-skipped。")
    parser.add_argument("--lint-fields", action="store_true", help="批量检查任务 6 要素字段的语义质量，不生成 /goal。")
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
    depends_on = _dependency_names(item.get("depends_on", item.get("dependencies")))
    return TaskSpec(name=name, description=description, fields=fields, depends_on=depends_on)



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
    depends_on = _dependency_names(row.get("depends_on") or row.get("dependencies"))
    return TaskSpec(name=name, description=description, fields=_remove_empty_fields(fields), depends_on=depends_on)


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
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "tasks": [_task_report(output, output_dir, output_file) for output in outputs],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
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
    return {"name": task.name, "description": task.description, "depends_on": task.depends_on}


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
