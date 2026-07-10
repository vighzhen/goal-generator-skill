# goal-generator-skill

Codex CLI `/goal` 指令生成器：把用户的一句话或详细编码任务需求，补全为可直接复制粘贴到 Codex CLI 的高质量 `/goal` 指令纯文本。

## 项目结构

```text
goal-generator-skill/
├── SKILL.md
├── scripts/
│   ├── generate_goal.py
│   └── batch_generate.py
├── examples/
│   ├── sample_tasks.json
│   └── sample_tasks.csv
├── references/
│   ├── elements.md
│   └── anti_laziness.md
└── assets/
    └── goal_template.txt
```

## 安装方式

本项目是一个 Codex Skill 目录，可直接复制或软链接到 Codex Skill 搜索路径中使用。也可以在本仓库内直接运行生成器脚本：

```bash
python3 scripts/generate_goal.py --help
```

脚本只依赖 Python 标准库，不需要额外安装第三方依赖。

## 单任务使用方式

### 分析需求完整度

使用 `--analyze` 检查用户任务描述是否包含 6 个必要要素，并以 JSON 输出缺失项和追问示例：

```bash
python3 scripts/generate_goal.py --analyze "我要让 Codex 帮我给项目加单元测试"
```

脚本也支持常见英文编码任务描述，可直接识别英文动作、验证命令、约束、边界、迭代和受阻条件：

```bash
python3 scripts/generate_goal.py --analyze "Add pytest unit tests for src/services, run pytest tests/services -q, do not change business logic, only touch src/services and tests/services, commit per module, ask me if expected behavior is unclear"
```

### 生成任务类型画像和推荐模板

使用 `--profile` 识别任务类型、复杂度、风险提示、追问策略，并给出该类型下 6 要素的推荐填写方向：

```bash
python3 scripts/generate_goal.py --profile "修复登录 API 在空 token 场景下偶发 500 的问题"
```

### 检查敏感信息

使用 `--redaction-check` 在把任务描述分享到机器人、Issue、PR 或外部系统前，检查是否包含疑似 token、密钥、邮箱、URL 等敏感片段，并输出脱敏预览和处理建议：

```bash
python3 scripts/generate_goal.py --redaction-check "修复登录问题，token=abcdef1234567890，联系 owner@example.com"
```

### 扫描代码路径上下文

当用户只指出“改这个目录/文件”，但没有整理边界、语言栈、测试文件和验证命令时，可用 `--inspect-path` 扫描本地路径并输出代码事实摘要、风险提示和可用于 6 要素草稿的 `suggested_fields`：

```bash
python3 scripts/generate_goal.py --inspect-path scripts --path-task "优化 goal 生成器的输入分析能力"
```

输出包含 `language_counts`、`sample_files`、`test_files`、`project_validation`、`verification_hints`、`risk_flags` 和 `suggested_fields`。其中 `project_validation` 会从目标路径附近的 `package.json`、`Makefile`、`pyproject.toml`、`pytest.ini`、`go.mod`、`Cargo.toml`、`pom.xml`、`build.gradle` 等常见配置中提取 test/lint/typecheck/build 命令及来源证据。建议先复核 `suggested_fields.outcome` 是否符合真实目标，再保存为 JSON 并配合 `--validate-fields-json` 与 `--generate --from-json` 生成最终 `/goal`。

### 解释缺失要素

使用 `--explain-missing` 在 `--analyze` 的基础上进一步输出“为什么缺、优先补什么、推荐怎么填”和可直接发送给用户的追问文案：

```bash
python3 scripts/generate_goal.py --explain-missing "给项目加单元测试"
```

### 使用内置任务模板库

如果已经知道任务类型，可以直接列出和读取内置模板，快速获得 6 要素填写方向：

```bash
python3 scripts/generate_goal.py --list-templates
python3 scripts/generate_goal.py --template bugfix
```

### 生成一次性追问文案

使用 `--questions` 把缺失要素分析结果转成可直接发给需求方的中文追问文本：

```bash
python3 scripts/generate_goal.py --questions "我要让 Codex 帮我给项目加单元测试"
```

### 合并原始需求和补充回答

当用户先给出模糊需求、再按追问补充路径、验证命令、约束和受阻条件时，可用 `--merge-context` 加可重复的 `--supplement` 把多轮上下文合并成 6 要素字段草稿：

