# 负责分析编码任务描述并生成 Codex CLI /goal 指令纯文本。
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import TypedDict

ELEMENT_ORDER: tuple[str, ...] = (
    "outcome",
    "verification",
    "constraints",
    "boundaries",
    "iteration",
    "blocked",
)
ELEMENT_LABELS: dict[str, str] = {
    "outcome": "Outcome（目标结果）",
    "verification": "Verification Surface（验证方式）",
    "constraints": "Constraints（约束）",
    "boundaries": "Boundaries（边界）",
    "iteration": "Iteration Policy（迭代策略）",
    "blocked": "Blocked Stop Condition（受阻停止条件）",
}
QUESTION_EXAMPLES: dict[str, str] = {
    "outcome": "示例：为 src/services/ 的核心逻辑新增不少于 20 个 pytest 用例。",
    "verification": "示例：每次改动后运行 pytest tests/services -q，最终运行全量测试。",
    "constraints": "示例：不改业务逻辑、不引入新依赖、不修改公共 API。",
    "boundaries": "示例：仅处理 src/services/，排除 legacy/、迁移脚本和生成文件。",
    "iteration": "示例：每个模块一组改动，验证通过后单独 commit，预期 4-8 个 commit。",
    "blocked": "示例：缺少业务预期时停下问人；允许跳过项不超过 10%。",
}
OUTCOME_KEYWORDS: tuple[str, ...] = (
    "实现",
    "新增",
    "添加",
    "修复",
    "优化",
    "重构",
    "迁移",
    "升级",
    "生成",
    "补齐",
    "覆盖",
    "开发",
    "文档",
    "测试",
)
SPECIFICITY_KEYWORDS: tuple[str, ...] = (
    "接口",
    "api",
    "模块",
    "页面",
    "服务",
    "目录",
    "文件",
    "函数",
    "类",
    "报告",
    "覆盖率",
)
VERIFICATION_ACTION_KEYWORDS: tuple[str, ...] = (
    "验证",
    "运行",
    "执行",
    "确认",
    "检查",
    "通过",
    "跑测试",
    "测试通过",
)
CONSTRAINT_KEYWORDS: tuple[str, ...] = (
    "不修改",
    "不改变",
    "不引入",
    "不做",
    "禁止",
    "不能",
    "不得",
    "保持",
    "兼容",
    "约束",
)
BOUNDARY_KEYWORDS: tuple[str, ...] = (
    "仅",
    "只",
    "范围",
    "目录",
    "文件",
    "排除",
    "包含",
    "src/",
    "app/",
    "services/",
    "tests/",
)
ITERATION_KEYWORDS: tuple[str, ...] = (
    "每个",
    "每次",
    "逐个",
    "分批",
    "迭代",
    "commit",
    "提交",
    "验证后",
    "预期",
)
BLOCKED_KEYWORDS: tuple[str, ...] = (
    "阻塞",
    "受阻",
    "停下",
    "问我",
    "跳过",
    "无法",
    "缺少",
    "人工",
    "不超过",
    "%",
)
NEGATION_PREFIXES: tuple[str, ...] = ("非", "不", "没有", "无需", "无")
TEST_TOOL_KEYWORDS: tuple[str, ...] = ("pytest", "unittest")
REPORT_KEYWORDS: tuple[str, ...] = ("报告", "文档", "readme", "report", "说明", "审计")
DETAIL_KEYWORDS: tuple[str, ...] = ("维度", "规则", "类别", "批量", "重构", "优化", "迁移")
COMMIT_SCOPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("错误处理", ("错误处理", "异常", "错误码", "失败路径")),
    ("类型注解", ("类型注解", "类型检查", "typing", "mypy")),
    ("结构设计", ("结构设计", "结构", "拆分", "长函数", "架构")),
    ("代码质量", ("代码质量", "质量", "优化", "坏味道")),
    ("接口", ("接口", "api")),
    ("文档", ("文档", "报告", "readme")),
    ("数据迁移", ("迁移", "升级")),
)
PROFILE_RULES: tuple[tuple[str, str, tuple[str, ...], dict[str, str]], ...] = (
    (
        "testing",
        "测试编写",
        ("测试", "pytest", "unittest", "覆盖率", "用例"),
        {
            "outcome": "明确测试目录、被测模块、目标用例数量或覆盖路径。",
            "verification": "指定局部测试命令和最终全量测试命令。",
            "constraints": "强调不改业务逻辑、不弱化现有测试、不引入未确认依赖。",
            "boundaries": "说明被测代码目录和对应测试目录，排除集成成本过高的范围。",
            "iteration": "建议按文件或模块分批补测试，每批验证后单独 commit。",
            "blocked": "无法从代码推断断言或业务预期时停下问人。",
        },
    ),
    (
        "bugfix",
        "Bug 修复",
        ("修复", "bug", "错误", "报错", "异常", "500"),
        {
            "outcome": "明确复现条件、期望行为和修复后的用户可见结果。",
            "verification": "要求复现用例、回归测试或最小命令证明问题消失。",
            "constraints": "保持公共 API、错误码、数据格式和兼容行为不被误改。",
            "boundaries": "限定到触发 bug 的模块、调用链和必要测试。",
            "iteration": "建议先补失败验证，再最小修复，再单独提交。",
            "blocked": "缺少业务期望或必须改变外部行为时停下确认。",
        },
    ),
    (
        "refactor",
        "代码质量优化/重构",
        ("优化", "重构", "质量", "坏味道", "复杂度"),
        {
            "outcome": "明确处理目录、文件数量、质量维度和是否生成报告。",
            "verification": "要求每个改动后的语法/测试验证和最终审计证据。",
            "constraints": "强调行为保持、不做范围外重构、不引入新依赖。",
            "boundaries": "列出包含目录、排除测试/迁移/生成文件等范围。",
            "iteration": "建议每个独立重构点验证后单独 commit。",
            "blocked": "只有不可避免改变功能行为时才允许跳过或停下。",
        },
    ),
    (
        "docs",
        "文档生成",
        ("文档", "报告", "readme", "说明"),
        {
            "outcome": "明确目标文档、章节结构、覆盖对象和可追溯证据。",
            "verification": "要求检查链接、命令示例、路径引用和与代码一致性。",
            "constraints": "不编造未验证能力，不修改无关代码。",
            "boundaries": "限定文档文件、引用代码范围和排除范围。",
            "iteration": "建议按章节或主题提交，每次检查死链和示例。",
            "blocked": "缺少产品口径或无法验证事实时停下问人。",
        },
    ),
)
DEFAULT_PROFILE_TEMPLATE: dict[str, str] = {
    "outcome": "明确最终交付物、完成程度、分支名和可审计产物。",
    "verification": "给出每步局部验证和最终验收命令或证据。",
    "constraints": "说明不能修改的行为、依赖、接口、数据和风格限制。",
    "boundaries": "列出包含目录/文件和排除范围，范围外只记录。",
    "iteration": "规定每步粒度、验证动作、commit 节奏和预期数量。",
    "blocked": "定义允许跳过、必须停下问人和记录证据的条件。",
}
DEFAULT_PATH_HINT = "对应文件"
MAX_INTERACTIVE_ROUNDS = 3
INTERACTIVE_DEFAULTS: dict[str, str] = {
    "outcome": "完成用户描述的编码任务，并在最终回复中列出实际交付物",
    "verification": "每个改动后运行最相关的现有验证命令，最终执行语法检查或项目可用测试",
    "constraints": "不改变与目标无关的既有功能行为，不引入未经确认的新依赖，不做范围外重构",
    "boundaries": "仅处理用户描述中直接相关的文件和目录，范围外问题只记录",
    "iteration": "每个独立改动验证通过后单独 git add + git commit，预期 3-8 个 commit",
    "blocked": "遇到必须由用户决定的业务行为变化、无法验证的关键风险或范围冲突时停下询问",
}
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "outcome": ("outcome", "目标结果", "目标", "交付"),
    "verification": ("verification", "验证方式", "验证", "验收"),
    "constraints": ("constraints", "约束", "限制", "不能"),
    "boundaries": ("boundaries", "边界", "范围", "目录"),
    "iteration": ("iteration", "迭代策略", "迭代", "提交"),
    "blocked": ("blocked", "受阻停止条件", "阻塞", "停下", "跳过"),
}
FIELD_VALUE_TRIM_CHARS = " ;；,，。.\n\t"
PATH_PATTERN = re.compile(r"(?:^|\s|`)([\w./-]+/[\w./-]*|[\w.-]+\.[A-Za-z0-9]+)")
NUMBER_PATTERN = re.compile(r"\d+")
BRANCH_PATTERN = re.compile(r"(?:分支|branch)\s*[`'\"]?([A-Za-z0-9._/-]+)")
COMMIT_RANGE_PATTERN = re.compile(r"(\d+)\s*[-~—]\s*(\d+)\s*(?:个)?\s*(?:commit)?", re.IGNORECASE)
DEFAULT_BRANCH_SLUG = "codex-goal-task"
DEFAULT_COMMIT_RANGE = "4-12 个 commit"
TEXT_PREVIEW_LENGTH = 120
SEPARATOR_START = "==================== /goal 指令开始 ===================="
SEPARATOR_END = "==================== /goal 指令结束 ===================="


