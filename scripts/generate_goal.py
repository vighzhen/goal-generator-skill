# 负责分析编码任务描述并生成 Codex CLI /goal 指令纯文本。
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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
    "implement",
    "add",
    "create",
    "fix",
    "optimize",
    "refactor",
    "migrate",
    "upgrade",
    "generate",
    "cover",
    "develop",
    "build",
    "write",
    "update",
    "remove",
    "replace",
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
    "endpoint",
    "module",
    "page",
    "service",
    "directory",
    "folder",
    "file",
    "function",
    "class",
    "report",
    "coverage",
    "docs",
    "tests",
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
    "verify",
    "validate",
    "run",
    "execute",
    "check",
    "pass",
    "pytest",
    "unittest",
    "run tests",
    "npm test",
    "make test",
)
CONSTRAINT_KEYWORDS: tuple[str, ...] = (
    "不修改",
    "不改",
    "不改变",
    "不引入",
    "不做",
    "禁止",
    "不能",
    "不得",
    "保持",
    "兼容",
    "约束",
    "do not",
    "don't",
    "must not",
    "without",
    "keep",
    "preserve",
    "compatible",
    "compatibility",
    "no new",
    "avoid",
    "forbid",
    "constraint",
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
    "only",
    "scope",
    "within",
    "under",
    "include",
    "exclude",
    "except",
    "directory",
    "folder",
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
    "each",
    "per",
    "one by one",
    "batch",
    "iteration",
    "commit",
    "after validation",
    "after verifying",
    "expected",
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
    "blocked",
    "blocker",
    "stop",
    "ask me",
    "ask user",
    "skip",
    "unable",
    "unclear",
    "missing",
    "manual",
    "no more than",
    "cannot infer",
)
NEGATION_PREFIXES: tuple[str, ...] = ("非", "不", "没有", "无需", "无", "non-", "not", "no", "without")
TEST_TOOL_KEYWORDS: tuple[str, ...] = ("pytest", "unittest", "unit test", "tests", "test suite", "coverage")
REPORT_KEYWORDS: tuple[str, ...] = ("报告", "文档", "readme", "report", "说明", "审计", "docs", "documentation", "guide")
DETAIL_KEYWORDS: tuple[str, ...] = (
    "维度",
    "规则",
    "类别",
    "批量",
    "重构",
    "优化",
    "迁移",
    "dimension",
    "rule",
    "category",
    "batch",
    "refactor",
    "optimize",
    "migration",
)
COMMIT_SCOPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("错误处理", ("错误处理", "异常", "错误码", "失败路径", "error handling", "exception", "error code", "failure path")),
    ("类型注解", ("类型注解", "类型检查", "typing", "mypy", "type hint", "type check")),
    ("结构设计", ("结构设计", "结构", "拆分", "长函数", "架构", "architecture", "split", "long function")),
    ("代码质量", ("代码质量", "质量", "优化", "坏味道", "code quality", "quality", "optimize", "cleanup")),
    ("接口", ("接口", "api", "endpoint")),
    ("文档", ("文档", "报告", "readme", "docs", "documentation", "report")),
    ("数据迁移", ("迁移", "升级", "migration", "upgrade")),
)
PROFILE_RULES: tuple[tuple[str, str, tuple[str, ...], dict[str, str]], ...] = (
    (
        "testing",
        "测试编写",
        ("测试", "pytest", "unittest", "覆盖率", "用例", "tests", "unit test", "test suite", "coverage"),
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
        ("修复", "bug", "错误", "报错", "异常", "500", "fix", "error", "exception", "failure"),
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
        ("优化", "重构", "质量", "坏味道", "复杂度", "optimize", "refactor", "quality", "cleanup", "complexity"),
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
        ("文档", "报告", "readme", "说明", "docs", "documentation", "report", "guide"),
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
    ("database", 20, "涉及数据库或持久化变更，需要明确迁移与回滚策略"),
    ("迁移", 18, "涉及迁移任务，需要更严格的边界和兼容验证"),
    ("migration", 18, "涉及迁移任务，需要更严格的边界和兼容验证"),
    ("全量", 16, "涉及全量范围，容易遗漏候选项或扩大改动"),
    ("full", 16, "涉及全量范围，容易遗漏候选项或扩大改动"),
    ("公共 api", 15, "涉及公共 API，需确认兼容性和调用方影响"),
    ("public api", 15, "涉及公共 API，需确认兼容性和调用方影响"),
    ("错误码", 12, "涉及错误码或响应契约，需避免破坏外部依赖"),
    ("error code", 12, "涉及错误码或响应契约，需避免破坏外部依赖"),
    ("并发", 12, "涉及并发场景，验证面通常更复杂"),
    ("concurrency", 12, "涉及并发场景，验证面通常更复杂"),
    ("批量", 10, "涉及批量处理，需要明确跳过和审计规则"),
    ("batch", 10, "涉及批量处理，需要明确跳过和审计规则"),
)
INSPECT_SKIP_DIRS: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "target",
    "vendor",
)
INSPECT_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".swift": "Swift",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".c": "C/C++",
    ".cc": "C/C++",
    ".cpp": "C/C++",
    ".h": "C/C++",
    ".hpp": "C/C++",
    ".md": "Markdown",
    ".rst": "reStructuredText",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".sh": "Shell",
}
INSPECT_TEST_NAME_PATTERNS: tuple[str, ...] = (
    "test_",
    "_test.",
    ".test.",
    ".spec.",
    "tests/",
    "__tests__/",
    "spec/",
)
INSPECT_MAX_FILES = 120
INSPECT_SAMPLE_LIMIT = 20
INSPECT_VERIFICATION_SAMPLE_LIMIT = 12
PROJECT_CONFIG_SEARCH_DEPTH = 5
PROJECT_VALIDATION_COMMAND_LIMIT = 12
PROJECT_CONFIG_FILENAMES: tuple[str, ...] = (
    "package.json",
    "Makefile",
    "makefile",
    "GNUmakefile",
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)
PACKAGE_SCRIPT_PRIORITY: tuple[str, ...] = (
    "test",
    "test:unit",
    "test:ci",
    "test:coverage",
    "lint",
    "typecheck",
    "type-check",
    "build",
)
MAKE_TARGET_PRIORITY: tuple[str, ...] = ("test", "check", "lint", "typecheck", "type-check", "unit", "build", "ci")
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
CONTEXT_CLAUSE_SPLIT_PATTERN = re.compile(r"[，。；;\n]+")
CONTEXT_FALLBACK_HINTS: dict[str, tuple[str, ...]] = {
    "verification": ("验证", "运行", "执行", "确认", "检查", "通过", "跑测试", "pytest", "unittest", "run", "verify", "test"),
    "constraints": ("不改", "不修改", "不改变", "不引入", "禁止", "不得", "保持", "兼容", "do not", "don't", "keep", "without"),
    "boundaries": ("边界", "范围", "仅", "只", "目录", "排除", "src/", "tests/", "only", "scope", "within", "exclude"),
    "iteration": ("迭代", "每个", "每次", "逐个", "commit", "提交", "预期", "each", "per", "after validation"),
    "blocked": ("受阻", "阻塞", "停下", "问人", "问我", "跳过", "无法", "缺少", "ask me", "stop", "skip", "blocked"),
}
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
SENSITIVE_VALUE_PATTERNS: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    (
        "private_key",
        "critical",
        "疑似私钥块，必须移除或替换为占位符后再共享",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.IGNORECASE | re.DOTALL),
    ),
    (
        "secret_assignment",
        "high",
        "疑似 token/key/password 赋值，建议改成 <SECRET> 占位符",
        re.compile(
            r"(?i)\b(?:api[_-]?key|token|secret|password|passwd|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:@-]{8,}",
        ),
    ),
    (
        "bearer_token",
        "high",
        "疑似 Bearer token，建议删除认证头或替换为 <TOKEN>",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{10,}"),
    ),
    (
        "url",
        "medium",
        "包含 URL，若为内网、工单、仓库或带参数链接，建议确认可共享或脱敏",
        re.compile(r"https?://[^\s)>\]\"]+"),
    ),
    (
        "email",
        "medium",
        "包含邮箱地址，建议确认是否可共享或替换为联系人角色",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
)
SENSITIVE_SEVERITY_SCORES: dict[str, int] = {"critical": 70, "high": 40, "medium": 20, "low": 10}


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
        if args.template:
            _emit_output(json.dumps(get_task_template(args.template), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.profile:
            _emit_output(json.dumps(build_task_profile(args.profile), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.redaction_check:
            _emit_output(json.dumps(check_redaction(args.redaction_check), ensure_ascii=False, indent=2), args.output_file)
            return 0
        if args.inspect_path:
            _emit_output(
                json.dumps(inspect_path_context(args.inspect_path, args.path_task or ""), ensure_ascii=False, indent=2),
                args.output_file,
            )
            return 0
        if args.merge_context:
            _emit_output(
                json.dumps(merge_task_context(args.merge_context, args.supplement or []), ensure_ascii=False, indent=2),
                args.output_file,
            )
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
        if args.lint_goal_file:
            lint_report = lint_goal_file(args.lint_goal_file)
            _emit_output(json.dumps(lint_report, ensure_ascii=False, indent=2), args.output_file)
            return 0 if lint_report["passed"] else 1
        if args.lint_goal_bundle:
            lint_report = lint_goal_bundle(args.lint_goal_bundle)
            _emit_output(json.dumps(lint_report, ensure_ascii=False, indent=2), args.output_file)
            return 0 if lint_report["passed"] else 1
        if args.lint_goal_dir:
            lint_report = lint_goal_dir(args.lint_goal_dir)
            _emit_output(json.dumps(lint_report, ensure_ascii=False, indent=2), args.output_file)
            return 0 if lint_report["passed"] else 1
        if args.lint_goal_tree:
            lint_report = lint_goal_tree(args.lint_goal_tree)
            _emit_output(json.dumps(lint_report, ensure_ascii=False, indent=2), args.output_file)
            return 0 if lint_report["passed"] else 1
        if args.validate_fields_json:
            validation = validate_fields_json_file(args.validate_fields_json)
            _emit_output(json.dumps(validation, ensure_ascii=False, indent=2), args.output_file)
            return 0 if validation["valid"] else 1
        if args.lint_fields_json:
            lint_report = lint_fields_json_file(args.lint_fields_json)
            _emit_output(json.dumps(lint_report, ensure_ascii=False, indent=2), args.output_file)
            return 0 if lint_report["passed"] else 1
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
    parser.add_argument("description", nargs="?", help="配合 --generate 使用的一句话任务描述。")
    parser.add_argument("--analyze", help="分析用户任务描述并输出缺失要素 JSON。")
    parser.add_argument("--profile", help="识别任务类型、复杂度和推荐 6 要素模板。")
    parser.add_argument("--redaction-check", help="检查任务描述中疑似 token、密钥、邮箱或 URL 等敏感信息，并输出脱敏预览 JSON。")
    parser.add_argument("--inspect-path", help="扫描本地文件或目录，输出代码上下文、边界和验证命令建议 JSON。")
    parser.add_argument("--path-task", help="配合 --inspect-path 提供用户目标描述，用于生成更贴近场景的建议字段。")
    parser.add_argument("--merge-context", help="合并原始任务描述和补充回答，输出可生成 /goal 的字段草稿 JSON。")
    parser.add_argument("--supplement", action="append", help="配合 --merge-context 提供一条补充回答，可重复传入。")
    parser.add_argument("--explain-missing", help="解释缺失 6 要素的原因、优先级和可直接追问的补全建议。")
    parser.add_argument("--list-templates", action="store_true", help="列出内置任务类型模板。")
    parser.add_argument("--template", help="输出指定任务类型模板，例如 testing、bugfix、refactor、docs。")
    parser.add_argument("--questions", help="生成可直接粘贴给用户的一次性追问文本。")
    parser.add_argument("--generate", action="store_true", help="生成完整 /goal 指令文本。")
    parser.add_argument("--validate-goal-file", help="校验已有 /goal 指令文件的分隔线、5 段结构和 6 要素提示。")
    parser.add_argument("--lint-goal-file", help="检查已有 /goal 指令文件的结构和 6 要素语义质量。")
    parser.add_argument("--lint-goal-bundle", help="逐段检查同一文件内多个 .txt /goal 文本的结构和 6 要素语义质量。")
    parser.add_argument("--lint-goal-dir", help="批量检查目录内 .txt /goal 文件的结构和 6 要素语义质量。")
    parser.add_argument("--lint-goal-tree", help="递归检查目录树内 .txt /goal 文件的结构和 6 要素语义质量。")
    parser.add_argument("--validate-fields-json", help="校验 6 要素 JSON 是否可用于 --generate --from-json。")
    parser.add_argument("--lint-fields-json", help="检查 6 要素 JSON 的语义质量、具体性和可执行性。")
    parser.add_argument("--interactive", action="store_true", help="交互式补全要素并生成 /goal 指令。")
    parser.add_argument("--from-json", help="从 JSON 文件读取 6 要素，命令行字段优先覆盖文件字段。")
    parser.add_argument("--output-file", help="把 analyze/profile/questions/generate 输出写入文件。")
    parser.add_argument("--branch", help="配合 --generate 简写指定目标分支名。")
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


def lint_goal_file(goal_file: str) -> dict[str, object]:
    """校验已有 /goal 指令文件的结构和 6 要素语义质量。"""
    goal_path = Path(goal_file)
    return lint_goal_text(goal_path.read_text(encoding="utf-8"), str(goal_path))


def lint_goal_bundle(goal_file: str) -> dict[str, object]:
    """逐段校验同一文本文件中的多个 /goal 指令。"""
    goal_path = Path(goal_file)
    if not goal_path.exists():
        raise ValueError(f"--lint-goal-bundle 不存在：{goal_file}")
    if not goal_path.is_file():
        raise ValueError(f"--lint-goal-bundle 必须是文件：{goal_file}")
    lines = goal_path.read_text(encoding="utf-8").splitlines()
    blocks, bundle_issues = _goal_bundle_blocks(lines)
    goal_reports = [_goal_bundle_block_report(goal_path, block) for block in blocks]
    failed_reports = [report for report in goal_reports if not report["passed"]]
    passed = bool(blocks) and not bundle_issues and not failed_reports
    return {
        "passed": passed,
        "source": str(goal_path),
        "goal_count": len(blocks),
        "passed_count": len(blocks) - len(failed_reports),
        "failed_count": len(failed_reports),
        "bundle_issue_count": len(bundle_issues),
        "bundle_issues": bundle_issues,
        "goals": goal_reports,
        "summary": _goal_bundle_lint_summary(passed, len(blocks), len(failed_reports), len(bundle_issues)),
    }


def lint_goal_dir(goal_dir: str) -> dict[str, object]:
    """批量校验目录内已有 /goal 指令文件的结构和语义质量。"""
    directory = Path(goal_dir)
    if not directory.exists():
        raise ValueError(f"--lint-goal-dir 不存在：{goal_dir}")
    if not directory.is_dir():
        raise ValueError(f"--lint-goal-dir 必须是目录：{goal_dir}")
    goal_files = _goal_dir_files(directory)
    file_reports = [_goal_dir_file_report(goal_file) for goal_file in goal_files]
    failed_reports = [report for report in file_reports if not report["passed"]]
    passed = bool(goal_files) and not failed_reports
    return {
        "passed": passed,
        "source": str(directory),
        "file_count": len(goal_files),
        "passed_count": len(goal_files) - len(failed_reports),
        "failed_count": len(failed_reports),
        "files": file_reports,
        "summary": _goal_dir_lint_summary(passed, len(goal_files), len(failed_reports)),
    }


def lint_goal_tree(goal_dir: str) -> dict[str, object]:
    """递归校验目录树内已有 /goal 指令文件的结构和语义质量。"""
    directory = Path(goal_dir)
    if not directory.exists():
        raise ValueError(f"--lint-goal-tree 不存在：{goal_dir}")
    if not directory.is_dir():
        raise ValueError(f"--lint-goal-tree 必须是目录：{goal_dir}")
    goal_files, skipped_directories = _goal_tree_files(directory)
    file_reports = [_goal_tree_file_report(directory, goal_file) for goal_file in goal_files]
    failed_reports = [report for report in file_reports if not report["passed"]]
    passed = bool(goal_files) and not failed_reports
    return {
        "passed": passed,
        "source": str(directory),
        "mode": "recursive",
        "file_count": len(goal_files),
        "passed_count": len(goal_files) - len(failed_reports),
        "failed_count": len(failed_reports),
        "skipped_directory_count": len(skipped_directories),
        "skipped_directories": [_goal_tree_relative_path(directory, path) for path in skipped_directories],
        "files": file_reports,
        "summary": _goal_tree_lint_summary(passed, len(goal_files), len(failed_reports)),
    }


def _goal_dir_files(directory: Path) -> list[Path]:
    return sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".txt"),
        key=lambda path: path.name,
    )


def _goal_tree_files(directory: Path) -> tuple[list[Path], list[Path]]:
    goal_files: list[Path] = []
    skipped_directories: list[Path] = []
    for current_root, dirnames, filenames in os.walk(directory):
        current_path = Path(current_root)
        kept_dirnames: list[str] = []
        for dirname in sorted(dirnames):
            if dirname in INSPECT_SKIP_DIRS:
                skipped_directories.append(current_path / dirname)
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames
        for filename in sorted(filenames):
            file_path = current_path / filename
            if file_path.is_file() and file_path.suffix.lower() == ".txt":
                goal_files.append(file_path)
    return (
        sorted(goal_files, key=lambda path: _goal_tree_relative_path(directory, path)),
        sorted(skipped_directories, key=lambda path: _goal_tree_relative_path(directory, path)),
    )


def _goal_bundle_blocks(lines: list[str]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    blocks: list[dict[str, object]] = []
    issues: list[dict[str, object]] = []
    start_line: int | None = None
    for line_number, line in enumerate(lines, start=1):
        stripped_line = line.strip()
        if stripped_line == SEPARATOR_START:
            if start_line is not None:
                issues.append(
                    _goal_bundle_issue(
                        line_number,
                        "发现新的开始分隔线，但上一个 /goal 块尚未结束",
                        "检查是否遗漏了结束分隔线，或把嵌套/重复分隔线移除。",
                    )
                )
            start_line = line_number
            continue
        if stripped_line != SEPARATOR_END:
            continue
        if start_line is None:
            issues.append(
                _goal_bundle_issue(
                    line_number,
                    "发现结束分隔线，但此前没有对应的开始分隔线",
                    "移除多余结束分隔线，或补齐该块的开始分隔线。",
                )
            )
            continue
        blocks.append(_goal_bundle_block(lines, len(blocks) + 1, start_line, line_number))
        start_line = None
    if start_line is not None:
        issues.append(
            _goal_bundle_issue(
                start_line,
                "发现开始分隔线，但文件结束前没有对应的结束分隔线",
                "补齐该 /goal 块的结束分隔线，或删除不完整块。",
            )
        )
    return blocks, issues


def _goal_bundle_issue(line: int, message: str, suggestion: str) -> dict[str, object]:
    return {"line": line, "message": message, "suggestion": suggestion}


def _goal_bundle_block(lines: list[str], goal_index: int, start_line: int, end_line: int) -> dict[str, object]:
    return {
        "goal_index": goal_index,
        "task_name": _goal_bundle_task_name(lines, start_line),
        "start_line": start_line,
        "end_line": end_line,
        "text": "\n".join(lines[start_line - 1 : end_line]),
    }


def _goal_bundle_task_name(lines: list[str], start_line: int) -> str:
    search_start = start_line - 2
    search_stop = max(-1, start_line - 8)
    for line_index in range(search_start, search_stop, -1):
        stripped_line = lines[line_index].strip()
        if stripped_line.startswith("任务："):
            return stripped_line.split("：", 1)[1].strip()
        if stripped_line.startswith("任务:"):
            return stripped_line.split(":", 1)[1].strip()
        lowered_line = stripped_line.lower()
        if lowered_line.startswith("task:"):
            return stripped_line.split(":", 1)[1].strip()
    return ""


def _goal_bundle_block_report(goal_path: Path, block: dict[str, object]) -> dict[str, object]:
    goal_index = int(block["goal_index"])
    source = f"{goal_path}#goal-{goal_index}"
    lint_report = lint_goal_text(str(block["text"]), source)
    field_lint = lint_report.get("field_lint", {})
    validation = lint_report.get("validation", {})
    issues = field_lint.get("issues", []) if isinstance(field_lint, dict) else []
    return {
        "goal_index": goal_index,
        "task_name": block.get("task_name", ""),
        "start_line": block["start_line"],
        "end_line": block["end_line"],
        "passed": lint_report["passed"],
        "validation_valid": validation.get("valid", False) if isinstance(validation, dict) else False,
        "score": field_lint.get("score", 0) if isinstance(field_lint, dict) else 0,
        "issue_count": field_lint.get("issue_count", 0) if isinstance(field_lint, dict) else 0,
        "high_issue_count": field_lint.get("high_issue_count", 0) if isinstance(field_lint, dict) else 0,
        "missing": validation.get("missing", []) if isinstance(validation, dict) else [],
        "issues": issues if isinstance(issues, list) else [],
        "summary": lint_report["summary"],
    }


def _goal_dir_file_report(goal_file: Path) -> dict[str, object]:
    lint_report = lint_goal_text(goal_file.read_text(encoding="utf-8"), str(goal_file))
    field_lint = lint_report.get("field_lint", {})
    validation = lint_report.get("validation", {})
    issues = field_lint.get("issues", []) if isinstance(field_lint, dict) else []
    return {
        "path": str(goal_file),
        "passed": lint_report["passed"],
        "validation_valid": validation.get("valid", False) if isinstance(validation, dict) else False,
        "score": field_lint.get("score", 0) if isinstance(field_lint, dict) else 0,
        "issue_count": field_lint.get("issue_count", 0) if isinstance(field_lint, dict) else 0,
        "high_issue_count": field_lint.get("high_issue_count", 0) if isinstance(field_lint, dict) else 0,
        "missing": validation.get("missing", []) if isinstance(validation, dict) else [],
        "issues": issues if isinstance(issues, list) else [],
        "summary": lint_report["summary"],
    }


def _goal_tree_file_report(root: Path, goal_file: Path) -> dict[str, object]:
    report = _goal_dir_file_report(goal_file)
    report["relative_path"] = _goal_tree_relative_path(root, goal_file)
    return report


def _goal_tree_relative_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _goal_dir_lint_summary(passed: bool, file_count: int, failed_count: int) -> str:
    if not file_count:
        return "目录内未发现可检查的 .txt /goal 文件。"
    if passed:
        return f"/goal 目录语义质量检查通过，共 {file_count} 个文件。"
    return f"/goal 目录语义质量检查未通过：失败 {failed_count} 个，总计 {file_count} 个文件。"


def _goal_tree_lint_summary(passed: bool, file_count: int, failed_count: int) -> str:
    if not file_count:
        return "目录树内未发现可检查的 .txt /goal 文件。"
    if passed:
        return f"/goal 目录树递归语义质量检查通过，共 {file_count} 个文件。"
    return f"/goal 目录树递归语义质量检查未通过：失败 {failed_count} 个，总计 {file_count} 个文件。"


def _goal_bundle_lint_summary(passed: bool, goal_count: int, failed_count: int, bundle_issue_count: int) -> str:
    if not goal_count:
        return "合集文件内未发现完整的 /goal 分隔块。"
    if passed:
        return f"/goal 合集文件语义质量检查通过，共 {goal_count} 个块。"
    return (
        f"/goal 合集文件语义质量检查未通过：失败 {failed_count} 个，"
        f"分隔线问题 {bundle_issue_count} 个，总计 {goal_count} 个块。"
    )


def lint_goal_text(goal_text: str, source: str = "") -> dict[str, object]:
    """从 /goal 文本抽取 6 要素并复用字段语义质量门禁。"""
    validation = validate_goal_text(goal_text, source)
    extracted_fields = _extract_goal_fields(goal_text)
    field_lint = lint_fields_json_data({"fields": extracted_fields}, source)
    passed = bool(validation["valid"]) and bool(field_lint["passed"])
    return {
        "passed": passed,
        "source": source,
        "validation": validation,
        "extracted_fields": extracted_fields,
        "field_lint": field_lint,
        "summary": _goal_lint_summary(passed, validation, field_lint),
    }


def _extract_goal_fields(goal_text: str) -> dict[str, str]:
    overview_text = _goal_overview_text(goal_text)
    fields: dict[str, str] = {}
    for key in ELEMENT_ORDER:
        value = _extract_goal_field_value(overview_text, key)
        if value:
            fields[key] = value
    return fields


def _goal_overview_text(goal_text: str) -> str:
    for line in goal_text.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith("/goal "):
            return stripped_line
    return _normalize_text(goal_text)


def _extract_goal_field_value(overview_text: str, key: str) -> str:
    cue = GOAL_ELEMENT_CUES[key]
    start_index = overview_text.find(cue)
    if start_index == -1:
        return ""
    value_start = start_index + len(cue)
    end_index = _goal_field_end_index(overview_text, key, value_start)
    return overview_text[value_start:end_index].strip(" 。，；;,.")


def _goal_field_end_index(overview_text: str, key: str, value_start: int) -> int:
    current_index = ELEMENT_ORDER.index(key)
    candidates = [
        index
        for next_key in ELEMENT_ORDER[current_index + 1 :]
        if (index := overview_text.find(GOAL_ELEMENT_CUES[next_key], value_start)) != -1
    ]
    final_marker_index = overview_text.find("全过程必须", value_start)
    if final_marker_index != -1:
        candidates.append(final_marker_index)
    return min(candidates) if candidates else len(overview_text)


def _goal_lint_summary(passed: bool, validation: dict[str, object], field_lint: dict[str, object]) -> str:
    if passed:
        return f"/goal 文件结构和语义质量检查通过，得分 {field_lint.get('score', 0)}。"
    structure_state = "结构通过" if validation.get("valid") else "结构未通过"
    return f"/goal 文件检查未通过：{structure_state}，语义得分 {field_lint.get('score', 0)}。"


def validate_fields_json_file(fields_file: str) -> dict[str, object]:
    """校验 6 要素 JSON 文件是否适合交给 --generate --from-json。"""
    fields_path = Path(fields_file)
    data = json.loads(fields_path.read_text(encoding="utf-8"))
    return validate_fields_json_data(data, str(fields_path))


def lint_fields_json_file(fields_file: str) -> dict[str, object]:
    """检查 6 要素 JSON 的语义质量，而不只检查结构完整性。"""
    fields_path = Path(fields_file)
    data = json.loads(fields_path.read_text(encoding="utf-8"))
    return lint_fields_json_data(data, str(fields_path))


def lint_fields_json_data(data: object, source: str = "") -> dict[str, object]:
    """对直接字段对象或 fields 包装对象做语义质量检查。"""
    validation = validate_fields_json_data(data, source)
    fields = _lintable_fields_from_data(data)
    issues = _validation_lint_issues(validation)
    for key in ELEMENT_ORDER:
        issues.extend(_semantic_lint_issues(key, fields.get(key, "")))
    score = _lint_score(issues)
    high_issue_count = sum(1 for issue in issues if issue["severity"] == "high")
    passed = bool(validation["valid"]) and high_issue_count == 0 and score >= 80
    return {
        "passed": passed,
        "source": source,
        "score": score,
        "issue_count": len(issues),
        "high_issue_count": high_issue_count,
        "issues": issues,
        "validation": validation,
        "summary": _lint_summary(passed, score, issues),
    }


def _lintable_fields_from_data(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    raw_fields = data.get("fields", data)
    if not isinstance(raw_fields, dict):
        return {}
    return {
        key: str(raw_fields.get(key)).strip()
        for key in ELEMENT_ORDER
        if raw_fields.get(key) is not None and str(raw_fields.get(key)).strip()
    }


def _validation_lint_issues(validation: dict[str, object]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for key in _object_list(validation.get("missing_fields")):
        issues.append(_lint_issue(key, "high", "缺少必填要素", f"补齐 {ELEMENT_LABELS[key]} 后再生成 /goal。"))
    for key in _object_list(validation.get("empty_fields")):
        issues.append(_lint_issue(key, "high", "要素为空", f"为 {ELEMENT_LABELS[key]} 填写非空内容。"))
    unknown_fields = _object_list(validation.get("unknown_fields"))
    if unknown_fields:
        issues.append(_lint_issue("fields", "medium", f"存在未知字段：{'、'.join(unknown_fields)}", "移除未知字段或放入元数据对象。"))
    return issues


def _semantic_lint_issues(key: str, value: str) -> list[dict[str, str]]:
    if not value:
        return []
    if key == "outcome":
        return _outcome_lint_issues(value)
    if key == "verification":
        return _verification_lint_issues(value)
    if key == "constraints":
        return _constraints_lint_issues(value)
    if key == "boundaries":
        return _boundaries_lint_issues(value)
    if key == "iteration":
        return _iteration_lint_issues(value)
    if key == "blocked":
        return _blocked_lint_issues(value)
    return []


def _outcome_lint_issues(value: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    lowered_value = value.lower()
    if len(value) < 12:
        issues.append(_lint_issue("outcome", "medium", "目标结果过短", "补充交付物、作用范围、数量或完成标准。"))
    if _contains_any(lowered_value, ("优化一下", "处理一下", "修一下", "improve it", "fix it")):
        issues.append(_lint_issue("outcome", "high", "目标结果过于泛化", "改成可验收的交付物，例如文件、模块、数量或用户可见行为。"))
    if not _has_specific_signal(value):
        issues.append(_lint_issue("outcome", "medium", "缺少可验收对象", "补充路径、模块、接口、报告、数量或覆盖范围。"))
    return issues


def _verification_lint_issues(value: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    lowered_value = value.lower()
    if _contains_any(lowered_value, ("确保没问题", "验证通过", "测试通过", "make sure", "ensure it works")):
        issues.append(_lint_issue("verification", "medium", "验证方式偏主观", "写出具体命令、检查点或可保存证据。"))
    if not _has_command_signal(value):
        issues.append(_lint_issue("verification", "high", "缺少具体验证命令或证据", "至少补充一个测试、编译、lint、构建或人工检查证据。"))
    return issues


def _constraints_lint_issues(value: str) -> list[dict[str, str]]:
    lowered_value = value.lower()
    if _contains_any(lowered_value, ("小心", "不要乱改", "保持稳定", "be careful", "keep stable")):
        return [_lint_issue("constraints", "medium", "约束过于笼统", "明确不能改的行为、依赖、API、数据或文件范围。")]
    if not _contains_any(lowered_value, CONSTRAINT_KEYWORDS):
        return [_lint_issue("constraints", "medium", "约束缺少明确禁止或保持项", "加入“不改/不引入/保持兼容”等可执行约束。")]
    return []


def _boundaries_lint_issues(value: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    lowered_value = value.lower()
    if _contains_any(lowered_value, ("相关", "附近", "整个项目", "全项目", "wherever", "related files")):
        issues.append(_lint_issue("boundaries", "medium", "边界可能过宽或模糊", "列出包含目录/文件，并说明排除范围。"))
    if not (PATH_PATTERN.search(value) or _contains_any(lowered_value, ("仅", "只", "范围", "only", "scope", "within"))):
        issues.append(_lint_issue("boundaries", "high", "缺少明确范围信号", "补充具体目录、文件、模块或明确包含/排除范围。"))
    return issues


def _iteration_lint_issues(value: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    lowered_value = value.lower()
    if "commit" not in lowered_value and "提交" not in lowered_value:
        issues.append(_lint_issue("iteration", "high", "缺少提交节奏", "说明每个独立改动验证后单独 commit。"))
    if not _contains_any(lowered_value, ("每个", "每次", "逐个", "分批", "each", "per", "batch")):
        issues.append(_lint_issue("iteration", "medium", "缺少迭代粒度", "说明按文件、模块、接口或问题分批推进。"))
    return issues


def _blocked_lint_issues(value: str) -> list[dict[str, str]]:
    lowered_value = value.lower()
    if _contains_any(lowered_value, ("有问题就问", "遇到困难再说", "ask if needed", "if any issue")):
        return [_lint_issue("blocked", "medium", "受阻条件过于泛化", "明确哪些情况必须停下，哪些情况可跳过以及记录要求。")]
    if not _contains_any(lowered_value, BLOCKED_KEYWORDS):
        return [_lint_issue("blocked", "medium", "缺少停下或跳过条件", "补充必须问人、允许跳过或不可自行决策的条件。")]
    return []


def _has_specific_signal(value: str) -> bool:
    lowered_value = value.lower()
    return (
        bool(PATH_PATTERN.search(value))
        or bool(NUMBER_PATTERN.search(value))
        or _contains_any(lowered_value, SPECIFICITY_KEYWORDS)
    )


def _has_command_signal(value: str) -> bool:
    lowered_value = value.lower()
    command_pattern = re.compile(r"\b(?:python3?|pytest|unittest|npm|pnpm|yarn|go|cargo|make|uv|ruff|mypy|tsc)\b")
    return bool(command_pattern.search(lowered_value)) or _contains_any(lowered_value, ("运行", "执行", "检查", "人工检查", "报告审计"))


def _lint_issue(element: str, severity: str, message: str, suggestion: str) -> dict[str, str]:
    label = ELEMENT_LABELS.get(element, element)
    return {"element": element, "label": label, "severity": severity, "message": message, "suggestion": suggestion}


def _object_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _lint_score(issues: list[dict[str, str]]) -> int:
    penalties = {"high": 20, "medium": 10, "low": 5}
    total_penalty = sum(penalties.get(issue.get("severity", "low"), 5) for issue in issues)
    return max(0, 100 - total_penalty)


def _lint_summary(passed: bool, score: int, issues: list[dict[str, str]]) -> str:
    if passed:
        return f"语义质量检查通过，得分 {score}。"
    high_count = sum(1 for issue in issues if issue["severity"] == "high")
    return f"语义质量检查未通过，得分 {score}，高优先级问题 {high_count} 个。"


def validate_fields_json_data(data: object, source: str = "") -> dict[str, object]:
    """校验直接字段对象或包含 fields 对象的 6 要素 JSON。"""
    base_report: dict[str, object] = {
        "valid": False,
        "source": source,
        "source_format": "unknown",
        "missing_fields": [],
        "empty_fields": [],
        "unknown_fields": [],
        "checks": {
            "json_object": isinstance(data, dict),
            "fields_object": False,
            "required_fields_present": {},
            "required_fields_non_empty": {},
            "no_unknown_fields": False,
            "renderable": False,
        },
        "normalized_fields": {},
        "suggestion": "",
    }
    if not isinstance(data, dict):
        base_report["suggestion"] = "JSON 顶层必须是对象，或包含 fields 对象。"
        return base_report

    raw_fields = data.get("fields", data)
    source_format = "wrapped_fields" if "fields" in data else "direct_fields"
    base_report["source_format"] = source_format
    if not isinstance(raw_fields, dict):
        base_report["suggestion"] = "fields 必须是对象，或直接提供 6 要素字段对象。"
        return base_report

    missing_fields, empty_fields, normalized_fields = _inspect_required_fields(raw_fields)
    unknown_fields = _unknown_field_keys(raw_fields)
    renderable, render_error = _is_fields_json_renderable(normalized_fields, missing_fields, empty_fields)
    checks = {
        "json_object": True,
        "fields_object": True,
        "required_fields_present": {key: key not in missing_fields for key in ELEMENT_ORDER},
        "required_fields_non_empty": {key: key not in missing_fields and key not in empty_fields for key in ELEMENT_ORDER},
        "no_unknown_fields": not unknown_fields,
        "renderable": renderable,
    }
    valid = not missing_fields and not empty_fields and not unknown_fields and renderable
    return {
        "valid": valid,
        "source": source,
        "source_format": source_format,
        "missing_fields": missing_fields,
        "empty_fields": empty_fields,
        "unknown_fields": unknown_fields,
        "checks": checks,
        "normalized_fields": normalized_fields if valid else {},
        "render_error": render_error,
        "suggestion": _fields_json_validation_suggestion(missing_fields, empty_fields, unknown_fields, render_error),
    }


def _inspect_required_fields(raw_fields: dict[object, object]) -> tuple[list[str], list[str], dict[str, str]]:
    missing_fields: list[str] = []
    empty_fields: list[str] = []
    normalized_fields: dict[str, str] = {}
    for key in ELEMENT_ORDER:
        if key not in raw_fields:
            missing_fields.append(key)
            continue
        value = "" if raw_fields.get(key) is None else str(raw_fields.get(key)).strip()
        if not value:
            empty_fields.append(key)
            continue
        normalized_fields[key] = value
    return missing_fields, empty_fields, normalized_fields


def _unknown_field_keys(raw_fields: dict[object, object]) -> list[str]:
    return sorted(str(key) for key in raw_fields if str(key) not in ELEMENT_ORDER)


def _is_fields_json_renderable(
    normalized_fields: dict[str, str],
    missing_fields: list[str],
    empty_fields: list[str],
) -> tuple[bool, str]:
    if missing_fields or empty_fields:
        return False, ""
    try:
        render_goal_text(
            _GoalFields(
                outcome=normalized_fields["outcome"],
                verification=normalized_fields["verification"],
                constraints=normalized_fields["constraints"],
                boundaries=normalized_fields["boundaries"],
                iteration=normalized_fields["iteration"],
                blocked=normalized_fields["blocked"],
            )
        )
    except (KeyError, ValueError) as error:
        return False, str(error)
    return True, ""


def _fields_json_validation_suggestion(
    missing_fields: list[str],
    empty_fields: list[str],
    unknown_fields: list[str],
    render_error: str,
) -> str:
    suggestions: list[str] = []
    if missing_fields:
        suggestions.append(f"补齐缺失字段：{_labels_for_keys(missing_fields)}")
    if empty_fields:
        suggestions.append(f"为以下字段填写非空内容：{_labels_for_keys(empty_fields)}")
    if unknown_fields:
        suggestions.append(f"移除或移到元数据中的未知字段：{'、'.join(unknown_fields)}")
    if render_error:
        suggestions.append(f"修复渲染错误：{render_error}")
    return "；".join(suggestions) if suggestions else "字段 JSON 完整，可继续执行 --generate --from-json。"


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


def _labels_for_keys(keys: list[str]) -> str:
    return "、".join(ELEMENT_LABELS[key] for key in keys)


def explain_missing_elements(description: str) -> dict[str, object]:
    """解释任务描述缺失哪些 /goal 要素以及如何一次性补齐。"""
    if not description.strip():
        raise ValueError("--explain-missing 不能为空，请提供任务描述")
    analysis = analyze_description(description)
    profile = build_task_profile(description)
    recommendations = _recommended_field_mapping(profile)
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


def check_redaction(description: str) -> dict[str, object]:
    """检查任务描述中可能需要脱敏的敏感片段。"""
    if not description.strip():
        raise ValueError("--redaction-check 不能为空，请提供任务描述")
    findings = _sensitive_findings(description)
    risk_score = _redaction_risk_score(findings)
    risk_level = _redaction_risk_level(risk_score, findings)
    return {
        "safe_to_share": not findings,
        "risk_level": risk_level,
        "risk_score": risk_score,
        "finding_count": len(findings),
        "finding_types": _finding_types(findings),
        "findings": findings,
        "redacted_preview": _normalize_text(_redact_sensitive_text(description, findings))[:TEXT_PREVIEW_LENGTH],
        "recommended_action": _redaction_recommended_action(risk_level, findings),
    }


def merge_task_context(original_description: str, supplements: list[str]) -> dict[str, object]:
    """合并原始需求和补充回答，产出可进入 --generate 的字段草稿。"""
    if not original_description.strip():
        raise ValueError("--merge-context 不能为空，请提供原始任务描述")
    clean_supplements = [_normalize_text(supplement) for supplement in supplements if supplement.strip()]
    combined_text = _combined_context_text(original_description, clean_supplements)
    analysis = analyze_description(combined_text)
    explicit_fields = _extract_labeled_fields(combined_text)
    fields = dict(explicit_fields)
    field_sources = {key: "explicit_label" for key in explicit_fields}
    _merge_context_present_fields(fields, field_sources, combined_text, analysis)
    missing = _missing_field_keys(fields)
    profile = build_task_profile(combined_text)
    recommended_fields = _recommended_field_mapping(profile)
    validation = validate_fields_json_data({"fields": fields}, "merge_context")
    ready_to_generate = not missing and bool(validation["valid"])
    return {
        "original_description": _normalize_text(original_description),
        "supplements": clean_supplements,
        "combined_preview": _normalize_text(combined_text)[:TEXT_PREVIEW_LENGTH],
        "task_type": profile.get("task_type", {}),
        "risk_level": profile.get("risk_level", "unknown"),
        "risk_score": profile.get("risk_score", 0),
        "ready_to_generate": ready_to_generate,
        "missing": missing,
        "fields": {key: fields[key] for key in ELEMENT_ORDER if key in fields},
        "field_sources": _merge_field_sources(field_sources),
        "recommended_for_missing": {
            key: str(recommended_fields.get(key, DEFAULT_PROFILE_TEMPLATE[key]))
            for key in missing
        },
        "validation": validation,
        "next_prompt": "" if ready_to_generate else format_question_prompt(combined_text),
        "next_command": _merge_next_command(ready_to_generate, missing),
    }


def _combined_context_text(original_description: str, supplements: list[str]) -> str:
    parts = [_normalize_text(original_description), *supplements]
    return "\n".join(part for part in parts if part)


def _merge_context_present_fields(
    fields: dict[str, str],
    field_sources: dict[str, str],
    combined_text: str,
    analysis: AnalysisResult,
) -> None:
    for key in analysis["present"]:
        if fields.get(key):
            continue
        fields[key] = _context_fallback_field_value(key, combined_text)
        field_sources[key] = "inferred_from_context"


def _context_fallback_field_value(key: str, combined_text: str) -> str:
    clauses = _context_clauses(combined_text)
    if key == "outcome":
        return _outcome_context_clause(clauses) or _normalize_text(combined_text)
    return _matching_context_clause(clauses, CONTEXT_FALLBACK_HINTS.get(key, ())) or _normalize_text(combined_text)


def _context_clauses(text: str) -> list[str]:
    return [clause.strip() for clause in CONTEXT_CLAUSE_SPLIT_PATTERN.split(text) if clause.strip()]


def _outcome_context_clause(clauses: list[str]) -> str:
    for clause in clauses:
        lowered_clause = clause.lower()
        if _has_outcome(lowered_clause):
            return _strip_context_label(clause)
    return _strip_context_label(clauses[0]) if clauses else ""


def _matching_context_clause(clauses: list[str], hints: tuple[str, ...]) -> str:
    for clause in clauses:
        lowered_clause = clause.lower()
        if any(hint.lower() in lowered_clause for hint in hints):
            return _strip_context_label(clause)
    return ""


def _strip_context_label(clause: str) -> str:
    if "：" in clause:
        return clause.split("：", 1)[1].strip()
    if ":" in clause:
        return clause.split(":", 1)[1].strip()
    return clause


def _merge_field_sources(field_sources: dict[str, str]) -> dict[str, str]:
    return {key: field_sources.get(key, "missing") for key in ELEMENT_ORDER}


def _merge_next_command(ready_to_generate: bool, missing: list[str]) -> str:
    if ready_to_generate:
        return "保存 fields 为 JSON 后执行：python3 scripts/generate_goal.py --generate --from-json <fields.json>"
    return f"先补齐缺失要素：{_labels_for_keys(missing)}，再重新运行 --merge-context。"


def inspect_path_context(path_value: str, task_description: str = "") -> dict[str, object]:
    """扫描本地路径并输出生成 /goal 前需要的代码上下文线索。"""
    if not path_value.strip():
        raise ValueError("--inspect-path 不能为空，请提供文件或目录路径")
    root = Path(path_value).expanduser()
    if not root.exists():
        raise ValueError(f"--inspect-path 不存在：{path_value}")

    files, truncated = _collect_inspection_files(root)
    language_counts = _inspection_language_counts(files)
    test_files = [file_path for file_path in files if _is_inspection_test_file(root, file_path)]
    project_validation = _inspection_project_validation(root)
    verification_hints = _inspection_verification_hints(root, files, language_counts, project_validation)
    risk_flags = _inspection_risk_flags(root, files, language_counts, test_files, verification_hints, truncated)
    suggested_fields = _inspection_suggested_fields(
        root,
        files,
        language_counts,
        test_files,
        verification_hints,
        task_description,
    )
    return {
        "path": _display_path(root),
        "kind": "file" if root.is_file() else "directory",
        "truncated": truncated,
        "file_count": len(files),
        "language_counts": language_counts,
        "sample_files": [_display_path(file_path) for file_path in files[:INSPECT_SAMPLE_LIMIT]],
        "test_file_count": len(test_files),
        "test_files": [_display_path(file_path) for file_path in test_files[:INSPECT_SAMPLE_LIMIT]],
        "project_validation": project_validation,
        "verification_hints": verification_hints,
        "risk_flags": risk_flags,
        "suggested_fields": suggested_fields,
        "next_steps": _inspection_next_steps(task_description),
    }


def _collect_inspection_files(root: Path) -> tuple[list[Path], bool]:
    if root.is_file():
        return [root], False
    files: list[Path] = []
    truncated = False
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(dirname for dirname in dirnames if dirname not in INSPECT_SKIP_DIRS)
        for filename in sorted(filenames):
            file_path = Path(current_root) / filename
            if not file_path.is_file():
                continue
            files.append(file_path)
            if len(files) >= INSPECT_MAX_FILES:
                return files, True
    return files, truncated


def _inspection_language_counts(files: list[Path]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for file_path in files:
        language = INSPECT_LANGUAGE_BY_SUFFIX.get(file_path.suffix.lower(), "Other")
        counts[language] = counts.get(language, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _is_inspection_test_file(root: Path, file_path: Path) -> bool:
    normalized = _normalized_inspection_path(root, file_path).lower()
    return any(pattern in normalized for pattern in INSPECT_TEST_NAME_PATTERNS)


def _inspection_verification_hints(
    root: Path,
    files: list[Path],
    language_counts: dict[str, int],
    project_validation: dict[str, object],
) -> list[str]:
    hints: list[str] = []
    python_files = [file_path for file_path in files if file_path.suffix.lower() == ".py"]
    if python_files:
        hints.append(f"python3 -m py_compile {_join_shell_paths(python_files[:INSPECT_VERIFICATION_SAMPLE_LIMIT])}")
        if not _project_validation_has_command(project_validation, "python3 -m pytest") and (
            _has_named_file_near(root, "pyproject.toml") or _has_named_file_near(root, "pytest.ini")
        ):
            hints.append("python3 -m pytest")
    hints.extend(_project_validation_command_hints(project_validation))
    if "JavaScript" in language_counts or "TypeScript" in language_counts:
        if not _project_validation_has_kind(project_validation, "node"):
            hints.append("npm test（如项目 package.json 定义了 test 脚本）")
    if "Go" in language_counts:
        if not _project_validation_has_command(project_validation, "go test ./..."):
            hints.append("go test ./...")
    if "Rust" in language_counts:
        if not _project_validation_has_command(project_validation, "cargo test"):
            hints.append("cargo test")
    if "Java" in language_counts or "Kotlin" in language_counts:
        if not _project_validation_has_kind(project_validation, "java"):
            hints.append("运行项目现有 Gradle/Maven 测试命令")
    if not hints and files:
        hints.append("运行项目现有测试、构建或语法检查命令；若没有自动化验证，至少记录人工检查证据")
    return _dedupe_strings(hints)


def _inspection_project_validation(root: Path) -> dict[str, object]:
    """从目标路径附近的项目配置中发现可执行验证命令。"""
    config_paths = _nearby_project_config_paths(root)
    commands: list[dict[str, str]] = []
    notes: list[str] = []
    for config_path in config_paths:
        try:
            config_commands, config_notes = _validation_commands_from_config(config_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            notes.append(f"读取 {_display_path(config_path)} 失败：{error}")
            continue
        commands.extend(config_commands)
        notes.extend(config_notes)
    commands = _dedupe_command_entries(commands)[:PROJECT_VALIDATION_COMMAND_LIMIT]
    if not config_paths:
        notes.append("未在目标路径附近发现常见项目配置文件，验证命令仍需结合项目文档人工确认。")
    elif not commands:
        notes.append("已发现项目配置文件，但未提取到 test/lint/typecheck/build 等常见验证命令。")
    return {
        "config_files": [_display_path(config_path) for config_path in config_paths],
        "commands": commands,
        "notes": _dedupe_strings(notes),
    }


def _nearby_project_config_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory in _inspection_candidate_directories(root):
        for filename in PROJECT_CONFIG_FILENAMES:
            config_path = directory / filename
            if config_path.is_file():
                paths.append(config_path)
    return paths


def _inspection_candidate_directories(root: Path) -> list[Path]:
    start = root if root.is_dir() else root.parent
    candidates: list[Path] = []
    seen: set[str] = set()
    for directory in [start, *start.parents]:
        if len(candidates) >= PROJECT_CONFIG_SEARCH_DEPTH:
            break
        key = str(directory.resolve()) if directory.exists() else str(directory)
        if key in seen:
            continue
        candidates.append(directory)
        seen.add(key)
    return candidates


def _validation_commands_from_config(config_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    filename = config_path.name
    if filename == "package.json":
        return _package_json_validation_commands(config_path)
    if filename in {"Makefile", "makefile", "GNUmakefile"}:
        return _makefile_validation_commands(config_path)
    if filename in {"pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"}:
        return _python_project_validation_commands(config_path)
    if filename == "go.mod":
        return [_validation_command("go test ./...", _config_source(config_path), "go", "发现 go.mod，可运行 Go 全量测试。")], []
    if filename == "Cargo.toml":
        return [_validation_command("cargo test", _config_source(config_path), "rust", "发现 Cargo.toml，可运行 Rust 测试。")], []
    if filename == "pom.xml":
        return [_validation_command("mvn test", _config_source(config_path), "java", "发现 Maven pom.xml，可运行 Maven 测试。")], []
    if filename in {"build.gradle", "build.gradle.kts"}:
        return [
            _validation_command(
                _gradle_test_command(config_path.parent),
                _config_source(config_path),
                "java",
                "发现 Gradle 构建文件，可运行 Gradle 测试。",
            )
        ], []
    return [], []


def _package_json_validation_commands(config_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    if not isinstance(scripts, dict) or not scripts:
        return [], [f"{_display_path(config_path)} 未定义 scripts，无法提取 Node 验证命令。"]
    package_manager = _node_package_manager(config_path.parent)
    script_names = _selected_package_script_names(scripts)
    commands = [
        _validation_command(
            _node_script_command(package_manager, script_name),
            _config_source(config_path, f"scripts.{script_name}"),
            "node",
            f"package.json 定义 {script_name} 脚本：{_script_preview(scripts.get(script_name))}",
        )
        for script_name in script_names
    ]
    notes = [] if commands else [f"{_display_path(config_path)} 的 scripts 未包含常见 test/lint/typecheck/build 命令。"]
    return commands, notes


def _selected_package_script_names(scripts: dict[object, object]) -> list[str]:
    available = {str(name): value for name, value in scripts.items() if _script_preview(value)}
    selected: list[str] = [name for name in PACKAGE_SCRIPT_PRIORITY if name in available]
    extra_names = sorted(
        name
        for name in available
        if name not in selected and _looks_like_validation_script(name)
    )
    selected.extend(extra_names)
    return selected[:PROJECT_VALIDATION_COMMAND_LIMIT]


def _looks_like_validation_script(script_name: str) -> bool:
    lowered_name = script_name.lower()
    return any(keyword in lowered_name for keyword in ("test", "lint", "type", "check", "build", "ci"))


def _node_package_manager(directory: Path) -> str:
    if (directory / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (directory / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _node_script_command(package_manager: str, script_name: str) -> str:
    quoted_script = shlex.quote(script_name)
    if package_manager == "npm":
        return "npm test" if script_name == "test" else f"npm run {quoted_script}"
    if package_manager == "yarn":
        return f"yarn {quoted_script}"
    return f"pnpm run {quoted_script}"


def _makefile_validation_commands(config_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    text = config_path.read_text(encoding="utf-8")
    targets = _makefile_targets(text)
    selected_targets = _selected_make_targets(targets)
    commands = [
        _validation_command(
            f"make {shlex.quote(target)}",
            _config_source(config_path, target),
            "make",
            f"Makefile 定义 {target} 目标。",
        )
        for target in selected_targets
    ]
    notes = [] if commands else [f"{_display_path(config_path)} 未发现常见 test/check/lint/typecheck/build 目标。"]
    return commands, notes


def _makefile_targets(text: str) -> list[str]:
    target_pattern = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?![=])", re.MULTILINE)
    targets: list[str] = []
    seen: set[str] = set()
    for match in target_pattern.finditer(text):
        target = match.group(1)
        if target in seen:
            continue
        targets.append(target)
        seen.add(target)
    return targets


def _selected_make_targets(targets: list[str]) -> list[str]:
    target_set = set(targets)
    selected = [target for target in MAKE_TARGET_PRIORITY if target in target_set]
    selected.extend(
        target
        for target in targets
        if target not in selected and _looks_like_validation_script(target)
    )
    return selected[:PROJECT_VALIDATION_COMMAND_LIMIT]


def _python_project_validation_commands(config_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    text = config_path.read_text(encoding="utf-8")
    filename = config_path.name
    if filename == "pytest.ini":
        return [_validation_command("python3 -m pytest", _config_source(config_path), "python", "发现 pytest.ini。")], []
    if filename == "tox.ini":
        return _tox_validation_commands(config_path, text)
    if filename == "setup.cfg":
        return _setup_cfg_validation_commands(config_path, text)
    return _pyproject_validation_commands(config_path, text)


def _pyproject_validation_commands(config_path: Path, text: str) -> tuple[list[dict[str, str]], list[str]]:
    commands: list[dict[str, str]] = []
    if _has_toml_section(text, "tool.pytest"):
        commands.append(_validation_command("python3 -m pytest", _config_source(config_path, "tool.pytest"), "python", "pyproject.toml 定义 pytest 配置。"))
    if _has_toml_section(text, "tool.ruff"):
        commands.append(_validation_command("python3 -m ruff check .", _config_source(config_path, "tool.ruff"), "python", "pyproject.toml 定义 ruff 配置。"))
    if _has_toml_section(text, "tool.mypy"):
        commands.append(_validation_command("python3 -m mypy .", _config_source(config_path, "tool.mypy"), "python", "pyproject.toml 定义 mypy 配置。"))
    if _has_toml_section(text, "tool.pyright"):
        commands.append(_validation_command("python3 -m pyright", _config_source(config_path, "tool.pyright"), "python", "pyproject.toml 定义 pyright 配置。"))
    if _has_toml_section(text, "tool.black"):
        commands.append(_validation_command("python3 -m black --check .", _config_source(config_path, "tool.black"), "python", "pyproject.toml 定义 black 配置。"))
    notes = [] if commands else [f"{_display_path(config_path)} 未发现 tool.pytest/ruff/mypy/pyright/black 等验证配置。"]
    return commands, notes


def _tox_validation_commands(config_path: Path, text: str) -> tuple[list[dict[str, str]], list[str]]:
    commands = [_validation_command("python3 -m tox", _config_source(config_path, "tox"), "python", "发现 tox.ini。")]
    if _has_ini_section(text, "pytest"):
        commands.append(_validation_command("python3 -m pytest", _config_source(config_path, "pytest"), "python", "tox.ini 同时定义 pytest 配置。"))
    return commands, []


def _setup_cfg_validation_commands(config_path: Path, text: str) -> tuple[list[dict[str, str]], list[str]]:
    commands: list[dict[str, str]] = []
    if _has_ini_section(text, "tool:pytest"):
        commands.append(_validation_command("python3 -m pytest", _config_source(config_path, "tool:pytest"), "python", "setup.cfg 定义 pytest 配置。"))
    if _has_ini_section(text, "mypy"):
        commands.append(_validation_command("python3 -m mypy .", _config_source(config_path, "mypy"), "python", "setup.cfg 定义 mypy 配置。"))
    if _has_ini_section(text, "flake8"):
        commands.append(_validation_command("python3 -m flake8 .", _config_source(config_path, "flake8"), "python", "setup.cfg 定义 flake8 配置。"))
    notes = [] if commands else [f"{_display_path(config_path)} 未发现 tool:pytest/mypy/flake8 等验证配置。"]
    return commands, notes


def _has_toml_section(text: str, section_prefix: str) -> bool:
    lowered_text = text.lower()
    return f"[{section_prefix.lower()}" in lowered_text


def _has_ini_section(text: str, section_name: str) -> bool:
    return f"[{section_name.lower()}]" in text.lower()


def _gradle_test_command(directory: Path) -> str:
    if (directory / "gradlew").exists():
        return "./gradlew test"
    return "gradle test"


def _validation_command(command: str, source: str, kind: str, reason: str) -> dict[str, str]:
    return {"command": command, "source": source, "kind": kind, "reason": reason}


def _config_source(config_path: Path, section: str = "") -> str:
    display_path = _display_path(config_path)
    return f"{display_path}:{section}" if section else display_path


def _script_preview(value: object) -> str:
    preview = str(value).strip() if value is not None else ""
    return preview[:TEXT_PREVIEW_LENGTH]


def _dedupe_command_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    unique_entries: list[dict[str, str]] = []
    seen_commands: set[str] = set()
    for entry in entries:
        command = entry.get("command", "").strip()
        if not command or command in seen_commands:
            continue
        unique_entries.append(entry)
        seen_commands.add(command)
    return unique_entries


def _project_validation_command_hints(project_validation: dict[str, object]) -> list[str]:
    hints: list[str] = []
    for entry in _project_validation_commands(project_validation):
        command = entry.get("command", "").strip()
        source = entry.get("source", "项目配置").strip() or "项目配置"
        if command:
            hints.append(f"{command}（来自 {source}）")
    return hints


def _project_validation_commands(project_validation: dict[str, object]) -> list[dict[str, str]]:
    raw_commands = project_validation.get("commands", [])
    if not isinstance(raw_commands, list):
        return []
    return [entry for entry in raw_commands if isinstance(entry, dict)]


def _project_validation_has_command(project_validation: dict[str, object], command: str) -> bool:
    return any(entry.get("command") == command for entry in _project_validation_commands(project_validation))


def _project_validation_has_kind(project_validation: dict[str, object], kind: str) -> bool:
    return any(entry.get("kind") == kind for entry in _project_validation_commands(project_validation))


def _dedupe_strings(values: list[str]) -> list[str]:
    unique_values: list[str] = []
    seen_values: set[str] = set()
    for value in values:
        if value in seen_values:
            continue
        unique_values.append(value)
        seen_values.add(value)
    return unique_values


def _join_shell_paths(paths: list[Path]) -> str:
    return " ".join(shlex.quote(_display_path(path)) for path in paths)


def _has_named_file_near(root: Path, filename: str) -> bool:
    candidates = [root] if root.is_dir() else [root.parent]
    candidates.extend(candidates[0].parents)
    return any((candidate / filename).exists() for candidate in candidates[:4])


def _inspection_risk_flags(
    root: Path,
    files: list[Path],
    language_counts: dict[str, int],
    test_files: list[Path],
    verification_hints: list[str],
    truncated: bool,
) -> list[str]:
    flags: list[str] = []
    if truncated:
        flags.append(f"扫描达到 {INSPECT_MAX_FILES} 个文件上限，建议在 /goal 中收窄边界或分批处理。")
    if not files:
        flags.append("目标路径下没有发现可扫描文件，需确认边界是否正确。")
    if len(language_counts) >= 3:
        flags.append("扫描范围包含多种语言，建议按语言或模块拆分迭代。")
    if files and not test_files:
        flags.append("扫描范围内未发现明显测试文件，需在 /goal 中说明替代验证方式。")
    if not verification_hints:
        flags.append("未能推断验证命令，生成 /goal 前应让用户补充项目验证方式。")
    if _path_has_generated_hint(root):
        flags.append("路径名包含生成物或构建产物线索，需确认是否允许修改生成文件。")
    return flags or ["未发现明显路径级风险；仍需结合用户目标确认最终 6 要素。"]


def _path_has_generated_hint(root: Path) -> bool:
    lowered_parts = {part.lower() for part in root.parts}
    return bool(lowered_parts.intersection({"dist", "build", "generated", "target"}))


def _inspection_suggested_fields(
    root: Path,
    files: list[Path],
    language_counts: dict[str, int],
    test_files: list[Path],
    verification_hints: list[str],
    task_description: str,
) -> dict[str, str]:
    language_summary = _language_summary(language_counts)
    outcome = _inspection_outcome(root, task_description)
    boundaries = _inspection_boundaries(root, files, language_summary)
    verification = "；".join(verification_hints) or "确认路径包含可处理文件后，补充项目现有验证命令或人工检查证据。"
    constraints = "不修改扫描范围外文件；不引入未经确认的新依赖；保持现有公开 API、数据格式和用户可见行为兼容。"
    if test_files:
        constraints += " 不删除、跳过或弱化现有测试。"
    iteration = "按文件、模块或语言分批处理；每个独立改动运行相关验证后单独 commit，预期 3-8 个 commit。"
    blocked = "扫描结果无法证明业务预期、必须改变公共行为、验证命令缺失或范围需要扩大时停下向用户确认。"
    return {
        "outcome": outcome,
        "verification": verification,
        "constraints": constraints,
        "boundaries": boundaries,
        "iteration": iteration,
        "blocked": blocked,
    }


def _inspection_outcome(root: Path, task_description: str) -> str:
    normalized_task = _normalize_text(task_description)
    if normalized_task:
        return f"围绕 `{_display_path(root)}` 完成用户描述的编码任务：{normalized_task}"
    return f"基于 `{_display_path(root)}` 的现有代码上下文补齐待执行编码任务；生成最终 /goal 前需进一步明确用户目标结果。"


def _inspection_boundaries(root: Path, files: list[Path], language_summary: str) -> str:
    if root.is_file():
        return f"仅处理文件 `{_display_path(root)}`；范围外问题只记录，不纳入本次改动。"
    sample_paths = "、".join(_display_path(file_path) for file_path in files[:5]) or "无样例文件"
    return (
        f"仅处理 `{_display_path(root)}` 下已扫描的 {len(files)} 个文件"
        f"（主要类型：{language_summary}；样例：{sample_paths}）；"
        "排除 .git、缓存目录、依赖目录、构建产物和生成文件，范围外问题只记录。"
    )


def _language_summary(language_counts: dict[str, int]) -> str:
    if not language_counts:
        return "未识别"
    return "、".join(f"{language} {count} 个" for language, count in list(language_counts.items())[:5])


def _inspection_next_steps(task_description: str) -> list[str]:
    steps = [
        "确认 suggested_fields.outcome 是否符合真实用户目标，必要时补充数量、模块或验收标准。",
        "把 suggested_fields 保存为 JSON 后，可用 --validate-fields-json 检查，再用 --generate --from-json 生成最终 /goal。",
    ]
    if not task_description.strip():
        steps.insert(0, "追加 --path-task '<用户目标>' 重新扫描，可得到更贴近场景的 outcome 建议。")
    return steps


def _normalized_inspection_path(root: Path, file_path: Path) -> str:
    try:
        relative = file_path.relative_to(root if root.is_dir() else root.parent)
    except ValueError:
        relative = file_path
    return str(relative).replace("\\", "/")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _sensitive_findings(text: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    occupied_spans: list[tuple[int, int]] = []
    for kind, severity, recommendation, pattern in SENSITIVE_VALUE_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if _span_overlaps(start, end, occupied_spans):
                continue
            occupied_spans.append((start, end))
            findings.append(
                {
                    "type": kind,
                    "severity": severity,
                    "start": start,
                    "end": end,
                    "preview": _mask_sensitive_value(match.group(0)),
                    "recommendation": recommendation,
                }
            )
    return sorted(findings, key=lambda item: int(item["start"]))


def _span_overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _mask_sensitive_value(value: str) -> str:
    normalized = _normalize_text(value)
    if len(normalized) <= 8:
        return "***"
    return f"{normalized[:4]}***{normalized[-4:]}"


def _redact_sensitive_text(text: str, findings: list[dict[str, object]]) -> str:
    redacted = text
    for finding in sorted(findings, key=lambda item: int(item["start"]), reverse=True):
        start = int(finding["start"])
        end = int(finding["end"])
        redacted = f"{redacted[:start]}[REDACTED:{finding['type']}]{redacted[end:]}"
    return redacted


def _redaction_risk_score(findings: list[dict[str, object]]) -> int:
    score = sum(SENSITIVE_SEVERITY_SCORES.get(str(finding.get("severity", "low")), 10) for finding in findings)
    return min(score, 100)


def _redaction_risk_level(risk_score: int, findings: list[dict[str, object]]) -> str:
    if any(finding.get("severity") == "critical" for finding in findings) or risk_score >= 70:
        return "high"
    if risk_score >= 30:
        return "medium"
    if findings:
        return "low"
    return "none"


def _finding_types(findings: list[dict[str, object]]) -> list[str]:
    return sorted({str(finding.get("type", "unknown")) for finding in findings})


def _redaction_recommended_action(risk_level: str, findings: list[dict[str, object]]) -> str:
    if not findings:
        return "未发现明显敏感片段，可继续复核 6 要素后共享。"
    if risk_level == "high":
        return "共享前必须移除或替换所有 high/critical 敏感片段，并重新运行 --redaction-check。"
    return "共享前建议确认发现项是否可公开；不能公开时使用 redacted_preview 或占位符替换。"


def _recommended_field_mapping(profile: dict[str, object]) -> dict[str, object]:
    recommended_fields = profile.get("recommended_fields", {})
    return recommended_fields if isinstance(recommended_fields, dict) else {}


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


def _infer_profile(text: str) -> tuple[str, str, dict[str, str]]:
    lowered_text = text.lower()
    for profile_id, label, keywords, template in PROFILE_RULES:
        if profile_id != "testing" and _matches_profile(profile_id, text, lowered_text, keywords):
            return profile_id, label, template
    for profile_id, label, keywords, template in PROFILE_RULES:
        if _matches_profile(profile_id, text, lowered_text, keywords):
            return profile_id, label, template
    return "generic", "通用编码任务", DEFAULT_PROFILE_TEMPLATE


def _matches_profile(profile_id: str, text: str, lowered_text: str, keywords: tuple[str, ...]) -> bool:
    if profile_id == "testing":
        return _mentions_test_task(text) or _contains_any(lowered_text, ("覆盖率", "用例"))
    return _contains_any(lowered_text, keywords)


def _estimate_complexity(text: str, missing: list[str]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if len(text) > 120:
        reasons.append("描述较长，可能包含多个子任务或隐含约束")
    if len(missing) >= 4:
        reasons.append("缺失 4 个以上必要要素，需要集中补齐")
    if any(keyword in text.lower() for keyword in ("批量", "迁移", "升级", "多个", "全量", "batch", "migration", "upgrade", "multiple", "full")):
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
    description = getattr(args, "description", None)
    if isinstance(description, str) and description.strip() and not field_values:
        field_values.update(_goal_defaults_from_description(description.strip(), getattr(args, "branch", None)))
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


def _goal_defaults_from_description(description: str, branch: object) -> dict[str, str]:
    values = dict(INTERACTIVE_DEFAULTS)
    branch_text = branch.strip() if isinstance(branch, str) else ""
    if branch_text:
        values["outcome"] = f"在分支 `{branch_text}` 上完成用户描述的编码任务：{description}"
    else:
        values["outcome"] = f"完成用户描述的编码任务：{description}"
    return values


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
    if "文档" in outcome or "报告" in outcome or "readme" in lowered_text or "docs" in lowered_text or "documentation" in lowered_text:
        return "docs"
    if "修复" in outcome or "bug" in lowered_text or "fix" in lowered_text or "error" in lowered_text:
        return "fix"
    if "重构" in outcome or "优化" in outcome or "refactor" in lowered_text or "optimize" in lowered_text:
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
    lowered_text = outcome.lower()
    if "拆分" in outcome or "长函数" in outcome or "split" in lowered_text or "long function" in lowered_text:
        return "拆分函数"
    if "补齐" in outcome or "新增" in outcome or "添加" in outcome or "add" in lowered_text or "create" in lowered_text:
        return "新增内容"
    if "修复" in outcome or "fix" in lowered_text:
        return "修复问题"
    if "迁移" in outcome or "升级" in outcome or "migrate" in lowered_text or "upgrade" in lowered_text:
        return "迁移实现"
    if "优化" in outcome or "重构" in outcome or "optimize" in lowered_text or "refactor" in lowered_text:
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
    if "修复" in outcome or "bug" in lowered_text or "fix" in lowered_text or "error" in lowered_text:
        return "fix"
    if "重构" in outcome or "refactor" in lowered_text:
        return "refactor"
    if "优化" in outcome or "质量" in outcome or "optimize" in lowered_text or "quality" in lowered_text:
        return "optimize"
    return "feature"


def _branch_slug(outcome: str) -> str:
    lowered_text = outcome.lower()
    if _mentions_test_task(outcome):
        return "add-tests"
    if "api" in lowered_text or "接口" in outcome:
        return "api-task"
    if "文档" in outcome or "报告" in outcome or "readme" in lowered_text or "docs" in lowered_text or "documentation" in lowered_text:
        return "docs-task"
    if "迁移" in outcome or "升级" in outcome or "migration" in lowered_text or "migrate" in lowered_text or "upgrade" in lowered_text:
        return "migration-task"
    if "质量" in outcome or "优化" in outcome or "quality" in lowered_text or "optimize" in lowered_text:
        return "code-quality"
    return DEFAULT_BRANCH_SLUG


if __name__ == "__main__":
    sys.exit(main())