```bash
python3 scripts/generate_goal.py \
  --merge-context "我要让 Codex 帮我给项目加单元测试" \
  --supplement "只测 src/services，用 pytest tests/services -q，不改业务逻辑" \
  --supplement "每个模块一个 commit，无法判断断言时停下问我"
```

输出包含 `fields`、`field_sources`、`missing`、`recommended_for_missing`、`ready_to_generate` 和 `next_command`。当 `ready_to_generate` 为 `true` 时，可保存 `fields` 为 JSON，再运行 `python3 scripts/generate_goal.py --generate --from-json <fields.json>`。

### 写入文件

单任务命令可追加 `--output-file`，把分析、画像、追问文案或完整 `/goal` 指令写入文件，避免长文本复制丢失：

```bash
python3 scripts/generate_goal.py --questions "我要让 Codex 帮我给项目加单元测试" --output-file questions.txt
```

### 生成完整 /goal 指令

当 6 个要素都已明确时，使用 `--generate` 传入字段并生成可复制的 `/goal` 指令纯文本：

```bash
python3 scripts/generate_goal.py --generate \
  --outcome "为 src/services/ 的核心逻辑新增不少于 20 个 pytest 单元测试用例" \
  --verification "每次新增测试后运行 pytest tests/services -q，最终运行 pytest -q" \
  --constraints "不改业务逻辑、不引入新依赖、不修改公共 API" \
  --boundaries "仅处理 src/services/ 和 tests/services/，排除 legacy/ 与生成文件" \
  --iteration "每个模块一组测试，验证通过后单独 commit，预期 4-8 个 commit" \
  --blocked "无法从现有代码推断业务预期时停下问人；允许跳过项不超过 10%"
```

如果 6 要素已经保存为 JSON，也可以使用 `--from-json` 读取，命令行显式字段会覆盖文件字段：

```bash
python3 scripts/generate_goal.py --generate --from-json goal_fields.json
```

只有一句话需求时，也可以用简写方式生成带默认约束和默认验证策略的 `/goal` 草案；如需指定分支名，追加 `--branch`：

```bash
python3 scripts/generate_goal.py --generate "给项目加单元测试" --branch test-branch
```

### 校验 6 要素字段 JSON

使用 `--validate-fields-json` 在执行 `--generate --from-json` 前检查 JSON 是否包含完整且非空的 6 要素字段、是否存在未知字段，以及是否能正常渲染成 `/goal`：

```bash
python3 scripts/generate_goal.py --validate-fields-json goal_fields.json
```

命令支持直接字段对象，也支持 `{ "fields": { ... } }` 包装结构；校验通过时退出码为 0，否则退出码为 1 并输出修复建议。

### 检查 6 要素语义质量

字段 JSON 结构完整不代表内容足够好。使用 `--lint-fields-json` 检查字段是否过短、过泛，验证方式是否有具体命令或证据，边界是否明确，迭代和受阻条件是否可执行：

```bash
python3 scripts/generate_goal.py --lint-fields-json goal_fields.json
```

命令输出 `score`、`issues`、`summary` 和原始结构校验结果；存在高优先级问题或得分低于门槛时退出码为 1，适合作为 `--generate --from-json` 前的质量门禁。

### 校验已有 /goal 文件

如果 `/goal` 指令经过手工编辑、复制或批量拼接，可以用 `--validate-goal-file` 检查分隔线、5 段结构和 6 要素提示是否完整：

```bash
python3 scripts/generate_goal.py --validate-goal-file goal.txt
```

### 检查已有 /goal 语义质量

结构完整的 `/goal` 仍可能存在字段空泛、验证方式不具体或边界不清的问题。使用 `--lint-goal-file` 会先做结构校验，再从概述中抽取 6 要素并复用字段语义质量门禁：

```bash
python3 scripts/generate_goal.py --lint-goal-file goal.txt
```

命令输出 `validation`、`extracted_fields`、`field_lint` 和 `summary`；结构无效或语义质量未通过时退出码为 1。

### 检查 /goal 输出目录

批量生成到 `--output-dir` 或人工整理多个 `/goal` 文本后，可用 `--lint-goal-dir` 一次性检查目录内所有 `.txt` 文件，避免逐个运行 `--lint-goal-file` 时漏检最终交付物：

```bash
python3 scripts/generate_goal.py --lint-goal-dir output/
```

