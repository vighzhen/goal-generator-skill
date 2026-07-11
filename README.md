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

# 更严格的 CI 门禁：语义质量得分低于 90 时失败
python3 scripts/generate_goal.py --lint-fields-json goal_fields.json --min-lint-score 90
```

命令输出 `score`、`issues`、`summary` 和原始结构校验结果；存在高优先级问题时退出码为 1，适合作为 `--generate --from-json` 前的质量门禁。追加 `--min-lint-score <0-100>` 时，报告会包含 `score_gate`，得分低于阈值也会返回非零退出码。

### 校验已有 /goal 文件

如果 `/goal` 指令经过手工编辑、复制或批量拼接，可以用 `--validate-goal-file` 检查分隔线、5 段结构和 6 要素提示是否完整：

```bash
python3 scripts/generate_goal.py --validate-goal-file goal.txt
```

### 检查已有 /goal 语义质量

结构完整的 `/goal` 仍可能存在字段空泛、验证方式不具体或边界不清的问题。使用 `--lint-goal-file` 会先做结构校验，再从概述中抽取 6 要素并复用字段语义质量门禁：

```bash
python3 scripts/generate_goal.py --lint-goal-file goal.txt

# 最终 /goal 文件得分低于 90 时失败
python3 scripts/generate_goal.py --lint-goal-file goal.txt --min-lint-score 90
```

命令输出 `validation`、`extracted_fields`、`field_lint` 和 `summary`；结构无效或语义质量未通过时退出码为 1。追加 `--min-lint-score <0-100>` 时，会用 `field_lint.score` 执行最低分门禁，并在报告中输出 `score_gate`。

### 检查 /goal 合集文件

当批量生成使用 `--output-file all_goals.txt`，或人工把多个标准 `/goal` 分隔块整理在同一个文本文件中时，可用 `--lint-goal-bundle` 逐段检查每个 `/goal` 块，而不是只把整个文件当作一个单任务文本：

```bash
python3 scripts/generate_goal.py --lint-goal-bundle all_goals.txt
```

命令会按 `==================== /goal 指令开始 ====================` 和 `==================== /goal 指令结束 ====================` 切分，输出 `goal_count`、`passed_count`、`failed_count`、`bundle_issues`，以及每个块的 `goal_index`、`start_line`、`end_line`、可选 `task_name`、结构校验和语义质量结果；任一块未通过、没有发现完整块或分隔线不配对时退出码为 1。

### 检查 /goal 输出目录

批量生成到 `--output-dir` 或人工整理多个 `/goal` 文本后，可用 `--lint-goal-dir` 一次性检查目录内所有 `.txt` 文件，避免逐个运行 `--lint-goal-file` 时漏检最终交付物：

```bash
python3 scripts/generate_goal.py --lint-goal-dir output/
```

命令按文件名稳定扫描目录直属 `.txt` 文件，输出 `file_count`、`passed_count`、`failed_count`、每个文件的结构与语义质量结果；如果某个 `.txt` 内包含多个标准 `/goal` 分隔块或分隔线不配对，会自动按合集文件逐段检查并在该文件报告中嵌入 `bundle` 详情。任一文件或任一合集块未通过、目录内没有 `.txt` 目标文件时退出码为 1，适合批量生成后的 CI 或交付前质量门禁。

### 递归检查 /goal 输出目录树

如果 `/goal` 文件按模块、依赖波次、任务来源或日期分散在多级子目录中，可用 `--lint-goal-tree` 递归检查整棵目录树下所有 `.txt` 文件：

```bash
python3 scripts/generate_goal.py --lint-goal-tree output/
```

命令会跳过 `.git`、缓存目录、依赖目录和构建产物目录，按相对路径稳定排序输出 `file_count`、`passed_count`、`failed_count`、`skipped_directories` 和每个文件的 `relative_path`、结构校验与语义质量结果；遇到合集文件时同样自动逐段检查并嵌入 `bundle` 详情。任一文件或任一合集块未通过、目录树内没有 `.txt` 目标文件时退出码为 1。若只想检查目录直属文件，继续使用 `--lint-goal-dir`。

### 自动检查 /goal 交付路径

如果 CI 或脚本只拿到一个交付路径，无法提前判断它是单个 `/goal` 文件、合集文件还是目录树，可用统一入口自动选择质量门禁：

```bash
python3 scripts/generate_goal.py --lint-goal-path output_or_goal.txt
```

文件路径会自动区分普通单目标文本与多个标准分隔块/分隔线不配对的合集文本；目录路径默认执行递归目录树检查。报告会包含 `auto_mode`（`file`、`bundle_file` 或 `directory_tree`），并保持任一文件或任一合集块未通过时退出码为 1。

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
    "path": "scripts",
    "depends_on": [],
    "fields": {
      "outcome": "对50个非测试 Python 文件做 7 维度代码质量优化",
      "verification": "每个改动后执行 python -m py_compile"
    }
  }
]
```