class Question(TypedDict):
    """表示一个待追问要素。"""

    element: str
    label: str
    example: str


class AnalysisResult(TypedDict):
    """表示需求完整度分析结果。"""

    missing: list[str]
    present: dict[str, str]
    questions: list[Question]


@dataclass(frozen=True)
class _GoalFields:
    outcome: str
    verification: str
    constraints: str
    boundaries: str
    iteration: str
    blocked: str


def analyze_description(description: str) -> AnalysisResult:
    """分析任务描述并返回缺失的 /goal 必要要素。

    Args:
        description: 用户输入的一句话或详细编码任务描述。

    Returns:
        包含缺失要素、已识别要素和一次性追问建议的分析结果。
    """
    normalized_text = _normalize_text(description)
    present = {
        key: _present_note(key, normalized_text)
        for key in ELEMENT_ORDER
        if _is_element_present(key, normalized_text)
    }
    missing = [key for key in ELEMENT_ORDER if key not in present]
    questions = [_build_question(key) for key in missing]
    return {"missing": missing, "present": present, "questions": questions}


def render_goal_text(goal: _GoalFields) -> str:
    """按照 5 段格式渲染可复制的 /goal 指令文本。

    Args:
        goal: 已补齐 6 个必要要素的任务字段。

    Returns:
        前后带分隔线的 /goal 指令纯文本。
    """
    branch_name = _suggest_branch_name(goal.outcome)
    sections = [
        SEPARATOR_START,
        f"/goal {_build_overview(goal, branch_name)}",
        "",
        _build_mandatory_section(goal),
        "",
        _build_commit_section(goal, branch_name),
        "",
        _build_output_section(goal),
        "",
        _build_detail_section(goal),
        SEPARATOR_END,
    ]
    return "\n".join(sections)