命令按文件名稳定扫描目录直属 `.txt` 文件，输出 `file_count`、`passed_count`、`failed_count`、每个文件的结构与语义质量结果；任一文件未通过或目录内没有 `.txt` 目标文件时退出码为 1，适合批量生成后的 CI 或交付前质量门禁。

### 交互模式

使用 `--interactive` 可按提示输入任务描述和补充信息，由脚本循环分析缺失要素并生成最终指令：

```bash
python3 scripts/generate_goal.py --interactive
```

## 批量生成

当需要一次为多个编码任务生成 `/goal` 指令时，使用 `scripts/batch_generate.py` 从 JSON 或 CSV 读取任务列表。

JSON 输入格式：

```json
[
  {
    "name": "代码质量优化",
    "description": "对50个非测试Python文件做7维度代码质量优化",
    "depends_on": [],
    "fields": {
      "outcome": "对50个非测试 Python 文件做 7 维度代码质量优化",
      "verification": "每个改动后执行 python -m py_compile"
    }
  }
]
```

CSV 输入格式支持以下表头（`depends_on`/`dependencies` 可选，6 要素列可按需填写）：

```csv
name,description,depends_on,outcome,verification,constraints,boundaries,iteration,blocked
```

常用命令：

```bash
# 只分析示例任务的要素完整度，不生成指令
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run

# 位置参数也可作为输入文件路径
python3 scripts/batch_generate.py examples/sample_tasks.json --dry-run

# 从 JSON 批量生成并输出到控制台
python3 scripts/batch_generate.py --input examples/sample_tasks.json

# 从 CSV 批量分析，适合用表格工具编辑后检查
python3 scripts/batch_generate.py --input examples/sample_tasks.csv --dry-run

# 严格模式：缺失 6 要素的任务会跳过，不使用默认填充
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --strict

# 校验清单快捷模式：等价于 dry-run + strict + summary-only + fail-on-skipped
python3 scripts/batch_generate.py --input examples/sample_tasks.json --check --report-json batch_report.json

# 批量检查任务 6 要素字段语义质量，适合生成前或 CI 门禁
python3 scripts/batch_generate.py --input examples/sample_tasks.json --lint-fields --report-json fields_lint.json

# 批量审计任务名称、描述和字段中的 token、邮箱、URL 等敏感信息
python3 scripts/batch_generate.py --input examples/sample_tasks.json --redaction-check --report-json redaction_report.json

# CI 门禁：只要存在跳过任务就返回非零退出码，同时仍输出摘要和报告
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --strict --fail-on-skipped --report-json batch_report.json

# 使用团队默认值覆盖缺失要素的默认填充
python3 scripts/batch_generate.py --input examples/sample_tasks.json --defaults-json team_defaults.json

# 也可以用环境变量配置团队默认值文件；命令行 --defaults-json 优先级更高
GOAL_GENERATOR_DEFAULTS_JSON=team_defaults.json python3 scripts/batch_generate.py --input examples/sample_tasks.json

# 输出结构化 JSON 报告，便于 CI、IDE 或脚本读取每个任务的缺失项和跳过原因
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --report-json batch_report.json

# 只处理任务名或描述匹配正则的任务
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --filter "测试|Bug"

# 只处理前 2 个任务，适合大清单首次接入时快速试跑
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --limit 2

# 只预览将要处理的任务名称，适合检查 filter/sort/limit 是否命中正确范围
python3 scripts/batch_generate.py --input examples/sample_tasks.json --list-tasks --filter "测试|Bug"

# 生成依赖执行计划，检查 depends_on/dependencies 是否存在未知依赖或循环依赖
python3 scripts/batch_generate.py --input examples/sample_tasks.json --plan-dependencies --report-json dependency_plan.json

# 按 depends_on/dependencies 拓扑顺序生成或 dry-run，依赖无效时失败
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dependency-order --dry-run

# 按任务名稳定排序输出，适合多人维护的大清单审计和结果比对
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --sort-by name

# 跳过重复任务，适合多人维护或多来源合并后的任务清单
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --dedupe --report-json batch_report.json

# 只看最终摘要，不在终端打印每个任务正文
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --summary-only

# 每个任务生成一个独立 .txt 文件，输出目录不存在时会自动创建
python3 scripts/batch_generate.py --input examples/sample_tasks.json --output-dir output/

# 所有任务写入同一个文件
python3 scripts/batch_generate.py --input examples/sample_tasks.json --output-file all_goals.txt
```

