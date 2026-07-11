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
    FIELD_ALIASES,
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
RISK_LEVEL_ORDER: dict[str, int] = {"low": 1, "medium": 2, "high": 3}
FALLBACK_HINTS: dict[str, tuple[str, ...]] = {
    "verification": ("验证", "运行", "执行", "确认", "检查", "通过", "跑测试"),
    "constraints": ("不改", "不修改", "不改变", "不引入", "禁止", "不得", "保持", "兼容"),
    "boundaries": ("边界", "范围", "仅", "只", "目录", "排除", "src/", "tests/"),
    "iteration": ("迭代", "每个", "每次", "逐个", "commit", "提交", "预期"),
    "blocked": ("受阻", "阻塞", "停下", "问人", "问我", "跳过", "无法", "缺少"),
}
SUPPLEMENT_TEXT_FIELDS: tuple[str, ...] = ("supplement", "answer", "response", "text", "description")
TASK_SCHEMA_PATH_KEYS: tuple[str, ...] = ("path", "inspect_path", "target_path")
TASK_SCHEMA_DEPENDENCY_KEYS: tuple[str, ...] = ("depends_on", "dependencies")
TASK_SCHEMA_ALLOWED_KEYS: tuple[str, ...] = (
    "name",
    "description",
    "fields",
    *TASK_SCHEMA_PATH_KEYS,
    *TASK_SCHEMA_DEPENDENCY_KEYS,
)
CSV_TASK_SCHEMA_ALLOWED_HEADERS: tuple[str, ...] = (
    "name",
    "description",
    *TASK_SCHEMA_PATH_KEYS,
    *TASK_SCHEMA_DEPENDENCY_KEYS,
    *ELEMENT_ORDER,
)
TASK_SCHEMA_SCALAR_KEYS: tuple[str, ...] = ("name", "description", *TASK_SCHEMA_PATH_KEYS)


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
class TaskSchemaIssue:
    task_index: int
    field: str
    severity: str
    message: str
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
        min_lint_score = _min_lint_score_from_args(args)
        max_defaulted_fields = _max_defaulted_fields_from_args(args)
        min_description_length = _min_description_length_from_args(args)
        max_task_count = _max_task_count_from_args(args)
        require_non_empty = _require_non_empty_from_args(args)
        _require_output_target_from_args(args)
        task_risk_threshold = _task_risk_threshold_from_args(args)
        required_explicit_fields = _required_explicit_fields_from_args(args)
        forbidden_default_fields = _forbidden_default_fields_from_args(args)
        require_task_path = _require_task_path_from_args(args)
        require_existing_task_path = _require_existing_task_path_from_args(args)
        allowed_path_roots = _allowed_path_roots_from_args(args)
        require_unique_task_names = _require_unique_task_names_from_args(args)
        required_name_pattern = _required_name_pattern_from_args(args)
        require_valid_dependencies = _require_valid_dependencies_from_args(args)
        require_dependency_order = _require_dependency_order_from_args(args)
        no_overwrite = _no_overwrite_from_args(args)
        if args.fail_on_high_risk and not args.profile_tasks:
            print("--fail-on-high-risk 仅适用于 --profile-tasks。", file=sys.stderr)
            return 1
        if args.fail_on_risk_level and not args.profile_tasks:
            print("--fail-on-risk-level 仅适用于 --profile-tasks。", file=sys.stderr)
            return 1
        if args.lint_defaults_json:
            if args.lint_output:
                print("--lint-defaults-json 不生成 /goal，请勿与 --lint-output 同用。", file=sys.stderr)
                return 1
            if min_lint_score is not None:
                print("--min-lint-score 仅适用于 --lint-fields 或 --lint-output。", file=sys.stderr)
                return 1
            try:
                return _run_lint_defaults_mode(args, start_time)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                print(f"读取输入失败：{error}", file=sys.stderr)
                return 1
        if args.lint_task_schema:
            try:
                return _run_task_schema_lint_mode(args, start_time)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                print(f"读取输入失败：{error}", file=sys.stderr)
                return 1
    except ValueError as error:
        print(f"参数错误：{error}", file=sys.stderr)
        return 1
    try:
        tasks = _load_tasks_from_input(_input_value_from_args(args))
        tasks = _filter_tasks(tasks, args.filter)
        tasks = _sort_tasks(tasks, args.sort_by)
        tasks = _limit_tasks(tasks, args.limit)
        if require_non_empty and not tasks:
            print(_require_non_empty_error(), file=sys.stderr)
            return 1
        if max_task_count is not None and len(tasks) > max_task_count:
            print(_max_task_count_error(len(tasks), max_task_count), file=sys.stderr)
            return 1
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
        return _run_lint_fields_mode(tasks, args, start_time, min_lint_score)
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
    outputs, skipped_tasks = _process_tasks(
        tasks,
        dry_run,
        strict,
        args.dedupe,
        default_values,
        args.verbose,
        max_defaulted_fields,
        min_description_length,
        task_risk_threshold,
        required_explicit_fields,
        forbidden_default_fields,
        require_task_path,
        require_existing_task_path,
        allowed_path_roots,
        require_unique_task_names,
        required_name_pattern,
        require_valid_dependencies,
        require_dependency_order,
    )
    try:
        _ensure_no_overwrite_targets(
            outputs,
            args.output_dir,
            args.output_file,
            args.report_json,
            no_overwrite,
        )
    except ValueError as error:
        print(f"输出保护失败：{error}", file=sys.stderr)
        return 1
    output_lint_report = None
    if args.lint_output:
        if dry_run:
            print("--lint-output 需要真实生成 /goal，请勿与 --dry-run 或 --check 同用。", file=sys.stderr)
            return 1
        output_lint_report = _build_output_lint_report(outputs, min_lint_score)
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
    if max_defaulted_fields is not None and _has_defaulted_limit_skips(skipped_tasks):
        return 1
    if min_description_length is not None and _has_description_length_skips(skipped_tasks):
        return 1
    if task_risk_threshold and _has_task_risk_gate_skips(skipped_tasks):
        return 1
    if required_explicit_fields and _has_explicit_field_skips(skipped_tasks):
        return 1
    if forbidden_default_fields and _has_forbidden_default_skips(skipped_tasks):
        return 1
    if require_task_path and _has_required_path_skips(skipped_tasks):
        return 1
    if require_existing_task_path and (_has_required_path_skips(skipped_tasks) or _has_missing_existing_path_skips(skipped_tasks)):
        return 1
    if allowed_path_roots and (_has_required_path_skips(skipped_tasks) or _has_allowed_path_root_skips(skipped_tasks)):
        return 1
    if require_unique_task_names and _has_duplicate_name_skips(skipped_tasks):
        return 1
    if required_name_pattern and _has_name_pattern_skips(skipped_tasks):
        return 1
    if require_valid_dependencies and _has_dependency_gate_skips(skipped_tasks):
        return 1
    if require_dependency_order and _has_dependency_order_skips(skipped_tasks):
        return 1
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
    parser.add_argument("--lint-defaults-json", help="检查团队默认值 JSON 合并后的 6 要素语义质量，不需要任务输入。")
    parser.add_argument("--lint-task-schema", action="store_true", help="检查批量 JSON/CSV 任务清单字段结构、未知字段和别名冲突，不生成 /goal。")
    parser.add_argument("--report-json", help="把批量处理结果、缺失要素和跳过原因写入 JSON 报告。")
    parser.add_argument("--filter", help="按正则筛选任务名或描述，只处理匹配的任务。")
    parser.add_argument("--sort-by", choices=("input", "name"), default="input", help="批量任务输出顺序，默认保持输入顺序。")
    parser.add_argument("--limit", type=int, help="只处理前 N 个任务，适合大清单试跑。")
    parser.add_argument("--max-task-count", type=int, help="任务读取、筛选和 limit 后最多允许处理的任务数量，超限时失败而不截断。")
    parser.add_argument("--require-non-empty", action="store_true", help="任务读取、筛选和 limit 后必须至少保留 1 个任务，否则失败。")
    parser.add_argument("--list-tasks", action="store_true", help="只预览将要处理的任务名称，不生成 /goal。")
    parser.add_argument("--plan-dependencies", action="store_true", help="根据 depends_on/dependencies 字段输出批量任务依赖执行计划。")
    parser.add_argument("--dependency-order", action="store_true", help="按 depends_on/dependencies 拓扑顺序处理批量任务。")
    parser.add_argument("--dedupe", action="store_true", help="按任务名和描述跳过重复任务。")
    parser.add_argument("--fail-on-skipped", action="store_true", help="有跳过任务时以退出码 1 结束，适合 CI 门禁。")
    parser.add_argument("--fail-on-high-risk", action="store_true", help="配合 --profile-tasks，高风险任务存在时以退出码 1 结束。")
    parser.add_argument(
        "--fail-on-risk-level",
        choices=tuple(RISK_LEVEL_ORDER),
        help="配合 --profile-tasks，存在指定风险等级及以上任务时以退出码 1 结束。",
    )
    parser.add_argument("--summary-only", action="store_true", help="抑制任务正文 stdout，仅输出最终摘要。")
    parser.add_argument("--redaction-check", action="store_true", help="批量检查任务名称、描述和字段值中的 token、邮箱、URL 等敏感信息。")
    parser.add_argument("--profile-tasks", action="store_true", help="批量识别任务类型、复杂度、风险和 6 要素缺口。")
    parser.add_argument("--questions", action="store_true", help="按任务生成可直接发送的批量缺失要素追问文案。")
    parser.add_argument("--merge-supplements", help="读取按任务名组织的补充回答 JSON/CSV，合并回批量任务 fields 并输出新的任务 JSON。")
    parser.add_argument("--dry-run", action="store_true", help="只分析要素完整度，不生成指令。")
    parser.add_argument("--lint-output", action="store_true", help="真实生成 /goal 后，在写出交付物前检查每个最终文本的结构和语义质量。")
    parser.add_argument(
        "--min-lint-score",
        type=int,
        help="配合 --lint-fields 或 --lint-output，任务得分低于该阈值时失败（0-100）。",
    )
    parser.add_argument(
        "--max-defaulted-fields",
        type=int,
        help="真实生成或 dry-run 时允许每个任务最多默认填充的 6 要素数量（0-6）。",
    )
    parser.add_argument(
        "--min-description-length",
        type=int,
        help="真实生成、dry-run、check 或 lint-output 时要求每个任务 description 至少达到指定字符数。",
    )
    parser.add_argument(
        "--fail-on-task-risk-level",
        choices=tuple(RISK_LEVEL_ORDER),
        help="真实生成、dry-run、check 或 lint-output 时存在指定风险等级及以上任务则跳过并失败。",
    )
    parser.add_argument(
        "--require-explicit-fields",
        help="真实生成、dry-run、check 或 lint-output 时要求指定 6 要素必须由 fields 或 description 标签显式提供，多个字段用逗号分隔。",
    )
    parser.add_argument(
        "--forbid-default-fields",
        help="真实生成、dry-run、check 或 lint-output 时禁止指定 6 要素使用默认值兜底，多个字段用逗号分隔。",
    )
    parser.add_argument(
        "--require-task-path",
        action="store_true",
        help="真实生成、dry-run、check 或 lint-output 时要求每个任务提供 path/inspect_path/target_path。",
    )
    parser.add_argument(
        "--require-existing-task-path",
        action="store_true",
        help="真实生成、dry-run、check 或 lint-output 时要求任务 path/inspect_path/target_path 在本地存在。",
    )
    parser.add_argument(
        "--allowed-path-roots",
        help="真实生成、dry-run、check 或 lint-output 时要求任务路径位于指定根目录内，多个根目录用逗号分隔。",
    )
    parser.add_argument(
        "--require-unique-task-names",
        action="store_true",
        help="真实生成、dry-run、check 或 lint-output 时要求每个任务 name 唯一。",
    )
    parser.add_argument(
        "--require-name-pattern",
        help="真实生成、dry-run、check 或 lint-output 时要求每个任务 name 匹配该正则表达式。",
    )
    parser.add_argument(
        "--require-valid-dependencies",
        action="store_true",
        help="真实生成、dry-run、check 或 lint-output 时要求 depends_on/dependencies 依赖图合法但不改变任务顺序。",
    )
    parser.add_argument(
        "--require-dependency-order",
        action="store_true",
        help="真实生成、dry-run、check 或 lint-output 时要求依赖任务在当前任务之前出现但不重排任务。",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="写入 --output-file、--output-dir 目标文件或 --report-json 前要求目标不存在，避免覆盖已有批量产物。",
    )
    parser.add_argument(
        "--require-output-target",
        action="store_true",
        help="真实批量生成或 lint-output 时必须指定 --output-file 或 --output-dir，避免交付物只输出到 stdout。",
    )
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


