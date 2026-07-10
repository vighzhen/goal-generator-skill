# 负责分析编码任务描述并生成 Codex CLI /goal 指令纯文本。
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
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
MISSING_REASON_TEMPLATES: dict[str, str] = {
    "outcome": "描述中没有同时出现明确动作和可验收对象，执行者难以判断最终交付物。",
    "verification": "描述中缺少具体命令、检查点或验收证据，完成后无法证明没有回归。",
    "constraints": "描述中缺少不可修改内容、兼容性或依赖限制，执行者可能为了达成目标扩大改动。",
    "boundaries": "描述中缺少明确包含/排除范围，执行者无法稳定列出候选文件或模块。",
    "iteration": "描述中缺少每步粒度、验证后提交和预期 commit 节奏，长期任务难以审计和回滚。",
    "blocked": "描述中缺少允许跳过或必须停下问人的条件，执行者容易擅自猜测或偷懒跳过。",
}
MISSING_PRIORITY_ORDER: tuple[str, ...] = (
    "outcome",
    "boundaries",
    "verification",
    "constraints",
    "iteration",
    "blocked",
)
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
RISK_KEYWORD_RULES: tuple[tuple[str, int, str], ...] = (
    ("数据库", 20, "涉及数据库或持久化变更，需要明确迁移与回滚策略"),
    ("迁移", 18, "涉及迁移任务，需要更严格的边界和兼容验证"),
    ("全量", 16, "涉及全量范围，容易遗漏候选项或扩大改动"),
    ("公共 api", 15, "涉及公共 API，需确认兼容性和调用方影响"),
    ("错误码", 12, "涉及错误码或响应契约，需避免破坏外部依赖"),
    ("并发", 12, "涉及并发场景，验证面通常更复杂"),
    ("批量", 10, "涉及批量处理，需要明确跳过和审计规则"),
)
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
GOAL_SECTION_LABELS: dict[str, str] = {
    "mandatory": "强制要求：",
    "commit": "提交规则：",
    "output": "输出文件格式：",
    "detail": "详细规则：",
}
GOAL_ELEMENT_CUES: dict[str, str] = {
    "outcome": "完成以下目标：",
    "verification": "验证方式为：",
    "constraints": "约束为：",
    "boundaries": "任务边界为：",
    "iteration": "迭代策略为：",
    "blocked": "受阻停止条件为：",
}


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
        if args.list_templates:
            _emit_output(json.dumps(list_task_templates(), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.capabilities:
            _emit_output(json.dumps(build_capabilities(), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.examples:
            _emit_output(json.dumps(build_usage_examples(), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.template:
            _emit_output(json.dumps(get_task_template(args.template), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.profile:
            _emit_output(json.dumps(build_task_profile(args.profile), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.score:
            _emit_output(json.dumps(score_description(args.score), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.suggest_fields:
            _emit_output(json.dumps(suggest_goal_fields(args.suggest_fields), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.explain_missing:
            _emit_output(
                json.dumps(explain_missing_elements(args.explain_missing), ensure_ascii=False, indent=2),
                args.output_file,
            )
            return 0
        if args.questions:
            _emit_output(format_question_prompt(args.questions), args.output_file)
            return 0
        if args.analyze:
            _emit_output(json.dumps(analyze_description(args.analyze), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.validate_goal_file:
            validation = validate_goal_file(args.validate_goal_file)
            _emit_output(json.dumps(validation, ensure_ascii=False, indent=2), args.output_file)
            return 0 if validation["valid"] else 1
        if args.draft:
            _emit_output(format_draft_goal(args.draft), args.output_file)
            return 0
        if args.generate:
            _emit_output(render_goal_text(_goal_from_args(args)), args.output_file)
            return 0
        if args.interactive:
            return _run_interactive(args.output_file)
    except (OSError, ValueError) as error:
        print(f"参数错误：{error}", file=sys.stderr)
        return 2
    parser.print_help(sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="分析任务描述并生成 Codex CLI /goal 指令。")
    parser.add_argument("--analyze", help="分析用户任务描述并输出缺失要素 JSON。")
    parser.add_argument("--profile", help="识别任务类型、复杂度和推荐 6 要素模板。")
    parser.add_argument("--score", help="输出任务描述的 /goal 可执行度评分、等级和下一步建议。")
    parser.add_argument("--suggest-fields", help="从任务描述生成可编辑的 6 要素字段建议 JSON。")
    parser.add_argument("--explain-missing", help="解释缺失 6 要素的原因、优先级和可直接追问的补全建议。")
    parser.add_argument("--list-templates", action="store_true", help="列出内置任务类型模板。")
    parser.add_argument("--capabilities", action="store_true", help="输出当前单任务和批量生成能力清单 JSON。")
    parser.add_argument("--examples", action="store_true", help="输出常见单任务和批量命令示例 JSON。")
    parser.add_argument("--template", help="输出指定任务类型模板，例如 testing、bugfix、refactor、docs。")
    parser.add_argument("--questions", help="生成可直接粘贴给用户的一次性追问文本。")
    parser.add_argument("--generate", action="store_true", help="生成完整 /goal 指令文本。")
    parser.add_argument("--validate-goal-file", help="校验已有 /goal 指令文件的分隔线、5 段结构和 6 要素提示。")
    parser.add_argument("--draft", help="从一句话描述生成可编辑 /goal 草稿，缺失要素使用默认值并标注。")
    parser.add_argument("--interactive", action="store_true", help="交互式补全要素并生成 /goal 指令。")
    parser.add_argument("--from-json", help="从 JSON 文件读取 6 要素，命令行字段优先覆盖文件字段。")
    parser.add_argument("--output-file", help="把 analyze/profile/questions/generate 输出写入文件。")
    parser.add_argument("--outcome", help="Outcome（目标结果）。")
    parser.add_argument("--verification", help="Verification Surface（验证方式）。")
    parser.add_argument("--constraints", help="Constraints（约束）。")
    parser.add_argument("--boundaries", help="Boundaries（边界）。")
    parser.add_argument("--iteration", help="Iteration Policy（迭代策略）。")
    parser.add_argument("--blocked", help="Blocked Stop Condition（受阻停止条件）。")
    return parser


def _emit_output(content: str, output_file: str | None) -> None:
    if not output_file:
        print(content)
        return
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(f"{content}\n", encoding="utf-8")
    print(f"已写入：{output_path}")


def list_task_templates() -> list[dict[str, str]]:
    """列出可直接复用的任务类型模板索引。"""
    templates = [{"id": profile_id, "label": label} for profile_id, label, _keywords, _template in PROFILE_RULES]
    templates.append({"id": "generic", "label": "通用编码任务"})
    return templates


def build_capabilities() -> dict[str, object]:
    """输出当前脚本和批量工具支持的能力清单。"""
    return {
        "single_task": {
            "commands": [
                "--analyze",
                "--questions",
                "--profile",
                "--score",
                "--suggest-fields",
                "--explain-missing",
                "--list-templates",
                "--template",
                "--capabilities",
                "--examples",
                "--validate-goal-file",
                "--draft",
                "--generate",
                "--interactive",
                "--from-json",
                "--output-file",
            ],
            "template_ids": [template["id"] for template in list_task_templates()],
        },
        "batch": {
            "formats": ["json", "jsonl", "csv", "yaml", "markdown", "stdin-json", "stdin-jsonl"],
            "options": [
                "--input",
                "--stdin-format",
                "--defaults-json",
                "GOAL_GENERATOR_DEFAULTS_JSON",
                "--filter",
                "--sort-by",
                "--limit",
                "--list-tasks",
                "--profile-summary",
                "--score-summary",
                "--dedupe",
                "--check",
                "--strict",
                "--fail-on-skipped",
                "--summary-only",
                "--output-dir",
                "--output-file",
                "--report-json",
                "--report-md",
                "--missing-report-md",
                "--index-md",
                "--include-profile",
                "--verbose",
            ],
        },
        "guarantees": [
            "不引入第三方依赖",
            "保持 /goal 五段式结构和分隔线格式",
            "批量任务失败时跳过并继续处理其他任务",
        ],
    }


def build_usage_examples() -> dict[str, object]:
    """输出常见使用场景对应的命令示例。"""
    return {
        "single_task": [
            {
                "name": "分析一句话需求",
                "scenario": "用户只给出粗略编码任务，需要先确认缺哪些 6 要素。",
                "command": "python3 scripts/generate_goal.py --analyze '给项目加单元测试'",
            },
            {
                "name": "生成任务画像",
                "scenario": "追问前先判断任务类型、复杂度、风险和推荐填写方向。",
                "command": "python3 scripts/generate_goal.py --profile '修复登录 API 在空 token 场景下偶发 500 的问题'",
            },
            {
                "name": "评估可执行度",
                "scenario": "快速判断一句需求距离可直接生成高质量 /goal 还差多少。",
                "command": "python3 scripts/generate_goal.py --score '给项目加单元测试'",
            },
            {
                "name": "生成字段建议",
                "scenario": "把一句话需求变成可编辑、可机器读取的 6 要素字段草稿。",
                "command": "python3 scripts/generate_goal.py --suggest-fields '给项目加单元测试'",
            },
            {
                "name": "解释缺失要素",
                "scenario": "不只想知道缺什么，还想知道为什么缺、优先补什么和怎么补。",
                "command": "python3 scripts/generate_goal.py --explain-missing '给项目加单元测试'",
            },
            {
                "name": "从 JSON 生成 /goal",
                "scenario": "自动化系统已保存 6 要素，希望避免展开多个 CLI 参数。",
                "command": "python3 scripts/generate_goal.py --generate --from-json goal_fields.json",
            },
            {
                "name": "生成可编辑草稿",
                "scenario": "只有一句话需求时，先生成带默认填充提示的 /goal 草稿供人工复核。",
                "command": "python3 scripts/generate_goal.py --draft '给项目加单元测试'",
            },
            {
                "name": "校验已有 /goal 文件",
                "scenario": "手工编辑或批量拼接后，需要确认分隔线、5 段结构和 6 要素提示仍完整。",
                "command": "python3 scripts/generate_goal.py --validate-goal-file goal.txt",
            },
        ],
        "batch": [
            {
                "name": "批量 dry-run",
                "scenario": "从任务清单检查每个任务的要素完整度，不生成 /goal。",
                "command": "python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run",
            },
            {
                "name": "CI 校验清单",
                "scenario": "交付前阻止缺失要素任务进入执行阶段。",
                "command": "python3 scripts/batch_generate.py --input examples/sample_tasks.json --check --report-json batch_report.json",
            },
            {
                "name": "预览任务范围",
                "scenario": "配置 filter/sort/limit 后先确认将处理哪些任务。",
                "command": "python3 scripts/batch_generate.py --input examples/sample_tasks.json --list-tasks --filter '测试|Bug' --sort-by name",
            },
            {
                "name": "批量风险摘要",
                "scenario": "团队评审时快速查看任务类型、风险等级和缺失要素。",
                "command": "python3 scripts/batch_generate.py --input examples/sample_tasks.json --profile-summary --sort-by name",
            },
            {
                "name": "批量可执行度摘要",
                "scenario": "大清单评审时先按分数识别哪些任务最需要补信息。",
                "command": "python3 scripts/batch_generate.py --input examples/sample_tasks.json --score-summary --sort-by name",
            },
            {
                "name": "批量缺失要素报告",
                "scenario": "团队只想聚焦每个任务缺哪些信息、风险多高以及该怎么补齐。",
                "command": "python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --missing-report-md missing_report.md",
            },
        ],
    }


def get_task_template(template_id: str) -> dict[str, object]:
    """返回指定任务类型的 6 要素填写模板。"""
    normalized_id = template_id.strip().lower()
    for profile_id, label, _keywords, template in PROFILE_RULES:
        if normalized_id == profile_id:
            return {"id": profile_id, "label": label, "recommended_fields": template}
    if normalized_id == "generic":
        return {"id": "generic", "label": "通用编码任务", "recommended_fields": DEFAULT_PROFILE_TEMPLATE}
    known_ids = "、".join(template["id"] for template in list_task_templates())
    raise ValueError(f"未知模板：{template_id}，可选值：{known_ids}")


def validate_goal_file(goal_file: str) -> dict[str, object]:
    """校验已有 /goal 指令文件的结构完整度。"""
    goal_path = Path(goal_file)
    return validate_goal_text(goal_path.read_text(encoding="utf-8"), str(goal_path))


def validate_goal_text(goal_text: str, source: str = "") -> dict[str, object]:
    """校验 /goal 指令文本是否保留分隔线、5 段结构和 6 要素提示。"""
    separator_checks = _goal_separator_checks(goal_text)
    section_checks = _goal_section_checks(goal_text)
    element_checks = _goal_element_checks(goal_text)
    missing = _missing_goal_validation_items(separator_checks, section_checks, element_checks)
    return {
        "valid": not missing,
        "source": source,
        "checks": {
            "separators": separator_checks,
            "sections": section_checks,
            "element_cues": element_checks,
        },
        "missing": missing,
    }


def _goal_separator_checks(goal_text: str) -> dict[str, bool]:
    start_index = goal_text.find(SEPARATOR_START)
    end_index = goal_text.find(SEPARATOR_END)
    return {
        "start_separator": start_index != -1,
        "end_separator": end_index != -1,
        "ordered": start_index != -1 and end_index != -1 and start_index < end_index,
    }


def _goal_section_checks(goal_text: str) -> dict[str, bool]:
    return {
        "overview": _has_goal_command_line(goal_text),
        **{key: label in goal_text for key, label in GOAL_SECTION_LABELS.items()},
    }


def _has_goal_command_line(goal_text: str) -> bool:
    return any(line.strip().startswith("/goal ") for line in goal_text.splitlines())


def _goal_element_checks(goal_text: str) -> dict[str, bool]:
    return {key: cue in goal_text for key, cue in GOAL_ELEMENT_CUES.items()}


def _missing_goal_validation_items(
    separator_checks: dict[str, bool],
    section_checks: dict[str, bool],
    element_checks: dict[str, bool],
) -> list[str]:
    missing: list[str] = []
    missing.extend(_missing_named_checks(separator_checks, "separator"))
    missing.extend(_missing_named_checks(section_checks, "section"))
    missing.extend(_missing_named_checks(element_checks, "element_cue"))
    return missing


def _missing_named_checks(checks: dict[str, bool], group: str) -> list[str]:
    return [f"{group}.{name}" for name, passed in checks.items() if not passed]


def build_task_profile(description: str) -> dict[str, object]:
    """构建任务类型画像和 6 要素推荐模板。"""
    normalized_text = _normalize_text(description)
    analysis = analyze_description(normalized_text)
    profile_id, label, template = _infer_profile(normalized_text)
    level, reasons = _estimate_complexity(normalized_text, analysis["missing"])
    risk = _risk_assessment(normalized_text, analysis["missing"], level)
    return {
        "task_type": {"id": profile_id, "label": label},
        "complexity": {"level": level, "reasons": reasons},
        "risk": risk,
        "risk_score": risk["score"],
        "risk_level": risk["level"],
        "risk_factors": risk["factors"],
        "ask_strategy": _ask_strategy(level, analysis["missing"]),
        "missing": analysis["missing"],
        "present": analysis["present"],
        "recommended_fields": template,
    }


def score_description(description: str) -> dict[str, object]:
    """输出任务描述距离可直接生成高质量 /goal 的可执行度评分。"""
    if not description.strip():
        raise ValueError("--score 不能为空，请提供任务描述")
    analysis = analyze_description(description)
    profile = build_task_profile(description)
    risk_score = _numeric_profile_value(profile, "risk_score")
    score = _readiness_score(analysis["missing"], profile, risk_score)
    level = _readiness_level(score)
    return {
        "readiness_score": score,
        "readiness_level": level,
        "missing_count": len(analysis["missing"]),
        "missing": analysis["missing"],
        "present": analysis["present"],
        "risk_level": profile.get("risk_level", "unknown"),
        "risk_score": risk_score,
        "task_type": profile.get("task_type", {}),
        "complexity": profile.get("complexity", {}),
        "reasons": _readiness_reasons(analysis["missing"], profile, risk_score),
        "next_action": _readiness_next_action(level, analysis["missing"]),
        "recommended_command": _readiness_recommended_command(level),
    }


def _numeric_profile_value(profile: dict[str, object], key: str) -> int:
    value = profile.get(key, 0)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _readiness_score(missing: list[str], profile: dict[str, object], risk_score: int) -> int:
    missing_penalty = len(missing) * 14
    risk_penalty = min(risk_score // 4, 25)
    complexity_penalty = _complexity_penalty(profile)
    return max(0, min(100, 100 - missing_penalty - risk_penalty - complexity_penalty))


def _complexity_penalty(profile: dict[str, object]) -> int:
    complexity = profile.get("complexity", {})
    level = complexity.get("level", "low") if isinstance(complexity, dict) else "low"
    if level == "high":
        return 10
    if level == "medium":
        return 5
    return 0


def _readiness_level(score: int) -> str:
    if score >= 85:
        return "ready"
    if score >= 65:
        return "needs_review"
    if score >= 40:
        return "incomplete"
    return "high_risk"


def _readiness_reasons(missing: list[str], profile: dict[str, object], risk_score: int) -> list[str]:
    reasons: list[str] = []
    if missing:
        reasons.append(f"缺失 {len(missing)} 个必要要素：{_labels_for_keys(missing)}")
    else:
        reasons.append("6 个必要要素均已识别")
    if risk_score:
        reasons.append(f"任务风险评分为 {risk_score}，风险等级为 {profile.get('risk_level', 'unknown')}")
    complexity = profile.get("complexity", {})
    if isinstance(complexity, dict):
        level = complexity.get("level", "unknown")
        complexity_reasons = complexity.get("reasons", [])
        if isinstance(complexity_reasons, list):
            reasons.append(f"复杂度为 {level}：{'；'.join(str(reason) for reason in complexity_reasons)}")
    return reasons


def _labels_for_keys(keys: list[str]) -> str:
    return "、".join(ELEMENT_LABELS[key] for key in keys)


def _readiness_next_action(level: str, missing: list[str]) -> str:
    if level == "ready":
        return "可以进入 --generate；仍建议人工快速复核边界、验证命令和受阻条件。"
    if level == "needs_review":
        return "建议先复核风险和缺失项，再补齐少量关键信息后生成 /goal。"
    labels = _labels_for_keys(missing) if missing else "风险项"
    return f"先补齐或确认：{labels}；可使用 --explain-missing 生成可发送的追问文案。"


def _readiness_recommended_command(level: str) -> str:
    if level == "ready":
        return "python3 scripts/generate_goal.py --generate ..."
    return "python3 scripts/generate_goal.py --explain-missing '<任务描述>'"


def suggest_goal_fields(description: str) -> dict[str, object]:
    """从任务描述生成可编辑、可机器读取的 6 要素字段建议。"""
    if not description.strip():
        raise ValueError("--suggest-fields 不能为空，请提供任务描述")
    field_values = _extract_labeled_fields(description)
    analysis = analyze_description(description)
    _merge_present_fallbacks(field_values, description, analysis)
    profile = build_task_profile(description)
    recommended_fields = profile.get("recommended_fields", {})
    recommendations = recommended_fields if isinstance(recommended_fields, dict) else {}
    fields: dict[str, str] = {}
    sources: dict[str, str] = {}
    for key in ELEMENT_ORDER:
        value = field_values.get(key)
        if value:
            fields[key] = value
            sources[key] = "input_or_detected"
            continue
        fields[key] = str(recommendations.get(key, DEFAULT_PROFILE_TEMPLATE[key]))
        sources[key] = "recommended_direction"
    return {
        "fields": fields,
        "sources": sources,
        "missing": analysis["missing"],
        "present": analysis["present"],
        "task_type": profile.get("task_type", {}),
        "score": score_description(description),
        "note": "sources 为 recommended_direction 的字段是补全方向，不是用户确认后的最终事实；执行 --generate 前请按真实项目情况编辑。",
    }


def explain_missing_elements(description: str) -> dict[str, object]:
    """解释任务描述缺失哪些 /goal 要素以及如何一次性补齐。"""
    if not description.strip():
        raise ValueError("--explain-missing 不能为空，请提供任务描述")
    analysis = analyze_description(description)
    profile = build_task_profile(description)
    recommended_fields = profile.get("recommended_fields", {})
    recommendations = recommended_fields if isinstance(recommended_fields, dict) else {}
    missing = _prioritized_missing_keys(analysis["missing"])
    return {
        "description_preview": _normalize_text(description)[:TEXT_PREVIEW_LENGTH],
        "task_type": profile.get("task_type", {}),
        "risk_level": profile.get("risk_level", "unknown"),
        "risk_score": profile.get("risk_score", 0),
        "missing_count": len(missing),
        "present": analysis["present"],
        "missing_details": [
            _missing_detail(key, recommendations.get(key, DEFAULT_PROFILE_TEMPLATE[key]), index)
            for index, key in enumerate(missing, start=1)
        ],
        "ask_strategy": profile.get("ask_strategy", ""),
        "next_prompt": format_question_prompt(description),
    }


def _prioritized_missing_keys(missing: list[str]) -> list[str]:
    missing_set = set(missing)
    return [key for key in MISSING_PRIORITY_ORDER if key in missing_set]


def _missing_detail(key: str, recommendation: object, priority: int) -> dict[str, object]:
    return {
        "priority": priority,
        "element": key,
        "label": ELEMENT_LABELS[key],
        "why_missing": MISSING_REASON_TEMPLATES[key],
        "example": QUESTION_EXAMPLES[key],
        "recommended_fill": str(recommendation),
    }


def format_question_prompt(description: str) -> str:
    """把缺失要素分析结果转成可直接发送给用户的追问文本。"""
    analysis = analyze_description(description)
    if not analysis["missing"]:
        return "这份需求已经覆盖 6 个必要要素，可以直接生成 /goal 指令。"
    lines = ["你的需求还缺少以下信息，请补充：", ""]
    for index, key in enumerate(analysis["missing"], start=1):
        lines.append(f"{index}. {ELEMENT_LABELS[key]}：{QUESTION_EXAMPLES[key]}")
    lines.extend(["", "你可以直接简短回答，我会帮你补全成完整指令。"])
    return "\n".join(lines)


def format_draft_goal(description: str) -> str:
    """从一句话描述生成带默认填充提示的 /goal 草稿。"""
    if not description.strip():
        raise ValueError("--draft 不能为空，请提供任务描述")
    field_values = _extract_labeled_fields(description)
    analysis = analyze_description(description)
    _merge_present_fallbacks(field_values, description, analysis)
    defaulted_keys = _apply_interactive_defaults(field_values)
    lines: list[str] = []
    if defaulted_keys:
        labels = "、".join(ELEMENT_LABELS[key] for key in defaulted_keys)
        lines.append(f"默认填充：{labels}。请在执行前按实际项目情况复核这些字段。")
    lines.append(render_goal_text(_fields_from_mapping(field_values)))
    return "\n".join(lines)


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


def _risk_assessment(text: str, missing: list[str], complexity_level: str) -> dict[str, object]:
    factors: list[str] = []
    score = _missing_risk_score(missing, factors) + _complexity_risk_score(complexity_level, factors)
    score += _keyword_risk_score(text, factors)
    bounded_score = min(score, 100)
    return {
        "score": bounded_score,
        "level": _risk_level(bounded_score),
        "factors": factors or ["未发现明显高风险信号"],
    }


def _missing_risk_score(missing: list[str], factors: list[str]) -> int:
    if not missing:
        return 0
    labels = "、".join(ELEMENT_LABELS[key] for key in missing)
    factors.append(f"缺失 {len(missing)} 个必要要素：{labels}")
    return min(len(missing) * 8, 40)


def _complexity_risk_score(level: str, factors: list[str]) -> int:
    if level == "high":
        factors.append("复杂度为 high，需要更完整的任务边界和验证面")
        return 25
    if level == "medium":
        factors.append("复杂度为 medium，建议补充路径、命令或数量")
        return 12
    return 0


def _keyword_risk_score(text: str, factors: list[str]) -> int:
    lowered_text = text.lower()
    score = 0
    for keyword, value, reason in RISK_KEYWORD_RULES:
        if keyword in lowered_text:
            score += value
            factors.append(reason)
    return score


def _risk_level(score: int) -> str:
    if score >= 70:
        return "high"
    if score >= 35:
        return "medium"
    return "low"


def _goal_from_args(args: argparse.Namespace) -> _GoalFields:
    field_values = _goal_values_from_json(args.from_json)
    for key in ELEMENT_ORDER:
        value = getattr(args, key)
        if isinstance(value, str) and value.strip():
            field_values[key] = value.strip()
    return _GoalFields(
        outcome=_required_field_value(field_values, "outcome"),
        verification=_required_field_value(field_values, "verification"),
        constraints=_required_field_value(field_values, "constraints"),
        boundaries=_required_field_value(field_values, "boundaries"),
        iteration=_required_field_value(field_values, "iteration"),
        blocked=_required_field_value(field_values, "blocked"),
    )


def _goal_values_from_json(json_path: str | None) -> dict[str, str]:
    if not json_path:
        return {}
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    raw_fields = data.get("fields", data) if isinstance(data, dict) else data
    if not isinstance(raw_fields, dict):
        raise ValueError("--from-json 必须指向 JSON 对象或包含 fields 对象的 JSON 文件")
    return {
        key: str(raw_fields.get(key)).strip()
        for key in ELEMENT_ORDER
        if raw_fields.get(key) is not None and str(raw_fields.get(key)).strip()
    }


def _run_interactive(output_file: str | None = None) -> int:
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
                _emit_output(render_goal_text(_fields_from_mapping(field_values)), output_file)
                return 0
            _print_missing_questions(missing)
            supplement = input("请一次性补充以上信息：").strip()
            if supplement:
                combined_text = f"{combined_text}\n{supplement}"
                field_values.update(_extract_labeled_fields(supplement))
        _merge_present_fallbacks(field_values, combined_text, analyze_description(combined_text))
        defaulted_keys = _apply_interactive_defaults(field_values)
        _print_default_notice(defaulted_keys)
        _emit_output(render_goal_text(_fields_from_mapping(field_values)), output_file)
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


def _required_field_value(field_values: dict[str, str], field_name: str) -> str:
    value = field_values.get(field_name)
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
