# 负责从 JSON/CSV 批量读取编码任务并生成 Codex CLI /goal 指令。
from __future__ import annotations

import argparse
import csv
import json
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
    _GoalFields,
    _extract_labeled_fields,
    analyze_description,
    render_goal_text,
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
        tasks = _load_tasks(_input_path_from_args(args))
        tasks = _filter_tasks(tasks, args.filter)
        tasks = _sort_tasks(tasks, args.sort_by)
        tasks = _limit_tasks(tasks, args.limit)
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
        _write_report_json(outputs, skipped_tasks, stats, Path(args.report_json))
    print(_format_summary(stats))
    if fail_on_skipped and stats.skipped_count:
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量生成 Codex CLI /goal 指令。")
    parser.add_argument("input_path", nargs="?", help="输入文件路径，可替代 --input。")
    parser.add_argument("--input", help="输入文件路径，支持 .json、.jsonl、.csv、.yaml、.yml、.md 或 .markdown。")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--output-dir", help="输出目录，每个任务生成一个 .txt 文件。")
    output_group.add_argument("--output-file", help="输出到单个文件。")
    parser.add_argument("--defaults-json", help="JSON 默认值文件，用于覆盖缺失 6 要素的默认填充。")
    parser.add_argument("--report-json", help="把批量处理结果、缺失要素和跳过原因写入 JSON 报告。")
    parser.add_argument("--filter", help="按正则筛选任务名或描述，只处理匹配的任务。")
    parser.add_argument("--sort-by", choices=("input", "name"), default="input", help="批量任务输出顺序，默认保持输入顺序。")
    parser.add_argument("--limit", type=int, help="只处理前 N 个任务，适合大清单试跑。")
    parser.add_argument("--dedupe", action="store_true", help="按任务名和描述跳过重复任务。")
    parser.add_argument("--fail-on-skipped", action="store_true", help="有跳过任务时以退出码 1 结束，适合 CI 门禁。")
    parser.add_argument("--summary-only", action="store_true", help="抑制任务正文 stdout，仅输出最终摘要。")
    parser.add_argument("--dry-run", action="store_true", help="只分析要素完整度，不生成指令。")
    parser.add_argument("--check", action="store_true", help="校验输入任务文件，等价于 --dry-run --strict --summary-only --fail-on-skipped。")
    parser.add_argument("--strict", action="store_true", help="缺失 6 要素时跳过任务，不使用默认填充。")
    parser.add_argument("--verbose", action="store_true", help="打印详细处理日志。")
    return parser



def _input_path_from_args(args: argparse.Namespace) -> Path:
    input_value = args.input or args.input_path
    if not input_value:
        raise ValueError("必须提供 --input 或位置输入文件路径")
    return Path(input_value)


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


def _load_default_values(defaults_json: str | None) -> dict[str, str]:
    defaults = dict(INTERACTIVE_DEFAULTS)
    if not defaults_json:
        return defaults
    data = json.loads(Path(defaults_json).read_text(encoding="utf-8"))
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
    data = json.loads(input_path.read_text(encoding="utf-8"))
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
    tasks: list[TaskSpec] = []
    for line_number, line in enumerate(input_path.read_text(encoding="utf-8").splitlines(), start=1):
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
        return "改用 .json、.jsonl、.csv、.md 或 .markdown 输入文件。"
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
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "success_count": stats.success_count,
        "skipped_count": stats.skipped_count,
        "elapsed_seconds": round(stats.elapsed_seconds, 4),
        "tasks": [_task_report(output) for output in outputs],
        "skipped": [_skipped_report(skipped) for skipped in skipped_tasks],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _task_report(output: TaskOutput) -> dict[str, object]:
    return {
        "name": output.task_name,
        "file_slug": output.file_slug,
        "present": output.present_keys,
        "missing_before_defaults": output.missing_before_defaults,
        "defaulted": output.defaulted_keys,
    }


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