def _min_lint_score_from_args(args: argparse.Namespace) -> int | None:
    min_lint_score = args.min_lint_score
    if min_lint_score is None:
        return None
    if min_lint_score < 0 or min_lint_score > 100:
        raise ValueError("--min-lint-score 必须是 0 到 100 之间的整数")
    if not (args.lint_fields or args.lint_output):
        raise ValueError("--min-lint-score 仅适用于 --lint-fields 或 --lint-output")
    return min_lint_score


def _max_defaulted_fields_from_args(args: argparse.Namespace) -> int | None:
    max_defaulted_fields = args.max_defaulted_fields
    if max_defaulted_fields is None:
        return None
    if max_defaulted_fields < 0 or max_defaulted_fields > len(ELEMENT_ORDER):
        raise ValueError(f"--max-defaulted-fields 必须是 0 到 {len(ELEMENT_ORDER)} 之间的整数")
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--max-defaulted-fields 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    return max_defaulted_fields


def _min_description_length_from_args(args: argparse.Namespace) -> int | None:
    min_description_length = args.min_description_length
    if min_description_length is None:
        return None
    if min_description_length < 1:
        raise ValueError("--min-description-length 必须是大于 0 的整数")
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--min-description-length 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    return min_description_length


def _task_risk_threshold_from_args(args: argparse.Namespace) -> str:
    task_risk_threshold = args.fail_on_task_risk_level or ""
    if not task_risk_threshold:
        return ""
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--fail-on-task-risk-level 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    return task_risk_threshold


def _max_task_count_from_args(args: argparse.Namespace) -> int | None:
    max_task_count = args.max_task_count
    if max_task_count is None:
        return None
    if max_task_count < 0:
        raise ValueError("--max-task-count 必须是大于或等于 0 的整数")
    if args.lint_defaults_json or args.lint_task_schema:
        raise ValueError("--max-task-count 需要读取批量任务清单，不适用于 --lint-defaults-json 或 --lint-task-schema")
    return max_task_count


def _require_non_empty_from_args(args: argparse.Namespace) -> bool:
    if not args.require_non_empty:
        return False
    if args.lint_defaults_json or args.lint_task_schema:
        raise ValueError("--require-non-empty 需要读取批量任务清单，不适用于 --lint-defaults-json 或 --lint-task-schema")
    if args.max_task_count == 0:
        raise ValueError("--require-non-empty 不能与 --max-task-count 0 同用")
    return True


def _required_explicit_fields_from_args(args: argparse.Namespace) -> list[str]:
    required_fields_value = args.require_explicit_fields
    if not required_fields_value:
        return []
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--require-explicit-fields 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    return _field_list_from_value(required_fields_value, "--require-explicit-fields")


def _forbidden_default_fields_from_args(args: argparse.Namespace) -> list[str]:
    forbidden_fields_value = args.forbid_default_fields
    if not forbidden_fields_value:
        return []
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--forbid-default-fields 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    return _field_list_from_value(forbidden_fields_value, "--forbid-default-fields")


def _require_task_path_from_args(args: argparse.Namespace) -> bool:
    if not args.require_task_path:
        return False
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--require-task-path 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    return True


def _require_existing_task_path_from_args(args: argparse.Namespace) -> bool:
    if not args.require_existing_task_path:
        return False
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--require-existing-task-path 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    return True


def _allowed_path_roots_from_args(args: argparse.Namespace) -> list[Path]:
    allowed_roots_value = args.allowed_path_roots
    if allowed_roots_value is None:
        return []
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--allowed-path-roots 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    roots: list[Path] = []
    seen: set[str] = set()
    for raw_root in DEPENDENCY_SPLIT_PATTERN.split(allowed_roots_value):
        root_value = raw_root.strip()
        if not root_value:
            continue
        root = _resolved_path(root_value)
        root_key = str(root)
        if root_key in seen:
            continue
        roots.append(root)
        seen.add(root_key)
    if not roots:
        raise ValueError("--allowed-path-roots 必须包含至少一个路径根目录")
    return roots