CSV 输入格式支持以下表头（`path`/`inspect_path`/`target_path`、`depends_on`/`dependencies` 可选，6 要素列可按需填写）：

```csv
name,description,path,depends_on,outcome,verification,constraints,boundaries,iteration,blocked
```

常用命令：

```bash
# 只分析示例任务的要素完整度，不生成指令
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run

# 位置参数也可作为输入文件路径
python3 scripts/batch_generate.py examples/sample_tasks.json --dry-run

# 从 JSON 批量生成并输出到控制台
python3 scripts/batch_generate.py --input examples/sample_tasks.json

# 从 JSON 批量生成前检查最终 /goal 文本质量，任一输出不合格则失败且不写出交付物
python3 scripts/batch_generate.py --input examples/sample_tasks.json --lint-output --report-json output_lint_report.json

# 批量最终输出最低分门禁：任一生成结果低于 95 分则失败且不写出交付物
python3 scripts/batch_generate.py --input examples/sample_tasks.json --lint-output --min-lint-score 95 --report-json output_lint_report.json

# 批量生成前查看任务类型、复杂度、风险和 6 要素缺口组合画像
python3 scripts/batch_generate.py --input examples/sample_tasks.json --profile-tasks --report-json task_profile.json

# 高风险画像门禁：存在 high 风险任务时返回非零退出码
python3 scripts/batch_generate.py --input examples/sample_tasks.json --profile-tasks --fail-on-high-risk --report-json task_profile.json

# 自定义画像风险阈值门禁：存在 medium/high 风险任务时返回非零退出码
python3 scripts/batch_generate.py --input examples/sample_tasks.json --profile-tasks --fail-on-risk-level medium --report-json task_profile.json

# 批量扫描任务 path/inspect_path/target_path 指向的本地路径，生成代码上下文画像
python3 scripts/batch_generate.py --input examples/sample_tasks.json --inspect-paths --report-json path_inspection.json

# 用路径画像 suggested_fields 回填缺失或仅由描述启发式推断的 6 要素，输出增强后的任务 JSON
python3 scripts/batch_generate.py --input examples/sample_tasks.json --enrich-from-paths --output-file enriched_tasks.json --report-json path_enrichment.json

# 从 CSV 批量分析，适合用表格工具编辑后检查
python3 scripts/batch_generate.py --input examples/sample_tasks.csv --dry-run

# 任务清单 Schema 门禁：检查 JSON/CSV 字段、表头、fields 对象和别名冲突
python3 scripts/batch_generate.py --input examples/sample_tasks.json --lint-task-schema --report-json task_schema_report.json

# 按任务生成可直接发送给需求方的缺失要素追问文案
python3 scripts/batch_generate.py --input examples/sample_tasks.json --questions --output-file batch_questions.txt

# 将按任务名收集到的补充回答合并回批量任务 fields，并输出新的任务 JSON
python3 scripts/batch_generate.py --input examples/sample_tasks.json --merge-supplements batch_answers.json --output-file merged_tasks.json --report-json merge_report.json

# 补充回答合并质量门禁：仍缺要素、输入错误或未匹配任务名时返回非零退出码
python3 scripts/batch_generate.py --input examples/sample_tasks.json --merge-supplements batch_answers.csv --check

# 严格模式：缺失 6 要素的任务会跳过，不使用默认填充
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --strict

# 名称唯一门禁：同名任务会跳过并返回非零退出码，避免补充回答和依赖引用歧义
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --require-unique-task-names --report-json batch_report.json

# 名称正则门禁：任务名必须以 Jira/Issue 编号开头
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --require-name-pattern '^[A-Z]+-[0-9]+' --report-json batch_report.json

# 路径必填门禁：每个任务必须提供 path/inspect_path/target_path 作为代码上下文锚点
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --require-task-path --report-json batch_report.json

# 路径存在门禁：每个任务路径字段必须指向当前工作目录下已存在的文件或目录
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --require-existing-task-path --report-json batch_report.json

# 路径根目录白名单门禁：每个任务路径必须位于 scripts/ 或 src/ 之内
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --allowed-path-roots scripts,src --report-json batch_report.json

# 默认填充数量门禁：每个任务最多允许默认填充 2 个要素，超限任务会跳过并返回非零退出码
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --max-defaulted-fields 2 --report-json batch_report.json

# 描述长度门禁：原始 description 低于 20 个字符的任务会跳过并返回非零退出码
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --min-description-length 20 --report-json batch_report.json

# 显式来源门禁：要求 verification 和 boundaries 必须由 fields 或 description 标签明确提供
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --require-explicit-fields verification,boundaries --report-json batch_report.json

# 禁用关键字段默认兜底：verification 可由 fields 或描述启发式提供，但不能使用默认值
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --forbid-default-fields verification,boundaries --report-json batch_report.json

# 校验清单快捷模式：等价于 dry-run + strict + summary-only + fail-on-skipped
python3 scripts/batch_generate.py --input examples/sample_tasks.json --check --report-json batch_report.json

# 批量检查任务 6 要素字段语义质量，适合生成前或 CI 门禁
python3 scripts/batch_generate.py --input examples/sample_tasks.json --lint-fields --report-json fields_lint.json

# 批量字段最低分门禁：任一任务低于 95 分时返回非零退出码
python3 scripts/batch_generate.py --input examples/sample_tasks.json --lint-fields --min-lint-score 95 --report-json fields_lint.json

# 批量审计任务名称、描述和字段中的 token、邮箱、URL 等敏感信息
python3 scripts/batch_generate.py --input examples/sample_tasks.json --redaction-check --report-json redaction_report.json

# CI 门禁：只要存在跳过任务就返回非零退出码，同时仍输出摘要和报告
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --strict --fail-on-skipped --report-json batch_report.json

# 使用团队默认值覆盖缺失要素的默认填充
python3 scripts/batch_generate.py --input examples/sample_tasks.json --defaults-json team_defaults.json

# 单独检查团队默认值文件合并后的 6 要素语义质量，不需要任务清单
python3 scripts/batch_generate.py --lint-defaults-json team_defaults.json --report-json defaults_lint.json

# 也可以用环境变量配置团队默认值文件；命令行 --defaults-json 优先级更高
GOAL_GENERATOR_DEFAULTS_JSON=team_defaults.json python3 scripts/batch_generate.py --input examples/sample_tasks.json

# 输出结构化 JSON 报告，便于 CI、IDE 或脚本读取每个任务的缺失项和跳过原因
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --report-json batch_report.json

# 只处理任务名或描述匹配正则的任务
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --filter "测试|Bug"

# 只处理前 2 个任务，适合大清单首次接入时快速试跑
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --limit 2

# 任务数量上限门禁：筛选和 limit 后仍超过 20 个任务时失败，不自动截断
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --max-task-count 20

# 非空任务集门禁：filter 拼错或上游导出为空时失败，避免成功处理 0 个任务
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --filter "测试|Bug" --require-non-empty

# 只预览将要处理的任务名称，适合检查 filter/sort/limit 是否命中正确范围
python3 scripts/batch_generate.py --input examples/sample_tasks.json --list-tasks --filter "测试|Bug"

# 生成依赖执行计划，检查 depends_on/dependencies 是否存在未知依赖或循环依赖
python3 scripts/batch_generate.py --input examples/sample_tasks.json --plan-dependencies --report-json dependency_plan.json

# 按 depends_on/dependencies 拓扑顺序生成或 dry-run，依赖无效时失败
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dependency-order --dry-run

# 依赖合法性主流程门禁：保持原顺序但阻断未知依赖、自依赖、重复名或循环依赖
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --require-valid-dependencies --report-json dependency_gate_report.json

# 依赖顺序一致性门禁：保持原顺序但要求依赖任务已出现在当前任务之前
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --require-dependency-order --report-json dependency_order_report.json

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

# 交付目标必填门禁：真实生成必须显式写入 output-file 或 output-dir，避免只落在 stdout 日志
python3 scripts/batch_generate.py --input examples/sample_tasks.json --output-file all_goals.txt --require-output-target

# 输出覆盖保护：目标正文文件、输出目录内将生成的 .txt 或 JSON 报告已存在时失败且不写入
python3 scripts/batch_generate.py --input examples/sample_tasks.json --output-dir output/ --report-json batch_report.json --no-overwrite
```

