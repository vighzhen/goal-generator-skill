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
REPORT_KEYWORDS: tuple[str, ...] = ("报告", "文档", "README", "说明", "审计")
DETAIL_KEYWORDS: tuple[str, ...] = ("维度", "规则", "类别", "批量", "重构", "优化", "迁移")
PATH_PATTERN = re.compile(r"(?:^|\s|`)([\w./-]+/[\w./-]*|[\w.-]+\.[A-Za-z0-9]+)")
NUMBER_PATTERN = re.compile(r"\d+")
BRANCH_PATTERN = re.compile(r"(?:分支|branch)\s*[`'\"]?([A-Za-z0-9._/-]+)")
COMMIT_RANGE_PATTERN = re.compile(r"\d+\s*[-~—]\s*\d+\s*个?\s*commit", re.IGNORECASE)
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
        if args.analyze:
            print(json.dumps(analyze_description(args.analyze), ensure_ascii=False, indent=2))
            return 0
        if args.generate:
            print(render_goal_text(_goal_from_args(args)))
            return 0
    except ValueError as error:
        print(f"参数错误：{error}", file=sys.stderr)
        return 2
    parser.print_help(sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="分析任务描述并生成 Codex CLI /goal 指令。")
    parser.add_argument("--analyze", help="分析用户任务描述并输出缺失要素 JSON。")
    parser.add_argument("--generate", action="store_true", help="生成完整 /goal 指令文本。")
    parser.add_argument("--outcome", help="Outcome（目标结果）。")
    parser.add_argument("--verification", help="Verification Surface（验证方式）。")
    parser.add_argument("--constraints", help="Constraints（约束）。")
    parser.add_argument("--boundaries", help="Boundaries（边界）。")
    parser.add_argument("--iteration", help="Iteration Policy（迭代策略）。")
    parser.add_argument("--blocked", help="Blocked Stop Condition（受阻停止条件）。")
    return parser


def _goal_from_args(args: argparse.Namespace) -> _GoalFields:
    return _GoalFields(
        outcome=_required_value(args, "outcome"),
        verification=_required_value(args, "verification"),
        constraints=_required_value(args, "constraints"),
        boundaries=_required_value(args, "boundaries"),
        iteration=_required_value(args, "iteration"),
        blocked=_required_value(args, "blocked"),
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
    return any(keyword.lower() in text for keyword in keywords)


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
    examples = _commit_examples(goal.outcome)
    lines = [
        "提交规则：",
        f"- 预期提交数量为 {commit_range}；如果实际偏离，必须在最终回复解释原因。",
        "- 每个独立改动完成后立即运行对应验证，再执行 `git add` 和 `git commit`。",
        "- Commit message 使用 `<type>(goal): <简短说明>` 格式。",
        f"- 示例：`{examples[0]}`；`{examples[1]}`；`{examples[2]}`。",
        f"- 全部完成并验证通过后执行 `git checkout main && git merge {branch_name}`。",
        "- 合并完成后执行 `git push -u origin main`；如果用户明确要求不推送，则记录跳过原因。",
    ]
    return "\n".join(lines)


def _expected_commit_range(iteration: str) -> str:
    match = COMMIT_RANGE_PATTERN.search(iteration)
    if match:
        return match.group(0)
    return DEFAULT_COMMIT_RANGE


def _commit_examples(outcome: str) -> list[str]:
    commit_type = _infer_commit_type(outcome)
    scope = _infer_commit_scope(outcome)
    return [
        f"{commit_type}({scope}): 完成首个独立改动",
        f"{commit_type}({scope}): 完成下一组范围内改动",
        f"chore({scope}): 补充最终验证记录",
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
    if "api" in outcome.lower() or "接口" in outcome:
        return "api"
    if _mentions_test_task(outcome):
        return "tests"
    if "文档" in outcome or "报告" in outcome:
        return "docs"
    if "质量" in outcome or "优化" in outcome:
        return "quality"
    return "task"


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