def _require_unique_task_names_from_args(args: argparse.Namespace) -> bool:
    if not args.require_unique_task_names:
        return False
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--require-unique-task-names 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    return True


def _required_name_pattern_from_args(args: argparse.Namespace) -> re.Pattern[str] | None:
    pattern_value = args.require_name_pattern
    if pattern_value is None:
        return None
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--require-name-pattern 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    if not pattern_value:
        raise ValueError("--require-name-pattern 必须提供非空正则表达式")
    try:
        return re.compile(pattern_value)
    except re.error as error:
        raise ValueError(f"--require-name-pattern 正则无效：{error}") from error


def _require_valid_dependencies_from_args(args: argparse.Namespace) -> bool:
    if not args.require_valid_dependencies:
        return False
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--require-valid-dependencies 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    return True


def _require_dependency_order_from_args(args: argparse.Namespace) -> bool:
    if not args.require_dependency_order:
        return False
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--require-dependency-order 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    if args.dependency_order:
        raise ValueError("--require-dependency-order 检查当前顺序，请勿与会重排任务的 --dependency-order 同用")
    return True


def _no_overwrite_from_args(args: argparse.Namespace) -> bool:
    if not args.no_overwrite:
        return False
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
        or args.questions
        or args.profile_tasks
        or args.redaction_check
        or args.lint_fields
        or args.inspect_paths
        or args.enrich_from_paths
        or args.plan_dependencies
        or args.list_tasks
    ):
        raise ValueError("--no-overwrite 仅适用于真实批量生成、--dry-run、--check 或 --lint-output")
    return True