说明：

- `--input` 和位置参数都可指定输入文件；推荐在脚本中显式使用 `--input`。
- 当前批量输入只支持 JSON 和 CSV，其他格式不再作为核心能力维护。
- JSON 任务可选 `depends_on` 或 `dependencies` 字段；CSV 可选 `depends_on` 或 `dependencies` 表头，多个依赖用逗号、分号、顿号或换行分隔。
- JSON/CSV 任务可选 `path`、`inspect_path` 或 `target_path` 字段；`--inspect-paths` 会逐任务扫描对应本地文件或目录，输出 `language_counts`、`verification_hints`、`risk_flags`、`suggested_fields` 和路径错误，并在任一路径缺失或不可读时退出码为 1；`--enrich-from-paths` 会把路径画像中的 `suggested_fields` 回填到缺失或仅由描述启发式推断的 6 要素，不覆盖用户显式填写的字段。
- `--lint-task-schema` 不生成 `/goal` 正文，而是直接检查原始 JSON/CSV 任务清单结构；会报告未知任务字段、未知 `fields` 6 要素、非对象 `fields`、CSV 未知或重复表头、`description` 缺失或为空、`path`/`inspect_path`/`target_path` 以及 `depends_on`/`dependencies` 别名冲突等问题，任一问题都会返回非零退出码，并可配合 `--output-file`、`--summary-only` 和 `--report-json` 接入 CI。
- `--output-dir` 和 `--output-file` 互斥。
- `--require-output-target` 可用于真实批量生成或 `--lint-output`，要求必须指定 `--output-file` 或 `--output-dir`；如果只会把正文输出到 stdout，则在读取/生成前失败，适合 CI 或发布脚本要求交付物必须落盘的场景。该门禁不用于 `--dry-run`、`--check` 或分析模式。
- `--no-overwrite` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 的批量输出写入前保护；如果 `--output-file`、`--output-dir` 内本次将生成的 `.txt` 文件或 `--report-json` 已存在，或多个输出目标指向同一路径，命令会在写入任何输出文件前失败，适合 CI 和多人协作中保护历史交付物。该门禁不用于 `--questions`、`--profile-tasks`、`--lint-task-schema` 等分析模式。
- `--max-task-count <N>` 会在任务读取、`--filter`、`--sort-by` 和 `--limit` 后检查当前将处理的任务数量；超过阈值时直接失败，不会像 `--limit` 一样自动截断，适合防止 CI、机器人导出或多人合并时意外生成过大的任务批次。该门禁适用于读取任务清单的批量模式，不用于 `--lint-defaults-json` 或 `--lint-task-schema`。
- `--require-non-empty` 会在任务读取、`--filter`、`--sort-by` 和 `--limit` 后要求至少保留 1 个任务；如果任务集为空会直接失败，适合在 CI 中防止空清单或错误筛选条件被当作成功。该门禁不用于 `--lint-defaults-json` 或 `--lint-task-schema`，也不能与 `--max-task-count 0` 同用。
- 任务中缺失的 6 要素会先尝试从 `description` 分析补齐；仍缺失时默认使用交互模式同款默认值填充，并在输出中标注默认填充的要素。
- `--require-unique-task-names` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；同名任务会全部跳过并返回非零退出码，适合依赖计划、补充回答合并或人工审计前确保任务身份唯一。
- `--require-name-pattern <regex>` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；任务名不匹配给定 Python 正则时会跳过并返回非零退出码，适合把 Jira/Issue 编号、模块前缀或团队命名规范接入批量生成门禁。
- `--require-task-path` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；没有 `path`、`inspect_path` 或 `target_path` 的任务会跳过并让命令返回非零退出码，适合要求批量任务全部绑定代码上下文锚点的团队。
- `--require-existing-task-path` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；任务缺少路径字段或路径不存在时会跳过并返回非零退出码，适合生成前确保批量任务路径锚点在当前仓库中真实可访问。
- `--allowed-path-roots <根目录列表>` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；任务路径必须位于任一允许根目录内，根目录列表用逗号、分号、顿号或换行分隔。该门禁会要求任务提供路径字段，但不单独检查路径存在性；如需同时检查存在性可配合 `--require-existing-task-path`。
- `--max-defaulted-fields <0-6>` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；当某任务默认填充数量超过阈值时，该任务会跳过并让命令返回非零退出码，适合在完全 `--strict` 前做渐进式质量门禁。
- `--min-description-length <N>` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；任务原始 `description` 去除首尾空白后必须至少达到 N 个字符，否则会跳过并返回非零退出码，适合阻断“优化”“修 bug”这类信息量过低但非空的批量需求。
- `--require-explicit-fields <字段列表>` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；字段列表支持 6 要素 key、英文标签或中文标签，多个字段用逗号、分号、顿号或换行分隔。被要求的字段必须来自任务 `fields` 或 `description` 中的显式标签，不能只靠描述启发式推断或默认值兜底。
- `--forbid-default-fields <字段列表>` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；字段列表同样支持 6 要素 key、英文标签或中文标签。被禁止默认的字段可以来自任务 `fields`、`description` 显式标签或描述启发式识别，但不能落到默认值兜底。
- `--defaults-json <path>` 或 `GOAL_GENERATOR_DEFAULTS_JSON` 可覆盖默认填充策略。
- `--lint-defaults-json <path>` 不需要任务输入，会读取团队默认值文件（支持顶层 6 要素或 `fields` 包装）、合并交互默认值后检查语义质量；适合把默认值配置接入 CI，避免空泛默认值污染批量生成。
- `--report-json <path>` 是保留的批量报告格式，包含成功任务、缺失项、默认填充项、输出路径、跳过原因和修复建议。
- `--check` 适合 CI 或交付前检查；`--strict` 和 `--fail-on-skipped` 可组合成质量门禁。
- `--questions` 不生成 `/goal` 正文，而是按任务名汇总缺失要素并生成可直接发送的追问文案；可配合 `--output-file` 保存文案，配合 `--report-json` 保存任务级缺失结构。
- `--profile-tasks` 不生成 `/goal` 正文，而是逐任务输出任务类型、复杂度、风险分数/层级、缺失要素和字段来源，并汇总类型/复杂度/风险分布；可配合 `--filter`、`--limit`、`--sort-by`、`--dedupe`、`--summary-only`、`--output-file` 和 `--report-json` 做批量清单评审；若要在 CI 中阻断高风险任务，可追加 `--fail-on-high-risk`；若团队需要更严格或更宽松的阈值，可用 `--fail-on-risk-level <low|medium|high>` 阻断指定等级及以上风险任务，并在报告的 `task_profile.risk_gate` 中查看命中任务。
- `--merge-supplements <path>` 不生成 `/goal` 正文，而是读取按任务名组织的补充回答 JSON/CSV，把补充文本、显式 6 要素字段和原任务的 `description`/`fields` 合并为新的任务 JSON；可配合 `--output-file` 保存合并清单，配合 `--report-json` 查看字段来源、未匹配补充和剩余缺口，配合 `--check` 在仍缺要素、输入错误或补充任务名未匹配时失败退出。
- `--inspect-paths` 不生成 `/goal` 正文，而是批量复用单任务 `--inspect-path` 的代码事实采集能力；支持 `--filter`、`--limit`、`--sort-by`、`--dedupe`、`--summary-only`、`--output-file` 和 `--report-json`，适合在批量生成前统一补齐边界、验证命令和风险线索。
- `--enrich-from-paths` 不生成 `/goal` 正文，而是扫描任务路径并输出增强后的任务 JSON；它保留 `name`、`description`、`inspect_path`、`depends_on` 和用户已有 `fields`，只为缺失或仅由描述启发式推断的要素写入路径画像建议，并在 `--report-json` 的 `path_enrichment` 中记录字段来源、路径错误、剩余缺失和可生成状态。
- `--lint-output` 只用于真实生成模式，不与 `--dry-run` 或 `--check` 同用；它会在写出 stdout/`--output-file`/`--output-dir` 交付物前逐任务检查最终 `/goal` 文本的结构和语义质量，任一输出未通过时退出码为 1，并把 `output_lint` 写入 `--report-json`。可追加 `--min-lint-score <0-100>`，让任一最终输出低于最低分时失败，并在任务报告中输出 `score_gate`。
- `--lint-fields` 不生成 `/goal` 正文，而是逐个任务检查 6 要素字段的具体性、验证命令、边界、提交节奏和受阻条件；任一任务未通过时退出码为 1，可配合 `--report-json` 做批量质量门禁。可追加 `--min-lint-score <0-100>`，对每个任务字段得分执行最低分门禁并重算批量通过/失败计数。
- `--redaction-check` 不生成 `/goal` 正文，而是逐个任务审计名称、描述和 6 要素字段值中的 token、密钥、邮箱、URL 等敏感片段；发现风险时退出码为 1，并在报告中提供脱敏预览。
- `--plan-dependencies` 不生成 `/goal` 正文，而是输出按依赖分批的执行计划；发现未知依赖、重复任务名或循环依赖时退出码为 1。
- `--dependency-order` 会在生成、dry-run、list、lint 或 redaction 前应用依赖拓扑顺序；如果筛选/limit 后导致依赖缺失、循环或重复任务名，会直接失败并提示先运行 `--plan-dependencies` 查看详情。
- `--require-valid-dependencies` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；它复用依赖计划检查未知依赖、自依赖、重复任务名和循环依赖，不改变原任务顺序，发现问题时跳过对应任务并返回非零退出码，适合希望保留人工排序但仍把依赖图合法性接入 CI 的团队。
- `--require-dependency-order` 可用于真实生成、`--dry-run`、`--check` 或 `--lint-output` 前的准备流程；它不改变原任务顺序，但要求每个 `depends_on`/`dependencies` 引用的任务已经出现在当前任务之前，未知依赖、自依赖、重复任务名或顺序错误都会让相关任务跳过并返回非零退出码，适合保护人工排序或发布步骤顺序。