def main(argv: list[str] | None = None) -> int:
    """执行命令行入口。

    Args:
        argv: 可选命令行参数列表；为空时使用进程参数。

    Returns:
        进程退出码，0 表示成功，2 表示参数错误。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.profile:
            print(json.dumps(build_task_profile(args.profile), ensure_ascii=False, indent=2))
            return 0
        if args.analyze:
            print(json.dumps(analyze_description(args.analyze), ensure_ascii=False, indent=2))
            return 0
        if args.generate:
            print(render_goal_text(_goal_from_args(args)))
            return 0
        if args.interactive:
            return _run_interactive()
    except ValueError as error:
        print(f"参数错误：{error}", file=sys.stderr)
        return 2
    parser.print_help(sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="分析任务描述并生成 Codex CLI /goal 指令。")
    parser.add_argument("--analyze", help="分析用户任务描述并输出缺失要素 JSON。")
    parser.add_argument("--profile", help="识别任务类型、复杂度和推荐 6 要素模板。")
    parser.add_argument("--generate", action="store_true", help="生成完整 /goal 指令文本。")
    parser.add_argument("--interactive", action="store_true", help="交互式补全要素并生成 /goal 指令。")
    parser.add_argument("--outcome", help="Outcome（目标结果）。")
    parser.add_argument("--verification", help="Verification Surface（验证方式）。")
    parser.add_argument("--constraints", help="Constraints（约束）。")
    parser.add_argument("--boundaries", help="Boundaries（边界）。")
    parser.add_argument("--iteration", help="Iteration Policy（迭代策略）。")
    parser.add_argument("--blocked", help="Blocked Stop Condition（受阻停止条件）。")
    return parser


def build_task_profile(description: str) -> dict[str, object]:
    """构建任务类型画像和 6 要素推荐模板。"""
    normalized_text = _normalize_text(description)
    analysis = analyze_description(normalized_text)
    profile_id, label, template = _infer_profile(normalized_text)
    level, reasons = _estimate_complexity(normalized_text, analysis["missing"])
    return {
        "task_type": {"id": profile_id, "label": label},
        "complexity": {"level": level, "reasons": reasons},
        "ask_strategy": _ask_strategy(level, analysis["missing"]),
        "missing": analysis["missing"],
        "present": analysis["present"],
        "recommended_fields": template,
    }


def _infer_profile(text: str) -> tuple[str, str, dict[str, str]]:
    lowered_text = text.lower()
    for profile_id, label, keywords, template in PROFILE_RULES:
        if _contains_any(lowered_text, keywords):
            return profile_id, label, template
    return "generic", "通用编码任务", DEFAULT_PROFILE_TEMPLATE


def _estimate_complexity(text: str, missing: list[str]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if len(text) > 120:
        reasons.append("描述较长，可能包含多个子任务或隐含约束")
    if len(missing) >= 4:
        reasons.append("缺失 4 个以上必要要素，需要集中补齐")
    if any(keyword in text for keyword in ("批量", "迁移", "升级", "多个", "全量")):
        reasons.append("涉及批量或跨范围执行，需要更严格边界和迭代策略")
    if not reasons:
        return "low", ["描述较短且缺失要素较少"]
    if len(reasons) == 1 and len(missing) <= 3:
        return "medium", reasons
    return "high", reasons


def _ask_strategy(level: str, missing: list[str]) -> str:
    labels = "、".join(ELEMENT_LABELS[key] for key in missing) or "无缺失要素"
    if level == "low":
        return f"简短追问缺失项即可：{labels}。"
    if level == "medium":
        return f"一次性追问缺失项，并要求用户给出路径、命令或数量：{labels}。"
    return f"按 6 要素分组追问，优先确认 Outcome、Boundaries 和 Verification：{labels}。"


def _goal_from_args(args: argparse.Namespace) -> _GoalFields:
    return _GoalFields(
        outcome=_required_value(args, "outcome"),
        verification=_required_value(args, "verification"),
        constraints=_required_value(args, "constraints"),
        boundaries=_required_value(args, "boundaries"),
        iteration=_required_value(args, "iteration"),
        blocked=_required_value(args, "blocked"),
    )


def _run_interactive() -> int:
    field_values: dict[str, str] = {}
    try:
        combined_text = input("请输入编码任务描述：").strip()
        if not combined_text:
            print("未输入任务描述，已退出。", file=sys.stderr)
            return 2
        field_values.update(_extract_labeled_fields(combined_text))
        for _round_index in range(MAX_INTERACTIVE_ROUNDS):
            analysis = analyze_description(combined_text)
            _merge_present_fallbacks(field_values, combined_text, analysis)
            missing = _missing_field_keys(field_values)
            if not missing:
                print(render_goal_text(_fields_from_mapping(field_values)))
                return 0
            _print_missing_questions(missing)
            supplement = input("请一次性补充以上信息：").strip()
            if supplement:
                combined_text = f"{combined_text}\n{supplement}"
                field_values.update(_extract_labeled_fields(supplement))
        _merge_present_fallbacks(field_values, combined_text, analyze_description(combined_text))
        defaulted_keys = _apply_interactive_defaults(field_values)
        _print_default_notice(defaulted_keys)
        print(render_goal_text(_fields_from_mapping(field_values)))
        return 0
    except KeyboardInterrupt:
        print("\n已取消交互。", file=sys.stderr)
        return 130


def _extract_labeled_fields(text: str) -> dict[str, str]:
    values = _extract_inline_labeled_fields(text)
    for line in text.splitlines():
        key, value = _parse_labeled_line(line)
        if key and value and key not in values:
            values[key] = value
    return values


def _extract_inline_labeled_fields(text: str) -> dict[str, str]:
    markers = _find_field_markers(text)
    values: dict[str, str] = {}
    for index, (_start, end, key) in enumerate(markers):
        next_start = markers[index + 1][0] if index + 1 < len(markers) else len(text)
        value = text[end:next_start].strip(FIELD_VALUE_TRIM_CHARS)
        if value:
            values[key] = value
    return values


def _find_field_markers(text: str) -> list[tuple[int, int, str]]:
    lowered_text = text.lower()
    markers: list[tuple[int, int, str]] = []
    for key, aliases in FIELD_ALIASES.items():
        markers.extend(_markers_for_aliases(lowered_text, key, aliases))
    return sorted(markers, key=lambda marker: marker[0])


def _markers_for_aliases(
    lowered_text: str,
    key: str,
    aliases: tuple[str, ...],
) -> list[tuple[int, int, str]]:
    markers: list[tuple[int, int, str]] = []
    for alias in aliases:
        for separator in ("：", ":"):
            marker = f"{alias.lower()}{separator}"
            markers.extend(_markers_for_text(lowered_text, key, marker))
    return markers


def _markers_for_text(lowered_text: str, key: str, marker: str) -> list[tuple[int, int, str]]:
    markers: list[tuple[int, int, str]] = []
    start_index = 0
    while True:
        index = lowered_text.find(marker, start_index)
        if index == -1:
            return markers
        markers.append((index, index + len(marker), key))
        start_index = index + len(marker)


def _parse_labeled_line(line: str) -> tuple[str | None, str]:
    stripped_line = line.strip()
    lowered_line = stripped_line.lower()
    for key, aliases in FIELD_ALIASES.items():
        value = _value_after_alias(stripped_line, lowered_line, aliases)
        if value:
            return key, value
    return None, ""


def _value_after_alias(line: str, lowered_line: str, aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        for separator in ("：", ":"):
            prefix = f"{alias.lower()}{separator}"
            if lowered_line.startswith(prefix):
                return line[len(prefix):].strip()
    return ""


def _merge_present_fallbacks(
    field_values: dict[str, str],
    combined_text: str,
    analysis: AnalysisResult,
) -> None:
    for key in analysis["present"]:
        if not field_values.get(key):
            field_values[key] = _fallback_field_value(key, combined_text)


def _fallback_field_value(key: str, combined_text: str) -> str:
    if key == "outcome":
        return combined_text.splitlines()[0].strip()
    return _normalize_text(combined_text)


def _missing_field_keys(field_values: dict[str, str]) -> list[str]:
    return [key for key in ELEMENT_ORDER if not field_values.get(key)]


def _print_missing_questions(missing: list[str]) -> None:
    print("你的需求还缺少以下信息，请补充：")
    for index, key in enumerate(missing, start=1):
        print(f"{index}. {ELEMENT_LABELS[key]}：{QUESTION_EXAMPLES[key]}")
    print("你可以直接简短回答，我会帮你补全成完整指令。")


def _apply_interactive_defaults(field_values: dict[str, str]) -> list[str]:
    defaulted_keys: list[str] = []
    for key in ELEMENT_ORDER:
        if not field_values.get(key):
            field_values[key] = INTERACTIVE_DEFAULTS[key]
            defaulted_keys.append(key)
    return defaulted_keys


def _print_default_notice(defaulted_keys: list[str]) -> None:
    if not defaulted_keys:
        return
    labels = "、".join(ELEMENT_LABELS[key] for key in defaulted_keys)
    print(f"已达到最多 {MAX_INTERACTIVE_ROUNDS} 轮补充，以下要素使用默认值：{labels}。")


def _fields_from_mapping(field_values: dict[str, str]) -> _GoalFields:
    return _GoalFields(
        outcome=field_values["outcome"],
        verification=field_values["verification"],
        constraints=field_values["constraints"],
        boundaries=field_values["boundaries"],
        iteration=field_values["iteration"],
        blocked=field_values["blocked"],
    )


def _required_value(args: argparse.Namespace, field_name: str) -> str:
    value = getattr(args, field_name)
    if not isinstance(value, str) or not value.strip():
        label = ELEMENT_LABELS.get(field_name, field_name)
        raise ValueError(f"--{field_name} 不能为空，请补充 {label}")
    return value.strip()


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().split())


def _is_element_present(key: str, text: str) -> bool:
    lowered_text = text.lower()
    if key == "outcome":
        return _has_outcome(lowered_text)
    if key == "verification":
        return _has_verification(lowered_text)
    if key == "constraints":
        return _contains_any(lowered_text, CONSTRAINT_KEYWORDS)
    if key == "boundaries":
        return _contains_any(lowered_text, BOUNDARY_KEYWORDS) or bool(PATH_PATTERN.search(text))
    if key == "iteration":
        return _contains_any(lowered_text, ITERATION_KEYWORDS)
    if key == "blocked":
        return _contains_any(lowered_text, BLOCKED_KEYWORDS)
    raise ValueError(f"未知要素：{key}")


def _has_outcome(lowered_text: str) -> bool:
    has_action = _contains_any(lowered_text, OUTCOME_KEYWORDS)
    has_specificity = (
        _contains_any(lowered_text, SPECIFICITY_KEYWORDS)
        or bool(PATH_PATTERN.search(lowered_text))
        or bool(NUMBER_PATTERN.search(lowered_text))
    )
    return has_action and has_specificity


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered_text = text.lower()
    return any(keyword.lower() in lowered_text for keyword in keywords)


def _has_verification(lowered_text: str) -> bool:
    return _contains_any(lowered_text, VERIFICATION_ACTION_KEYWORDS)


def _mentions_test_task(text: str) -> bool:
    return _contains_unnegated_keyword(text, "测试") or _contains_unnegated_tool(text)


def _contains_unnegated_keyword(text: str, keyword: str) -> bool:
    start_index = 0
    while True:
        index = text.find(keyword, start_index)
        if index == -1:
            return False
        if not _has_negation_prefix(text, index):
            return True
        start_index = index + len(keyword)


def _contains_unnegated_tool(text: str) -> bool:
    lowered_text = text.lower()
    return any(_contains_unnegated_keyword(lowered_text, keyword) for keyword in TEST_TOOL_KEYWORDS)


def _has_negation_prefix(text: str, keyword_index: int) -> bool:
    compact_prefix = text[:keyword_index].rstrip()
    return any(compact_prefix.endswith(prefix) for prefix in NEGATION_PREFIXES)


def _present_note(key: str, text: str) -> str:
    preview = text[:TEXT_PREVIEW_LENGTH]
    suffix = "..." if len(text) > TEXT_PREVIEW_LENGTH else ""
    return f"输入中已提及 {ELEMENT_LABELS[key]} 相关信息：{preview}{suffix}"


def _build_question(key: str) -> Question:
    return {
        "element": key,
        "label": ELEMENT_LABELS[key],
        "example": QUESTION_EXAMPLES[key],
    }


def _build_overview(goal: _GoalFields, branch_name: str) -> str:
    return (
        f"请新建并切换到 `{branch_name}` 分支，完成以下目标：{goal.outcome}。"
        f"验证方式为：{goal.verification}。约束为：{goal.constraints}。"
        f"任务边界为：{goal.boundaries}。迭代策略为：{goal.iteration}。"
        f"受阻停止条件为：{goal.blocked}。全过程必须保持改动可审计、可验证、可回滚。"
    )


def _build_mandatory_section(goal: _GoalFields) -> str:
    max_skip_rule = _blocked_rule(goal.blocked)
    rules = [
        "强制要求：",
        "1. 禁止以“风险大”“没有必要”“时间不足”为笼统理由跳过范围内工作。",
        f"2. {max_skip_rule}",
        "3. 禁止把多个独立改动合并成一个大 commit；每个独立改动必须先验证再提交。",
        "4. 禁止为了通过验证而删除、弱化或绕过现有测试、类型检查或质量检查。",
        "5. 禁止擅自扩大边界；发现范围外问题只记录，不纳入本轮改动。",
        "6. 最终回复必须列出完成项、验证命令、commit 列表、未完成项和剩余风险。",
    ]
    return "\n".join(rules)


def _blocked_rule(blocked: str) -> str:
    if "%" in blocked or "不超过" in blocked:
        return f"确需跳过时必须满足用户给定条件：{blocked}。"
    return f"只有触发受阻条件时才允许停下或跳过，并必须给出证据：{blocked}。"


def _build_commit_section(goal: _GoalFields, branch_name: str) -> str:
    commit_range = _expected_commit_range(goal.iteration)
    commit_scope = _infer_commit_scope(goal.outcome)
    examples = _commit_examples(goal.outcome, goal.boundaries)
    lines = [
        "提交规则：",
        f"- 预期提交数量为 {commit_range}；如果实际偏离，必须在最终回复解释原因。",
        "- 每个独立改动完成后立即运行对应验证，再执行 `git add` 和 `git commit`。",
        "- Commit message 使用 `<type>(<scope>): <改动类型> - <简要说明>` 格式。",
        f"- `<scope>` 优先使用业务维度名；当前建议 scope 为 `{commit_scope}`。",
        f"- 示例：`{examples[0]}`；`{examples[1]}`；`{examples[2]}`。",
        f"- 全部完成并验证通过后执行 `git checkout main && git merge {branch_name}`。",
        "- 合并完成后执行 `git push -u origin main`；如果用户明确要求不推送，则记录跳过原因。",
    ]
    return "\n".join(lines)


def _expected_commit_range(iteration: str) -> str:
    match = COMMIT_RANGE_PATTERN.search(iteration)
    if match:
        return f"{match.group(1)}-{match.group(2)} 个 commit"
    return DEFAULT_COMMIT_RANGE


def _commit_examples(outcome: str, boundaries: str) -> list[str]:
    commit_type = _infer_commit_type(outcome)
    scope = _infer_commit_scope(outcome)
    path_hint = _extract_path_hint(f"{boundaries} {outcome}")
    change_type = _commit_change_type(outcome)
    return [
        f"{commit_type}({scope}): {change_type} - {path_hint}",
        f"{commit_type}({scope}): 完成下一项独立改动 - {path_hint}",
        f"chore({scope}): 补充最终验证记录 - {path_hint}",
    ]


def _infer_commit_type(outcome: str) -> str:
    lowered_text = outcome.lower()
    if _mentions_test_task(outcome):
        return "test"
    if "文档" in outcome or "报告" in outcome or "readme" in lowered_text:
        return "docs"
    if "修复" in outcome or "bug" in lowered_text:
        return "fix"
    if "重构" in outcome or "优化" in outcome:
        return "refactor"
    return "feat"


def _infer_commit_scope(outcome: str) -> str:
    lowered_text = outcome.lower()
    if _mentions_test_task(outcome):
        return "单元测试"
    for scope, keywords in COMMIT_SCOPE_RULES:
        if _contains_any(lowered_text, keywords):
            return scope
    path_hint = _extract_path_hint(outcome)
    if path_hint != DEFAULT_PATH_HINT:
        return _scope_from_path(path_hint)
    return "业务逻辑"


def _commit_change_type(outcome: str) -> str:
    if "拆分" in outcome or "长函数" in outcome:
        return "拆分函数"
    if "补齐" in outcome or "新增" in outcome or "添加" in outcome:
        return "新增内容"
    if "修复" in outcome:
        return "修复问题"
    if "迁移" in outcome or "升级" in outcome:
        return "迁移实现"
    if "优化" in outcome or "重构" in outcome:
        return "重构优化"
    return "完成改动"


def _extract_path_hint(text: str) -> str:
    match = PATH_PATTERN.search(text)
    if match:
        return match.group(1).strip("`")
    return DEFAULT_PATH_HINT


def _scope_from_path(path_hint: str) -> str:
    parts = [part for part in path_hint.strip("/").split("/") if part]
    if len(parts) >= 2:
        return parts[-2]
    if parts:
        return parts[0].split(".")[0]
    return "业务逻辑"


def _build_output_section(goal: _GoalFields) -> str:
    if _needs_report(goal):
        return "\n".join(
            [
                "输出文件格式：",
                "如需生成报告或文档，未指定文件名时使用 `GOAL_REPORT.md`，并采用以下结构：",
                "# 任务报告",
                "| 序号 | 范围 | 改动内容 | 验证方式 | Commit | 备注 |",
                "| --- | --- | --- | --- | --- | --- |",
                "| 1 | 路径或模块 | 实际完成的改动 | 执行过的命令或检查 | commit hash | 风险/说明 |",
            ]
        )
    return "输出文件格式：\n不需要额外报告；最终回复必须按“变更摘要、验证结果、commit 列表、未完成项、风险”五项列出。"


def _needs_report(goal: _GoalFields) -> bool:
    combined_text = " ".join([goal.outcome, goal.constraints, goal.boundaries])
    return _contains_any(combined_text, REPORT_KEYWORDS)


def _build_detail_section(goal: _GoalFields) -> str:
    if _needs_detail_rules(goal):
        return "\n".join(
            [
                "详细规则：",
                "- 先按任务边界列出候选文件或模块，再逐项判断是否符合目标结果。",
                "- 每个维度或类别都必须有对应改动、验证证据或明确的跳过理由。",
                "- 对同一文件的多个小改动可合并验证，但不同目标的改动必须分开提交。",
                "- 遇到会改变功能行为的选择时立即停下，不用猜测用户意图。",
            ]
        )
    return "详细规则：\n按用户给定边界逐项完成，不额外扩展范围；如发现多类别问题，只记录并等待用户另行确认。"


def _needs_detail_rules(goal: _GoalFields) -> bool:
    combined_text = " ".join([goal.outcome, goal.boundaries, goal.iteration])
    return _contains_any(combined_text, DETAIL_KEYWORDS)


def _suggest_branch_name(outcome: str) -> str:
    explicit_branch = _extract_explicit_branch(outcome)
    if explicit_branch:
        return explicit_branch
    prefix = _branch_prefix(outcome)
    slug = _branch_slug(outcome)
    return f"{prefix}/{slug}"


def _extract_explicit_branch(outcome: str) -> str | None:
    match = BRANCH_PATTERN.search(outcome)
    if match:
        return match.group(1).strip("`'\"")
    return None


def _branch_prefix(outcome: str) -> str:
    lowered_text = outcome.lower()
    if "修复" in outcome or "bug" in lowered_text:
        return "fix"
    if "重构" in outcome:
        return "refactor"
    if "优化" in outcome or "质量" in outcome:
        return "optimize"
    return "feature"


def _branch_slug(outcome: str) -> str:
    lowered_text = outcome.lower()
    if _mentions_test_task(outcome):
        return "add-tests"
    if "api" in lowered_text or "接口" in outcome:
        return "api-task"
    if "文档" in outcome or "报告" in outcome or "readme" in lowered_text:
        return "docs-task"
    if "迁移" in outcome or "升级" in outcome:
        return "migration-task"
    if "质量" in outcome or "优化" in outcome:
        return "code-quality"
    return DEFAULT_BRANCH_SLUG


if __name__ == "__main__":
    sys.exit(main())