def _require_output_target_from_args(args: argparse.Namespace) -> bool:
    if not args.require_output_target:
        return False
    if (
        args.lint_defaults_json
        or args.lint_task_schema
        or args.merge_supplements
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
        raise ValueError("--require-output-target 仅适用于真实批量生成或 --lint-output，不能与分析、dry-run 或 check 模式同用")
    if not (args.output_file or args.output_dir):
        raise ValueError("--require-output-target 要求指定 --output-file 或 --output-dir，避免真实批量生成只输出到 stdout")
    return True


def _field_list_from_value(field_list_value: str, option_name: str) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()
    unknown_tokens: list[str] = []
    for raw_token in DEPENDENCY_SPLIT_PATTERN.split(field_list_value):
        token = raw_token.strip()
        if not token:
            continue
        field = _explicit_field_key(token)
        if not field:
            unknown_tokens.append(token)
            continue
        if field not in seen:
            fields.append(field)
            seen.add(field)
    if unknown_tokens:
        supported = "、".join(ELEMENT_ORDER)
        raise ValueError(f"{option_name} 包含未知字段：{'、'.join(unknown_tokens)}；可选：{supported}")
    if not fields:
        raise ValueError(f"{option_name} 必须包含至少一个 6 要素字段")
    return fields


def _explicit_field_key(token: str) -> str:
    normalized_token = token.strip().lower()
    for key in ELEMENT_ORDER:
        if normalized_token == key:
            return key
        if normalized_token == ELEMENT_LABELS[key].lower():
            return key
        english_label = ELEMENT_LABELS[key].split("（", 1)[0].strip().lower()
        if normalized_token == english_label:
            return key
        aliases = FIELD_ALIASES.get(key, ())
        if any(normalized_token == alias.lower() for alias in aliases):
            return key
    return ""


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


def _max_task_count_error(task_count: int, max_task_count: int) -> str:
    return (
        f"任务数量超限：当前将处理 {task_count} 个任务，超过 --max-task-count {max_task_count}；"
        "请缩小输入清单、调整 --filter，或确认后使用 --limit 明确截断。"
    )


def _require_non_empty_error() -> str:
    return "任务集为空：--require-non-empty 要求筛选后至少保留 1 个任务；请检查输入清单、--filter 或 --limit。"


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


def _run_lint_defaults_mode(args: argparse.Namespace, start_time: float) -> int:
    if args.output_dir:
        print("--lint-defaults-json 不支持 --output-dir，请使用 --output-file 写入检查报告。", file=sys.stderr)
        return 1
    lint_report = _lint_defaults_json_file(args.lint_defaults_json)
    lint_text = json.dumps(lint_report, ensure_ascii=False, indent=2)
    summary_only = args.summary_only or args.check
    if args.output_file:
        _write_text_file(lint_text, Path(args.output_file))
    elif not summary_only:
        print(lint_text)
    elapsed_seconds = time.perf_counter() - start_time
    if args.report_json:
        _write_defaults_lint_report(lint_report, elapsed_seconds, Path(args.report_json))
    print(_format_defaults_lint_summary(lint_report, elapsed_seconds))
    return 0 if lint_report["passed"] else 1


def _run_task_schema_lint_mode(args: argparse.Namespace, start_time: float) -> int:
    if args.output_dir:
        print("--lint-task-schema 不支持 --output-dir，请使用 --output-file 写入检查报告。", file=sys.stderr)
        return 1
    if args.lint_output:
        print("--lint-task-schema 不生成 /goal，请勿与 --lint-output 同用。", file=sys.stderr)
        return 1
    schema_report = _lint_task_schema_file(Path(_input_value_from_args(args)))
    schema_text = _format_task_schema_report(schema_report)
    summary_only = args.summary_only or args.check
    if args.output_file:
        _write_text_file(schema_text, Path(args.output_file))
    elif not summary_only:
        print(schema_text)
    elapsed_seconds = time.perf_counter() - start_time
    if args.report_json:
        _write_task_schema_lint_report(schema_report, elapsed_seconds, Path(args.report_json))
    print(_format_task_schema_summary(schema_report, elapsed_seconds))
    return 0 if schema_report["valid"] else 1


def _lint_task_schema_file(input_path: Path) -> dict[str, object]:
    suffix = input_path.suffix.lower()
    if suffix == ".json":
        return _lint_json_task_schema(input_path)
    if suffix == ".csv":
        return _lint_csv_task_schema(input_path)
    supported = "、".join(SUPPORTED_SUFFIXES)
    raise ValueError(f"--lint-task-schema 不支持的输入格式：{suffix}，仅支持 {supported}")


def _lint_json_task_schema(input_path: Path) -> dict[str, object]:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        issue = TaskSchemaIssue(
            0,
            "root",
            "high",
            "JSON 顶层必须是任务数组",
            "把输入改成由任务对象组成的数组，例如 [{\"name\": \"任务\", \"description\": \"...\"}]。",
        )
        return _task_schema_report(input_path, "json", 0, [issue])
    issues: list[TaskSchemaIssue] = []
    if not data:
        issues.append(
            TaskSchemaIssue(
                0,
                "root",
                "high",
                "JSON 任务数组为空",
                "至少提供一个包含 name、description 和可选 fields/path/depends_on 的任务对象。",
            )
        )
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            issues.append(
                TaskSchemaIssue(
                    index,
                    "task",
                    "high",
                    "JSON 任务必须是对象",
                    "把该条任务改成包含 name、description、fields 等字段的对象。",
                )
            )
            continue
        issues.extend(_json_task_schema_issues(index, item))
    return _task_schema_report(input_path, "json", len(data), issues)


def _json_task_schema_issues(index: int, item: dict[str, Any]) -> list[TaskSchemaIssue]:
    issues: list[TaskSchemaIssue] = []
    issues.extend(_unknown_json_task_field_issues(index, item))
    issues.extend(_json_scalar_field_issues(index, item))
    issues.extend(_json_fields_object_issues(index, item.get("fields")) if "fields" in item else [])
    issues.extend(_json_dependency_field_issues(index, item))
    issues.extend(_alias_conflict_issues(index, item, TASK_SCHEMA_PATH_KEYS, "任务路径别名"))
    issues.extend(_alias_conflict_issues(index, item, TASK_SCHEMA_DEPENDENCY_KEYS, "依赖别名"))
    if not _string_value(item.get("description")):
        issues.append(
            TaskSchemaIssue(
                index,
                "description",
                "high",
                "缺少或为空 description",
                "为该任务补充 description；主生成流程需要 description 才能分析和生成 /goal。",
            )
        )
    return issues


def _unknown_json_task_field_issues(index: int, item: dict[str, Any]) -> list[TaskSchemaIssue]:
    allowed = set(TASK_SCHEMA_ALLOWED_KEYS)
    supported = "、".join(TASK_SCHEMA_ALLOWED_KEYS)
    return [
        TaskSchemaIssue(
            index,
            str(key),
            "high",
            f"未知任务字段：{key}",
            f"确认是否拼写错误；批量 JSON 任务仅支持：{supported}。",
        )
        for key in sorted(str(raw_key) for raw_key in item if str(raw_key) not in allowed)
    ]


def _json_scalar_field_issues(index: int, item: dict[str, Any]) -> list[TaskSchemaIssue]:
    issues: list[TaskSchemaIssue] = []
    for key in TASK_SCHEMA_SCALAR_KEYS:
        if key in item and not _is_schema_scalar_value(item.get(key)):
            issues.append(
                TaskSchemaIssue(
                    index,
                    key,
                    "high",
                    f"{key} 必须是字符串或可转成字符串的标量",
                    f"把 {key} 改成单个字符串；复杂对象请放入 description 或 fields 中的文本。",
                )
            )
    return issues


def _json_fields_object_issues(index: int, raw_fields: Any) -> list[TaskSchemaIssue]:
    if not isinstance(raw_fields, dict):
        return [
            TaskSchemaIssue(
                index,
                "fields",
                "high",
                "fields 必须是对象",
                "把 fields 改成包含 outcome、verification、constraints、boundaries、iteration、blocked 的对象。",
            )
        ]
    issues: list[TaskSchemaIssue] = []
    supported = "、".join(ELEMENT_ORDER)
    for raw_key, value in raw_fields.items():
        key = str(raw_key)
        field_name = f"fields.{key}"
        if key not in ELEMENT_ORDER:
            issues.append(
                TaskSchemaIssue(
                    index,
                    field_name,
                    "high",
                    f"未知 6 要素字段：{key}",
                    f"确认字段名是否拼写错误；fields 仅支持：{supported}。",
                )
            )
            continue
        if not _is_schema_scalar_value(value):
            issues.append(
                TaskSchemaIssue(
                    index,
                    field_name,
                    "high",
                    f"{field_name} 必须是字符串或可转成字符串的标量",
                    "把该 6 要素值改成一段可直接写入 /goal 的文本。",
                )
            )
    return issues


def _json_dependency_field_issues(index: int, item: dict[str, Any]) -> list[TaskSchemaIssue]:
    issues: list[TaskSchemaIssue] = []
    for key in TASK_SCHEMA_DEPENDENCY_KEYS:
        if key in item and not _is_schema_dependency_value(item.get(key)):
            issues.append(
                TaskSchemaIssue(
                    index,
                    key,
                    "high",
                    f"{key} 必须是字符串、标量或标量数组",
                    f"把 {key} 改成任务名字符串、用分隔符连接的字符串，或任务名数组。",
                )
            )
    return issues


def _lint_csv_task_schema(input_path: Path) -> dict[str, object]:
    issues: list[TaskSchemaIssue] = []
    with input_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            issue = TaskSchemaIssue(0, "header", "high", "CSV 文件缺少表头", "补充 name、description 等表头后重试。")
            return _task_schema_report(input_path, "csv", 0, [issue])
        issues.extend(_csv_header_schema_issues(fieldnames))
        task_count = 0
        for task_count, row in enumerate(reader, start=1):
            issues.extend(_csv_row_schema_issues(task_count, row))
    if task_count == 0:
        issues.append(
            TaskSchemaIssue(
                0,
                "rows",
                "high",
                "CSV 文件没有任务行",
                "至少提供一行任务数据，并填写 description。",
            )
        )
    return _task_schema_report(input_path, "csv", task_count, issues)


def _csv_header_schema_issues(fieldnames: list[str]) -> list[TaskSchemaIssue]:
    issues: list[TaskSchemaIssue] = []
    seen: set[str] = set()
    duplicates: set[str] = set()
    allowed = set(CSV_TASK_SCHEMA_ALLOWED_HEADERS)
    supported = "、".join(CSV_TASK_SCHEMA_ALLOWED_HEADERS)
    for raw_fieldname in fieldnames:
        fieldname = _string_value(raw_fieldname)
        if not fieldname:
            issues.append(
                TaskSchemaIssue(
                    0,
                    "header",
                    "high",
                    "CSV 存在空表头",
                    "删除空列，或把表头改成受支持字段名。",
                )
            )
            continue
        if fieldname in seen:
            duplicates.add(fieldname)
        seen.add(fieldname)
        if fieldname not in allowed:
            issues.append(
                TaskSchemaIssue(
                    0,
                    fieldname,
                    "high",
                    f"未知 CSV 表头：{fieldname}",
                    f"确认是否拼写错误；CSV 仅支持：{supported}。",
                )
            )
    for fieldname in sorted(duplicates):
        issues.append(
            TaskSchemaIssue(
                0,
                fieldname,
                "high",
                f"CSV 表头重复：{fieldname}",
                "删除重复列，避免 csv.DictReader 覆盖同名字段值。",
            )
        )
    return issues


def _csv_row_schema_issues(index: int, row: dict[str, str | None]) -> list[TaskSchemaIssue]:
    issues: list[TaskSchemaIssue] = []
    if row.get(None):
        issues.append(
            TaskSchemaIssue(
                index,
                "row",
                "high",
                "CSV 行列数超过表头数量",
                "检查该行是否存在未转义逗号、缺失引号或多余列。",
            )
        )
    if not _string_value(row.get("description")):
        issues.append(
            TaskSchemaIssue(
                index,
                "description",
                "high",
                "缺少或为空 description",
                "为该任务补充 description；主生成流程需要 description 才能分析和生成 /goal。",
            )
        )
    issues.extend(_alias_conflict_issues(index, row, TASK_SCHEMA_PATH_KEYS, "任务路径别名"))
    issues.extend(_alias_conflict_issues(index, row, TASK_SCHEMA_DEPENDENCY_KEYS, "依赖别名"))
    return issues


def _alias_conflict_issues(
    index: int,
    item: dict[Any, Any],
    aliases: tuple[str, ...],
    label: str,
) -> list[TaskSchemaIssue]:
    values = {
        alias: _schema_alias_value(item.get(alias), aliases)
        for alias in aliases
        if alias in item and _schema_alias_value(item.get(alias), aliases)
    }
    if len(set(values.values())) <= 1:
        return []
    alias_text = "、".join(f"{alias}={value}" for alias, value in values.items())
    preferred = aliases[0]
    return [
        TaskSchemaIssue(
            index,
            "/".join(aliases),
            "high",
            f"{label}存在冲突：{alias_text}",
            f"只保留一个别名或确保这些别名取值一致；推荐统一使用 {preferred}。",
        )
    ]


def _schema_alias_value(value: Any, aliases: tuple[str, ...]) -> str:
    if aliases == TASK_SCHEMA_DEPENDENCY_KEYS:
        return "|".join(_dependency_names(value))
    if isinstance(value, list):
        return "|".join(_string_value(item) for item in value if _string_value(item))
    return _string_value(value)


def _is_schema_scalar_value(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _is_schema_dependency_value(value: Any) -> bool:
    if _is_schema_scalar_value(value):
        return True
    if isinstance(value, list):
        return all(_is_schema_scalar_value(item) for item in value)
    return False


def _task_schema_report(
    input_path: Path,
    source_format: str,
    task_count: int,
    issues: list[TaskSchemaIssue],
) -> dict[str, object]:
    failed_task_indexes = sorted({issue.task_index for issue in issues if issue.task_index > 0})
    return {
        "valid": not issues,
        "source": str(input_path),
        "format": source_format,
        "task_count": task_count,
        "passed_task_count": max(0, task_count - len(failed_task_indexes)),
        "failed_task_count": len(failed_task_indexes),
        "issue_count": len(issues),
        "issues": [_task_schema_issue_report(issue) for issue in issues],
        "summary": _task_schema_report_summary(task_count, issues, failed_task_indexes),
    }


def _task_schema_issue_report(issue: TaskSchemaIssue) -> dict[str, object]:
    return {
        "task_index": issue.task_index,
        "field": issue.field,
        "severity": issue.severity,
        "message": issue.message,
        "suggestion": issue.suggestion,
    }


def _task_schema_report_summary(
    task_count: int,
    issues: list[TaskSchemaIssue],
    failed_task_indexes: list[int],
) -> str:
    if not issues:
        return f"任务清单 Schema 检查通过：任务 {task_count} 个。"
    return (
        "任务清单 Schema 检查未通过："
        f"任务 {task_count} 个，问题 {len(issues)} 个，"
        f"涉及任务 {len(failed_task_indexes)} 个。"
    )


def _format_task_schema_report(schema_report: dict[str, object]) -> str:
    lines = ["批量任务清单 Schema 检查：", str(schema_report.get("summary", ""))]
    issues = schema_report.get("issues", [])
    if not isinstance(issues, list) or not issues:
        return "\n".join(lines)
    lines.append("")
    for issue in issues[:20]:
        if isinstance(issue, dict):
            lines.append(_format_task_schema_issue_line(issue))
    if len(issues) > 20:
        lines.append(f"- 其余 {len(issues) - 20} 个问题请查看 --report-json。")
    return "\n".join(lines)


def _format_task_schema_issue_line(issue: dict[str, object]) -> str:
    task_index = int(issue.get("task_index", 0))
    location = "文件级" if task_index <= 0 else f"第 {task_index} 个任务"
    return (
        f"- {location} / {issue.get('field', 'unknown')}："
        f"{issue.get('message', '')}；建议：{issue.get('suggestion', '')}"
    )


def _write_task_schema_lint_report(
    schema_report: dict[str, object],
    elapsed_seconds: float,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": schema_report.get("passed_task_count", 0),
        "skipped_count": schema_report.get("failed_task_count", 0),
        "elapsed_seconds": round(elapsed_seconds, 4),
        "task_schema_lint": schema_report,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _format_task_schema_summary(schema_report: dict[str, object], elapsed_seconds: float) -> str:
    return f"{schema_report.get('summary', '任务清单 Schema 检查完成')} 总耗时 {elapsed_seconds:.2f} 秒。"


def _lint_defaults_json_file(defaults_file: str) -> dict[str, object]:
    defaults_path = Path(defaults_file)
    data = json.loads(defaults_path.read_text(encoding="utf-8"))
    raw_defaults = data.get("fields", data) if isinstance(data, dict) else data
    overrides = _fields_from_mapping(raw_defaults)
    if not overrides:
        raise ValueError("--lint-defaults-json 必须包含至少一个 6 要素默认值")
    merged_fields = dict(INTERACTIVE_DEFAULTS)
    merged_fields.update(overrides)
    field_lint = lint_fields_json_data({"fields": merged_fields}, str(defaults_path))
    return {
        "passed": field_lint["passed"],
        "source": str(defaults_path),
        "override_count": len(overrides),
        "overridden_fields": [key for key in ELEMENT_ORDER if key in overrides],
        "overrides": {key: overrides[key] for key in ELEMENT_ORDER if key in overrides},
        "merged_fields": merged_fields,
        "field_lint": field_lint,
        "summary": _defaults_lint_summary(bool(field_lint["passed"]), len(overrides), field_lint),
    }


def _write_defaults_lint_report(
    lint_report: dict[str, object],
    elapsed_seconds: float,
    report_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": 1 if lint_report["passed"] else 0,
        "skipped_count": 0 if lint_report["passed"] else 1,
        "elapsed_seconds": round(elapsed_seconds, 4),
        "defaults_lint": lint_report,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _defaults_lint_summary(passed: bool, override_count: int, field_lint: dict[str, object]) -> str:
    score = field_lint.get("score", 0)
    issue_count = field_lint.get("issue_count", 0)
    if passed:
        return f"团队默认值语义质量检查通过：覆盖 {override_count} 个字段，得分 {score}。"
    return f"团队默认值语义质量检查未通过：覆盖 {override_count} 个字段，得分 {score}，问题 {issue_count} 个。"


def _format_defaults_lint_summary(lint_report: dict[str, object], elapsed_seconds: float) -> str:
    return f"{lint_report.get('summary', '团队默认值检查完成')} 总耗时 {elapsed_seconds:.2f} 秒。"


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
    risk_threshold = _profile_risk_threshold(args)
    profile_report = _build_batch_profile_report(profile_tasks, risk_threshold)
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
    risk_gate = profile_report.get("risk_gate", {})
    if isinstance(risk_gate, dict) and risk_gate.get("enabled") and not risk_gate.get("passed", True):
        return 1
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _profile_risk_threshold(args: argparse.Namespace) -> str:
    if args.fail_on_risk_level:
        return str(args.fail_on_risk_level)
    if args.fail_on_high_risk:
        return "high"
    return ""


def _build_batch_profile_report(tasks: list[TaskSpec], risk_threshold: str = "") -> dict[str, object]:
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
        "risk_gate": _batch_profile_risk_gate(profiled_reports, risk_threshold),
        "tasks": task_reports,
    }


def _batch_profile_risk_gate(task_reports: list[dict[str, object]], threshold: str) -> dict[str, object]:
    if not threshold:
        return {
            "enabled": False,
            "threshold": "",
            "failed_count": 0,
            "failed_tasks": [],
            "passed": True,
            "summary": "未启用风险阈值门禁。",
        }
    failed_tasks = [
        _risk_gate_task_entry(report)
        for report in task_reports
        if _risk_level_matches_threshold(str(report.get("risk_level", "unknown")), threshold)
    ]
    return {
        "enabled": True,
        "threshold": threshold,
        "failed_count": len(failed_tasks),
        "failed_tasks": failed_tasks,
        "passed": not failed_tasks,
        "summary": _risk_gate_summary(threshold, len(failed_tasks)),
    }


def _risk_gate_task_entry(task_report: dict[str, object]) -> dict[str, object]:
    return {
        "name": str(task_report.get("name", "未知任务")),
        "risk_level": str(task_report.get("risk_level", "unknown")),
        "risk_score": task_report.get("risk_score", 0),
    }


def _risk_level_matches_threshold(risk_level: str, threshold: str) -> bool:
    level_value = RISK_LEVEL_ORDER.get(risk_level)
    threshold_value = RISK_LEVEL_ORDER.get(threshold)
    if level_value is None or threshold_value is None:
        return False
    return level_value >= threshold_value


def _risk_gate_summary(threshold: str, failed_count: int) -> str:
    if failed_count:
        return f"风险阈值门禁未通过：存在 {failed_count} 个 {threshold} 及以上风险任务。"
    return f"风险阈值门禁通过：未发现 {threshold} 及以上风险任务。"


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
        _format_profile_risk_gate(profile_report.get("risk_gate", {})),
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


def _format_profile_risk_gate(risk_gate: object) -> str:
    if not isinstance(risk_gate, dict) or not risk_gate.get("enabled"):
        return "风险阈值门禁：未启用"
    threshold = risk_gate.get("threshold", "unknown")
    failed_count = risk_gate.get("failed_count", 0)
    status = "通过" if risk_gate.get("passed", True) else "未通过"
    return f"风险阈值门禁：阈值 {threshold} 及以上，命中 {failed_count} 个，{status}"


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
    risk_gate = profile_report.get("risk_gate", {})
    risk_gate_text = ""
    if isinstance(risk_gate, dict) and risk_gate.get("enabled"):
        risk_gate_text = (
            f"风险阈值 {risk_gate.get('threshold', 'unknown')} 及以上命中 "
            f"{risk_gate.get('failed_count', 0)} 个，"
        )
    return (
        "画像生成完成："
        f"已画像 {profile_report.get('profiled_count', 0)} 个，"
        f"输入错误 {profile_report.get('input_error_count', 0)} 个，"
        f"高风险 {profile_report.get('high_risk_count', 0)} 个，"
        f"{risk_gate_text}"
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


def _run_lint_fields_mode(
    tasks: list[TaskSpec],
    args: argparse.Namespace,
    start_time: float,
    min_lint_score: int | None = None,
) -> int:
    lint_tasks, skipped_tasks = _dedupe_preview_tasks(tasks, args.dedupe)
    lint_report = _build_batch_field_lint_report(lint_tasks, min_lint_score)
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


def _build_batch_field_lint_report(tasks: list[TaskSpec], min_lint_score: int | None = None) -> dict[str, object]:
    task_reports = [_apply_task_min_lint_score(_task_field_lint_report(task), min_lint_score) for task in tasks]
    failed_count = sum(1 for report in task_reports if not report["passed"])
    return {
        "valid": failed_count == 0,
        "task_count": len(task_reports),
        "passed_count": len(task_reports) - failed_count,
        "failed_count": failed_count,
        "score_gate": _batch_score_gate(task_reports, min_lint_score),
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


def _apply_task_min_lint_score(task_report: dict[str, object], min_lint_score: int | None) -> dict[str, object]:
    if min_lint_score is None:
        return task_report
    score = int(task_report.get("score", 0))
    score_gate = _task_score_gate(score, min_lint_score)
    gated_report = dict(task_report)
    gated_report["score_gate"] = score_gate
    if not score_gate["passed"]:
        gated_report["passed"] = False
    gated_report["summary"] = _append_score_gate_summary(str(task_report.get("summary", "")), score_gate)
    return gated_report


def _task_score_gate(score: int, min_lint_score: int) -> dict[str, object]:
    passed = score >= min_lint_score
    return {
        "enabled": True,
        "min_score": min_lint_score,
        "score": score,
        "passed": passed,
        "summary": _task_score_gate_summary(score, min_lint_score, passed),
    }


def _task_score_gate_summary(score: int, min_lint_score: int, passed: bool) -> str:
    if passed:
        return f"最低分门禁通过：得分 {score}，要求至少 {min_lint_score}。"
    return f"最低分门禁未通过：得分 {score}，要求至少 {min_lint_score}。"


def _append_score_gate_summary(summary: str, score_gate: dict[str, object]) -> str:
    gate_summary = str(score_gate.get("summary", ""))
    if not summary:
        return gate_summary
    return f"{summary} {gate_summary}"


def _batch_score_gate(task_reports: list[dict[str, object]], min_lint_score: int | None) -> dict[str, object]:
    if min_lint_score is None:
        return {
            "enabled": False,
            "min_score": None,
            "failed_count": 0,
            "failed_tasks": [],
            "passed": True,
            "summary": "未启用最低分门禁。",
        }
    failed_tasks = [
        {"name": str(report.get("name", "未知任务")), "score": int(report.get("score", 0))}
        for report in task_reports
        if not _task_score_gate_passed(report)
    ]
    return {
        "enabled": True,
        "min_score": min_lint_score,
        "failed_count": len(failed_tasks),
        "failed_tasks": failed_tasks,
        "passed": not failed_tasks,
        "summary": _batch_score_gate_summary(min_lint_score, len(failed_tasks)),
    }


def _task_score_gate_passed(task_report: dict[str, object]) -> bool:
    score_gate = task_report.get("score_gate", {})
    if not isinstance(score_gate, dict) or not score_gate.get("enabled"):
        return True
    return bool(score_gate.get("passed", True))


def _batch_score_gate_summary(min_lint_score: int, failed_count: int) -> str:
    if failed_count:
        return f"最低分门禁未通过：{failed_count} 个任务低于 {min_lint_score} 分。"
    return f"最低分门禁通过：所有任务均达到 {min_lint_score} 分。"


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
    score_gate = task_report.get("score_gate", {})
    if isinstance(score_gate, dict) and score_gate.get("enabled") and not score_gate.get("passed", True):
        lines.append(f"  - {score_gate.get('summary', '最低分门禁未通过')}")
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
    score_gate = lint_report.get("score_gate", {})
    score_gate_text = ""
    if isinstance(score_gate, dict) and score_gate.get("enabled"):
        score_gate_text = (
            f"最低分门禁命中 {score_gate.get('failed_count', 0)} 个，"
        )
    return (
        "字段语义质量检查完成："
        f"通过 {lint_report.get('passed_count', 0)} 个，"
        f"失败 {lint_report.get('failed_count', 0)} 个，"
        f"{score_gate_text}"
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


def _build_output_lint_report(outputs: list[TaskOutput], min_lint_score: int | None = None) -> dict[str, object]:
    task_reports = [_apply_task_min_lint_score(_output_lint_task_report(output), min_lint_score) for output in outputs]
    failed_count = sum(1 for report in task_reports if not report["passed"])
    return {
        "passed": failed_count == 0,
        "task_count": len(task_reports),
        "passed_count": len(task_reports) - failed_count,
        "failed_count": failed_count,
        "score_gate": _batch_score_gate(task_reports, min_lint_score),
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
    score_gate = task_report.get("score_gate", {})
    if isinstance(score_gate, dict) and score_gate.get("enabled") and not score_gate.get("passed", True):
        lines.append(f"  - {score_gate.get('summary', '最低分门禁未通过')}")
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
    score_gate = lint_report.get("score_gate", {})
    score_gate_text = ""
    if isinstance(score_gate, dict) and score_gate.get("enabled"):
        score_gate_text = f"最低分门禁命中 {score_gate.get('failed_count', 0)} 个，"
    return (
        "输出 /goal 自检完成："
        f"通过 {lint_report.get('passed_count', 0)} 个，"
        f"{score_gate_text}"
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
    max_defaulted_fields: int | None = None,
    min_description_length: int | None = None,
    task_risk_threshold: str = "",
    required_explicit_fields: list[str] | None = None,
    forbidden_default_fields: list[str] | None = None,
    require_task_path: bool = False,
    require_existing_task_path: bool = False,
    allowed_path_roots: list[Path] | None = None,
    require_unique_task_names: bool = False,
    required_name_pattern: re.Pattern[str] | None = None,
    require_valid_dependencies: bool = False,
    require_dependency_order: bool = False,
) -> tuple[list[TaskOutput], list[SkippedTask]]:
    outputs: list[TaskOutput] = []
    skipped_tasks: list[SkippedTask] = []
    used_slugs: set[str] = set()
    seen_task_keys: set[str] = set()
    duplicate_names = (
        set(_duplicate_task_names(_effective_tasks_for_name_gate(tasks, dedupe))) if require_unique_task_names else set()
    )
    dependency_issues_by_task = (
        _dependency_gate_issues_by_task(_effective_tasks_for_name_gate(tasks, dedupe))
        if require_valid_dependencies
        else {}
    )
    dependency_order_issues_by_task = (
        _dependency_order_gate_issues_by_task(_effective_tasks_for_name_gate(tasks, dedupe))
        if require_dependency_order
        else {}
    )
    for index, task in enumerate(tasks, start=1):
        if dedupe and _is_duplicate_task(task, seen_task_keys):
            reason = "重复任务：任务名和描述已出现过"
            skipped_tasks.append(SkippedTask(task.name, reason, _skip_suggestion(reason)))
            continue
        if task.name in duplicate_names:
            reason = _duplicate_task_name_error(task.name)
            suggestion = _skip_suggestion(reason)
            skipped_tasks.append(SkippedTask(task.name, reason, suggestion))
            print(f"跳过任务 {task.name}：{reason}；建议：{suggestion}", file=sys.stderr)
            continue
        if required_name_pattern and not required_name_pattern.search(task.name):
            reason = _name_pattern_error(task.name, required_name_pattern.pattern)
            suggestion = _skip_suggestion(reason)
            skipped_tasks.append(SkippedTask(task.name, reason, suggestion))
            print(f"跳过任务 {task.name}：{reason}；建议：{suggestion}", file=sys.stderr)
            continue
        dependency_issues = dependency_issues_by_task.get(task.name, [])
        if dependency_issues:
            reason = _dependency_gate_error(task.name, dependency_issues)
            suggestion = _skip_suggestion(reason)
            skipped_tasks.append(SkippedTask(task.name, reason, suggestion))
            print(f"跳过任务 {task.name}：{reason}；建议：{suggestion}", file=sys.stderr)
            continue
        dependency_order_issues = dependency_order_issues_by_task.get(task.name, [])
        if dependency_order_issues:
            reason = _dependency_order_gate_error(task.name, dependency_order_issues)
            suggestion = _skip_suggestion(reason)
            skipped_tasks.append(SkippedTask(task.name, reason, suggestion))
            print(f"跳过任务 {task.name}：{reason}；建议：{suggestion}", file=sys.stderr)
            continue
        task_risk_issue = _task_risk_gate_issue(task, task_risk_threshold)
        if task_risk_issue:
            reason = _task_risk_gate_error(task.name, task_risk_threshold, task_risk_issue)
            suggestion = _skip_suggestion(reason)
            skipped_tasks.append(SkippedTask(task.name, reason, suggestion))
            print(f"跳过任务 {task.name}：{reason}；建议：{suggestion}", file=sys.stderr)
            continue
        try:
            output = _process_one_task(
                task,
                index,
                dry_run,
                strict,
                default_values,
                used_slugs,
                verbose,
                max_defaulted_fields,
                min_description_length,
                required_explicit_fields or [],
                forbidden_default_fields or [],
                require_task_path,
                require_existing_task_path,
                allowed_path_roots or [],
            )
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


def _effective_tasks_for_name_gate(tasks: list[TaskSpec], dedupe: bool) -> list[TaskSpec]:
    if not dedupe:
        return tasks
    effective_tasks: list[TaskSpec] = []
    seen_task_keys: set[str] = set()
    for task in tasks:
        task_key = _dedupe_key(task)
        if task_key in seen_task_keys:
            continue
        effective_tasks.append(task)
        seen_task_keys.add(task_key)
    return effective_tasks


def _duplicate_task_name_error(task_name: str) -> str:
    return f"重复任务名：{task_name}；--require-unique-task-names 要求每个任务 name 唯一"


def _has_duplicate_name_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("重复任务名" in skipped.reason for skipped in skipped_tasks)


def _name_pattern_error(task_name: str, pattern: str) -> str:
    return f"任务名称不匹配正则：{task_name}；--require-name-pattern 要求匹配：{pattern}"


def _has_name_pattern_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("任务名称不匹配正则" in skipped.reason for skipped in skipped_tasks)


def _dependency_gate_issues_by_task(tasks: list[TaskSpec]) -> dict[str, list[dict[str, str]]]:
    plan = _build_dependency_plan(tasks)
    grouped: dict[str, list[dict[str, str]]] = {}
    issues = plan.get("issues", [])
    if not isinstance(issues, list):
        return grouped
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        task_name = str(issue.get("name", "")).strip()
        if not task_name:
            continue
        grouped.setdefault(task_name, []).append(
            {
                "reason": str(issue.get("reason", "")),
                "suggestion": str(issue.get("suggestion", "")),
            }
        )
    return grouped


def _dependency_gate_error(task_name: str, issues: list[dict[str, str]]) -> str:
    reason_text = "；".join(
        issue.get("reason", "")
        for issue in issues[:3]
        if issue.get("reason", "")
    )
    if len(issues) > 3:
        reason_text = f"{reason_text}；其余 {len(issues) - 3} 个依赖问题可用 --plan-dependencies 查看"
    if not reason_text:
        reason_text = "未知依赖问题"
    return f"依赖关系无效：{task_name}；--require-valid-dependencies 检查失败：{reason_text}"


def _has_dependency_gate_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("依赖关系无效" in skipped.reason for skipped in skipped_tasks)


def _dependency_order_gate_issues_by_task(tasks: list[TaskSpec]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    duplicate_names = set(_duplicate_task_names(tasks))
    for duplicate_name in duplicate_names:
        grouped.setdefault(duplicate_name, []).append(
            {
                "reason": f"重复任务名，无法判断依赖顺序：{duplicate_name}",
                "suggestion": "先为重复任务改成唯一 name，再检查依赖顺序。",
            }
        )
    first_index_by_name: dict[str, int] = {}
    for index, task in enumerate(tasks):
        first_index_by_name.setdefault(task.name, index)
    for index, task in enumerate(tasks):
        for dependency_name in task.depends_on:
            if dependency_name == task.name:
                grouped.setdefault(task.name, []).append(
                    {
                        "reason": "任务依赖自身，无法满足依赖顺序",
                        "suggestion": "移除自依赖，或把前置任务拆分成不同 name 后再引用。",
                    }
                )
                continue
            if dependency_name in duplicate_names:
                grouped.setdefault(task.name, []).append(
                    {
                        "reason": f"依赖引用重复任务名，无法判断顺序：{dependency_name}",
                        "suggestion": "先为重复任务改成唯一 name，再让 depends_on/dependencies 引用唯一任务名。",
                    }
                )
                continue
            dependency_index = first_index_by_name.get(dependency_name)
            if dependency_index is None:
                grouped.setdefault(task.name, []).append(
                    {
                        "reason": f"依赖不存在，无法判断顺序：{dependency_name}",
                        "suggestion": "补充缺失的依赖任务，或修正 depends_on/dependencies 中的任务名。",
                    }
                )
                continue
            if dependency_index > index:
                grouped.setdefault(task.name, []).append(
                    {
                        "reason": f"依赖顺序错误：{dependency_name} 位于当前任务之后",
                        "suggestion": "把依赖任务移动到当前任务之前，或改用 --dependency-order 自动拓扑排序。",
                    }
                )
    return grouped


def _dependency_order_gate_error(task_name: str, issues: list[dict[str, str]]) -> str:
    reason_text = "；".join(issue.get("reason", "") for issue in issues[:3] if issue.get("reason", ""))
    if len(issues) > 3:
        reason_text = f"{reason_text}；其余 {len(issues) - 3} 个顺序问题可用 --plan-dependencies 查看"
    if not reason_text:
        reason_text = "未知依赖顺序问题"
    return f"依赖顺序无效：{task_name}；--require-dependency-order 检查失败：{reason_text}"


def _has_dependency_order_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("依赖顺序无效" in skipped.reason for skipped in skipped_tasks)


def _task_risk_gate_issue(task: TaskSpec, threshold: str) -> dict[str, object] | None:
    if not threshold or task.load_error:
        return None
    profile_text, profile_source = _task_profile_text(task)
    if not profile_text:
        return None
    profile = build_task_profile(profile_text)
    risk_level = str(profile.get("risk_level", "unknown"))
    if not _risk_level_matches_threshold(risk_level, threshold):
        return None
    return {
        "risk_level": risk_level,
        "risk_score": profile.get("risk_score", 0),
        "risk_factors": profile.get("risk_factors", []),
        "profile_source": profile_source,
    }


def _task_risk_gate_error(task_name: str, threshold: str, issue: dict[str, object]) -> str:
    risk_level = str(issue.get("risk_level", "unknown"))
    risk_score = issue.get("risk_score", 0)
    factors = issue.get("risk_factors", [])
    factor_text = _task_risk_factor_text(factors)
    profile_source = str(issue.get("profile_source", "unknown"))
    return (
        f"任务风险等级超限：{task_name}；--fail-on-task-risk-level {threshold} "
        f"命中 {risk_level}/{risk_score}（来源：{profile_source}）"
        f"{factor_text}"
    )


def _task_risk_factor_text(factors: object) -> str:
    if not isinstance(factors, list) or not factors:
        return ""
    factor_preview = "；".join(str(factor) for factor in factors[:3])
    if len(factors) > 3:
        factor_preview = f"{factor_preview}；其余 {len(factors) - 3} 个风险因素可用 --profile-tasks 查看"
    return f"；风险因素：{factor_preview}"


def _has_task_risk_gate_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("任务风险等级超限" in skipped.reason for skipped in skipped_tasks)


def _dedupe_key(task: TaskSpec) -> str:
    return _normalize_description(f"{task.name}\n{task.description}")


def _skip_suggestion(reason: str) -> str:
    if "缺少 description" in reason:
        return "为该任务补充 description 字段，说明编码目标和上下文。"
    if "缺少任务路径" in reason:
        return "为该任务补充 path、inspect_path 或 target_path，指向本地文件或目录。"
    if "任务路径不存在" in reason:
        return "修正 path、inspect_path 或 target_path，确保其相对当前工作目录存在且可访问。"
    if "任务路径超出允许根目录" in reason:
        return "把任务 path 调整到允许根目录内，或确认范围后扩展 --allowed-path-roots。"
    if "重复任务名" in reason:
        return "为同名任务改成唯一 name；如果确实是重复任务，可先使用 --dedupe 移除完全重复项。"
    if "任务名称不匹配正则" in reason:
        return "按 --require-name-pattern 指定的团队命名规则调整任务 name，或确认后放宽正则。"
    if "依赖关系无效" in reason:
        return "修正 depends_on/dependencies 中的未知依赖、自依赖、重复任务名或循环依赖；可先运行 --plan-dependencies 查看完整依赖报告。"
    if "依赖顺序无效" in reason:
        return "把 depends_on/dependencies 引用的任务移动到当前任务之前；如需自动重排可使用 --dependency-order。"
    if "strict 模式缺失要素" in reason:
        return "补齐提示中的 6 要素，或移除 --strict 允许脚本使用默认值。"
    if "超过 --max-defaulted-fields" in reason:
        return "补齐超限的 6 要素，或在确认可接受默认兜底后调高 --max-defaulted-fields。"
    if "description 长度不足" in reason:
        return "扩写任务 description，补充目标、范围、验证方式、约束或受阻条件等关键上下文。"
    if "任务风险等级超限" in reason:
        return "先运行 --profile-tasks 查看完整风险画像，拆分高风险任务、补充约束和验证面，或确认后调整 --fail-on-task-risk-level 阈值。"
    if "禁止默认兜底要素" in reason:
        return "在任务 fields 中填写这些要素，或在 description 中补充可被识别的明确字段内容。"
    if "缺少显式要素" in reason:
        return "在任务 fields 中填写这些要素，或在 description 中使用字段标签显式写出对应内容。"
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
    max_defaulted_fields: int | None = None,
    min_description_length: int | None = None,
    required_explicit_fields: list[str] | None = None,
    forbidden_default_fields: list[str] | None = None,
    require_task_path: bool = False,
    require_existing_task_path: bool = False,
    allowed_path_roots: list[Path] | None = None,
) -> TaskOutput:
    if task.load_error:
        raise ValueError(task.load_error)
    if not task.description:
        raise ValueError("缺少 description")
    if min_description_length is not None and len(task.description.strip()) < min_description_length:
        raise ValueError(_description_length_error(task.description, min_description_length))
    if (require_task_path or require_existing_task_path) and not task.inspect_path:
        raise ValueError(_required_task_path_error())
    if require_existing_task_path and not Path(task.inspect_path).exists():
        raise ValueError(_missing_existing_task_path_error(task.inspect_path))
    if allowed_path_roots:
        if not task.inspect_path:
            raise ValueError(_required_task_path_error())
        if not _path_inside_allowed_roots(task.inspect_path, allowed_path_roots):
            raise ValueError(_allowed_path_roots_error(task.inspect_path, allowed_path_roots))
    missing_explicit_fields = _missing_explicit_fields(task, required_explicit_fields or [])
    if missing_explicit_fields:
        raise ValueError(_missing_explicit_fields_error(missing_explicit_fields))
    prepared = _prepare_task(task, strict, default_values)
    if max_defaulted_fields is not None and len(prepared.defaulted_keys) > max_defaulted_fields:
        raise ValueError(_defaulted_limit_error(prepared.defaulted_keys, max_defaulted_fields))
    forbidden_defaults = _forbidden_defaulted_fields(prepared.defaulted_keys, forbidden_default_fields or [])
    if forbidden_defaults:
        raise ValueError(_forbidden_default_fields_error(forbidden_defaults))
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


def _defaulted_limit_error(defaulted_keys: list[str], max_defaulted_fields: int) -> str:
    return (
        f"默认填充要素 {len(defaulted_keys)} 个，超过 --max-defaulted-fields {max_defaulted_fields}："
        f"{_format_labels(defaulted_keys)}"
    )


def _has_defaulted_limit_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("超过 --max-defaulted-fields" in skipped.reason for skipped in skipped_tasks)


def _description_length_error(description: str, min_description_length: int) -> str:
    return (
        f"description 长度不足：当前 {len(description.strip())} 个字符，"
        f"低于 --min-description-length {min_description_length}"
    )


def _has_description_length_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("description 长度不足" in skipped.reason for skipped in skipped_tasks)


def _required_task_path_error() -> str:
    return "缺少任务路径：--require-task-path 要求提供 path、inspect_path 或 target_path"


def _has_required_path_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("缺少任务路径" in skipped.reason for skipped in skipped_tasks)


def _missing_existing_task_path_error(task_path: str) -> str:
    return f"任务路径不存在：{task_path}；--require-existing-task-path 要求 path/inspect_path/target_path 指向本地已存在路径"


def _has_missing_existing_path_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("任务路径不存在" in skipped.reason for skipped in skipped_tasks)


def _path_inside_allowed_roots(task_path: str, allowed_roots: list[Path]) -> bool:
    resolved_path = _resolved_path(task_path)
    return any(_path_is_relative_to(resolved_path, root) for root in allowed_roots)


def _resolved_path(raw_path: str) -> Path:
    return Path(raw_path).expanduser().resolve(strict=False)


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _allowed_path_roots_error(task_path: str, allowed_roots: list[Path]) -> str:
    return (
        f"任务路径超出允许根目录：{task_path}；"
        f"--allowed-path-roots 允许：{_format_allowed_roots(allowed_roots)}"
    )


def _format_allowed_roots(allowed_roots: list[Path]) -> str:
    return "、".join(str(root) for root in allowed_roots)


def _has_allowed_path_root_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("任务路径超出允许根目录" in skipped.reason for skipped in skipped_tasks)


def _forbidden_defaulted_fields(defaulted_keys: list[str], forbidden_fields: list[str]) -> list[str]:
    if not forbidden_fields:
        return []
    defaulted_set = set(defaulted_keys)
    return [key for key in forbidden_fields if key in defaulted_set]


def _forbidden_default_fields_error(forbidden_defaults: list[str]) -> str:
    return f"禁止默认兜底要素：{_format_labels(forbidden_defaults)}；--forbid-default-fields 要求这些字段不能使用默认值"


def _has_forbidden_default_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("禁止默认兜底要素" in skipped.reason for skipped in skipped_tasks)


def _missing_explicit_fields(task: TaskSpec, required_fields: list[str]) -> list[str]:
    if not required_fields:
        return []
    explicit_fields = _explicit_task_fields(task)
    return [key for key in required_fields if key not in explicit_fields]


def _explicit_task_fields(task: TaskSpec) -> set[str]:
    fields = {key for key, value in task.fields.items() if value}
    fields.update(_extract_labeled_fields(task.description).keys())
    return fields


def _missing_explicit_fields_error(missing_fields: list[str]) -> str:
    return f"缺少显式要素：{_format_labels(missing_fields)}；--require-explicit-fields 要求这些字段来自 fields 或 description 标签"


def _has_explicit_field_skips(skipped_tasks: list[SkippedTask]) -> bool:
    return any("缺少显式要素" in skipped.reason for skipped in skipped_tasks)


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


def _ensure_no_overwrite_targets(
    outputs: list[TaskOutput],
    output_dir: str | None,
    output_file: str | None,
    report_json: str | None,
    no_overwrite: bool,
) -> None:
    if not no_overwrite:
        return
    targets = _no_overwrite_target_entries(outputs, output_dir, output_file, report_json)
    problems: list[str] = []
    duplicate_targets = _duplicate_output_targets(targets)
    if duplicate_targets:
        problems.append("多个输出目标指向同一路径：" + "；".join(duplicate_targets))
    existing_targets = _existing_output_targets(targets)
    if existing_targets:
        problems.append("目标已存在：" + "；".join(existing_targets))
    if output_dir:
        output_dir_path = Path(output_dir)
        if output_dir_path.exists() and not output_dir_path.is_dir():
            problems.append(f"--output-dir 目标已存在但不是目录：{output_dir_path}")
    if problems:
        raise ValueError("；".join(problems) + "。请更换输出路径、删除旧文件，或移除 --no-overwrite 后明确允许覆盖。")


def _no_overwrite_target_entries(
    outputs: list[TaskOutput],
    output_dir: str | None,
    output_file: str | None,
    report_json: str | None,
) -> list[tuple[str, Path]]:
    targets: list[tuple[str, Path]] = []
    if output_file:
        targets.append(("--output-file", Path(output_file)))
    if output_dir:
        output_dir_path = Path(output_dir)
        for output in outputs:
            filename = f"{output.file_slug}{GOAL_FILE_SUFFIX}"
            targets.append((f"--output-dir/{filename}", output_dir_path / filename))
    if report_json:
        targets.append(("--report-json", Path(report_json)))
    return targets


def _duplicate_output_targets(targets: list[tuple[str, Path]]) -> list[str]:
    by_path: dict[Path, list[str]] = {}
    for label, path in targets:
        by_path.setdefault(path.expanduser().resolve(strict=False), []).append(label)
    duplicates: list[str] = []
    for path, labels in by_path.items():
        if len(labels) > 1:
            duplicates.append(f"{' 和 '.join(labels)} -> {path}")
    return duplicates


def _existing_output_targets(targets: list[tuple[str, Path]]) -> list[str]:
    existing = [f"{label} {path}" for label, path in targets if path.exists()]
    if len(existing) > 10:
        return [*existing[:10], f"其余 {len(existing) - 10} 个目标也已存在"]
    return existing


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