### 批量合并补充回答

`--merge-supplements` 用于承接 `--questions` 之后的需求方回答。补充文件可以是 JSON 对象映射、JSON 数组或 CSV。最简 JSON 对象映射中，键必须与任务清单的 `name` 完全一致，值可以是一段自然语言回答：

```json
{
  "补测试": "outcome: 为 src/services/payment.py 新增 12 个 pytest 用例；verification: 运行 python3 -m pytest tests/services/test_payment.py -q；constraints: 不改业务逻辑、不引入新依赖；boundaries: 仅处理 src/services/payment.py 和 tests/services/test_payment.py；iteration: 每个行为路径一组测试，验证后单独 commit；blocked: 无法推断断言时停下问人"
}
```

也可以使用对象数组，把显式字段放入 `fields`，把自然语言回答放入 `supplement` / `answer` / `response` / `text`：

```json
[
  {
    "name": "补测试",
    "supplement": "只测 src/services/payment.py，对应测试放在 tests/services/test_payment.py，每个行为路径单独 commit。",
    "fields": {
      "verification": "每次新增测试后运行 python3 -m pytest tests/services/test_payment.py -q，最终运行 python3 -m pytest tests/services -q",
      "blocked": "无法从现有代码推断业务预期或断言时停下问人；允许跳过项不超过 10%"
    }
  }
]
```