说明：

- `--input` 和位置参数都可指定输入文件；推荐在脚本中显式使用 `--input`。
- 当前批量输入只支持 JSON 和 CSV，其他格式不再作为核心能力维护。
- JSON 任务可选 `depends_on` 或 `dependencies` 字段；CSV 可选 `depends_on` 或 `dependencies` 表头，多个依赖用逗号、分号、顿号或换行分隔。
- `--output-dir` 和 `--output-file` 互斥。
- 任务中缺失的 6 要素会先尝试从 `description` 分析补齐；仍缺失时默认使用交互模式同款默认值填充，并在输出中标注默认填充的要素。
- `--defaults-json <path>` 或 `GOAL_GENERATOR_DEFAULTS_JSON` 可覆盖默认填充策略。
- `--report-json <path>` 是保留的批量报告格式，包含成功任务、缺失项、默认填充项、输出路径、跳过原因和修复建议。
- `--check` 适合 CI 或交付前检查；`--strict` 和 `--fail-on-skipped` 可组合成质量门禁。
- `--lint-fields` 不生成 `/goal` 正文，而是逐个任务检查 6 要素字段的具体性、验证命令、边界、提交节奏和受阻条件；任一任务未通过时退出码为 1，可配合 `--report-json` 做批量质量门禁。
- `--redaction-check` 不生成 `/goal` 正文，而是逐个任务审计名称、描述和 6 要素字段值中的 token、密钥、邮箱、URL 等敏感片段；发现风险时退出码为 1，并在报告中提供脱敏预览。
- `--plan-dependencies` 不生成 `/goal` 正文，而是输出按依赖分批的执行计划；发现未知依赖、重复任务名或循环依赖时退出码为 1。
- `--dependency-order` 会在生成、dry-run、list、lint 或 redaction 前应用依赖拓扑顺序；如果筛选/limit 后导致依赖缺失、循环或重复任务名，会直接失败并提示先运行 `--plan-dependencies` 查看详情。

## 6 个必要要素

1. **Outcome（目标结果）**：最终要交付什么，必须具体到可验证。
2. **Verification Surface（验证方式）**：如何确认每个改动正确，以及最终如何验收。
3. **Constraints（约束）**：不能碰什么、必须保持什么兼容性和行为。
4. **Boundaries（边界）**：任务作用范围，包含哪些文件/目录，排除哪些内容。
5. **Iteration Policy（迭代策略）**：每一步的粒度、验证动作、commit 节奏和预期提交数量。
6. **Blocked Stop Condition（受阻停止条件）**：什么情况下可以跳过，什么情况下必须停下问人。

## 适用场景

- 代码质量优化/重构
- 新功能开发
- 接口/API 开发
- 测试编写
- 批量 Bug 修复
- 代码迁移/升级
- 文档生成
- 批量任务指令生成（JSON/CSV）
- 英文 Issue/PR/Jira 编码任务描述的 6 要素分析
- 任务类型画像与 6 要素模板推荐
- 缺失 6 要素的原因解释、优先级和补全建议
- 从本地文件或目录反向整理 `/goal` 边界、项目配置验证命令和风险线索
- 单任务敏感信息检查和脱敏预览
- 批量任务敏感信息审计和脱敏预览
- 单任务 6 要素字段 JSON 质量校验
- 内置任务模板库（测试、Bug 修复、重构、文档、通用任务）
- 已有 `/goal` 文件结构校验
- 已有 `/goal` 文件语义质量检查
- `/goal` 输出目录结构与语义质量门禁
- 一键生成可复制的缺失要素追问文案
- 原始需求与补充回答合并为 6 要素字段草稿
- 单任务 6 要素字段语义质量检查和生成前门禁
- 单任务输出写入文件
- 单任务从 JSON 文件读取 6 要素生成 `/goal`
- 批量过滤、排序、limit 试跑、去重、摘要输出、strict/check/fail-on-skipped 门禁
- 批量任务 6 要素字段语义质量门禁
- 批量任务依赖计划、未知依赖和循环依赖检查
- 批量任务依赖顺序生成和检查
- 批量 JSON 报告、输出目录、输出文件、团队默认值配置

不适合非编码任务、主要依赖人工判断的设计决策，或只需要一次性小改动的场景。