CSV 补充文件至少包含 `name` 表头，可选 `supplement`、`answer`、`response`、`text`、`description` 以及 6 要素字段列。合并时，原任务的 `fields` 作为基线，补充回答中的显式标签和字段会覆盖对应要素；自然语言回答会用于推断仍缺的要素。输出 JSON 保留 `name`、`description`、`inspect_path`、`depends_on` 和合并后的 `fields`，可继续交给 `--enrich-from-paths`、`--lint-fields`、`--check` 或批量生成命令。

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
- 批量任务类型、复杂度、风险和 6 要素缺口画像
- 批量任务画像高风险 CI 门禁
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
- 包含多个 `/goal` 块的合集文件逐段语义质量门禁
- `/goal` 输出目录结构与语义质量门禁
- `/goal` 嵌套输出目录树递归结构与语义质量门禁
- `/goal` 文件/合集/目录树统一路径自动质量门禁
- 一键生成可复制的缺失要素追问文案
- 批量任务缺失要素追问文案
- 批量追问回答合并回任务清单
- 原始需求与补充回答合并为 6 要素字段草稿
- 单任务 6 要素字段语义质量检查和生成前门禁
- 单任务输出写入文件
- 单任务从 JSON 文件读取 6 要素生成 `/goal`
- 批量过滤、排序、limit 试跑、去重、摘要输出、strict/check/fail-on-skipped 门禁
- 批量任务描述最小长度门禁
- 批量任务数量上限门禁
- 批量非空任务集门禁
- 批量任务清单 Schema 门禁（未知字段、重复表头、description 缺失和别名冲突）
- 批量交付目标必填门禁
- 批量输出覆盖保护门禁
- 团队默认值 JSON 语义质量门禁
- 批量任务 6 要素字段语义质量门禁
- 批量生成后最终 `/goal` 输出自检门禁
- 批量任务路径上下文画像、项目验证命令和风险线索汇总
- 批量路径建议字段回填为可生成任务 JSON
- 批量任务依赖计划、未知依赖和循环依赖检查
- 批量任务依赖顺序生成和检查
- 批量依赖合法性主流程门禁
- 批量依赖顺序一致性门禁
- 批量 JSON 报告、输出目录、输出文件、团队默认值配置

不适合非编码任务、主要依赖人工判断的设计决策，或只需要一次性小改动的场景。
