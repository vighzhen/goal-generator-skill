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
│   ├── sample_tasks.jsonl
│   ├── sample_tasks.csv
│   ├── sample_tasks.yaml
│   └── sample_tasks.md
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

## 使用方式

### 分析需求完整度

使用 `--analyze` 检查用户任务描述是否包含 6 个必要要素，并以 JSON 输出缺失项和追问示例：

```bash
python3 scripts/generate_goal.py --analyze "我要让 Codex 帮我给项目加单元测试"
```

### 生成任务类型画像和推荐模板

使用 `--profile` 可在追问前识别任务类型、复杂度、风险评分、追问策略，并给出该类型下 6 要素的推荐填写方向：

```bash
python3 scripts/generate_goal.py --profile "修复登录 API 在空 token 场景下偶发 500 的问题"
```

该功能适合把一句话需求快速转成“应该追问什么、优先确认什么、风险在哪里、每个要素怎么写”的模板化建议。

### 评估任务可执行度

使用 `--score` 可快速得到任务描述距离“可直接生成高质量 `/goal`”的评分、等级、缺失项、风险和下一步建议：

```bash
python3 scripts/generate_goal.py --score "给项目加单元测试"
```

该功能适合在接到一句话需求时先做质量门禁，判断是直接生成、简单复核还是必须补信息。

### 对比两个任务描述

使用 `--compare` 可一次对比两个任务描述的可执行度、风险、缺失要素差异，并判断哪个更适合直接生成 `/goal`、哪个更应该先补信息：

```bash
python3 scripts/generate_goal.py --compare "给项目加单元测试" "为 src/services/ 补齐 pytest 单元测试，运行 pytest tests/services -q"
```

该功能适合评审多个候选需求、排优先级或快速定位更不完整的任务描述。

### 生成 Markdown 评审卡片

使用 `--review-card` 可把单个任务的可执行度、任务类型、风险、缺失要素、推荐 6 要素草稿和可直接发送的追问文案合成一份 Markdown：

```bash
python3 scripts/generate_goal.py --review-card "给项目加单元测试"
```

该功能适合把任务质量评审结果贴到 PR、Issue、评审文档或团队群聊中。

### 生成 6 要素字段建议

使用 `--suggest-fields` 可把任务描述转成可编辑、可机器读取的 6 要素 JSON 草稿；已识别字段会保留输入内容，缺失字段会填入该任务类型下的推荐补全方向：

```bash
python3 scripts/generate_goal.py --suggest-fields "给项目加单元测试"
```

该功能适合 IDE、表单或团队自动化先生成字段草稿，再让人工补齐后交给 `--generate --from-json`。

### 校验 6 要素字段 JSON

使用 `--validate-fields-json` 可在执行 `--generate --from-json` 前检查 JSON 是否包含完整且非空的 6 要素字段、是否存在未知字段，以及是否能正常渲染成 `/goal`：

```bash
python3 scripts/generate_goal.py --validate-fields-json goal_fields.json
```

命令支持直接字段对象，也支持 `--suggest-fields` 输出的 `{ "fields": { ... } }` 包装结构；校验通过时退出码为 0，否则退出码为 1 并输出修复建议。

### 解释缺失要素

使用 `--explain-missing` 可在 `--analyze` 的缺失项基础上进一步输出“为什么缺、优先补什么、推荐怎么填”和可直接发送给用户的追问文案：

```bash
python3 scripts/generate_goal.py --explain-missing "给项目加单元测试"
```

该功能适合新用户或产品/研发协作场景，避免只看到字段名却不知道如何补齐。

### 使用内置任务模板库

如果你已经知道任务类型，可以直接列出和读取内置模板，快速获得 6 要素填写方向：

```bash
python3 scripts/generate_goal.py --list-templates
python3 scripts/generate_goal.py --template bugfix
```

### 查看能力清单

使用 `--capabilities` 可输出当前单任务命令、批量输入格式、批量选项和模板 ID，适合工具集成或快速发现能力：

```bash
python3 scripts/generate_goal.py --capabilities
```

使用 `--examples` 可输出常见单任务和批量命令示例 JSON，适合新用户快速选择入口或被 IDE/脚本读取：

```bash
python3 scripts/generate_goal.py --examples
```

### 生成一次性追问文案

使用 `--questions` 可把缺失要素分析结果转成可直接发给需求方的中文追问文本：

```bash
python3 scripts/generate_goal.py --questions "我要让 Codex 帮我给项目加单元测试"
```

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

如果只有一句话需求、但想先得到可编辑草稿，可以使用 `--draft` 自动填充缺失要素并明确标注默认填充项：

```bash
python3 scripts/generate_goal.py --draft "给项目加单元测试"
```

### 校验已有 /goal 文件

如果 `/goal` 指令经过手工编辑、复制或批量拼接，可以用 `--validate-goal-file` 检查分隔线、5 段结构和 6 要素提示是否完整：

```bash
python3 scripts/generate_goal.py --validate-goal-file goal.txt
```

命令会输出 JSON 校验报告；结构完整时退出码为 0，否则退出码为 1，便于在脚本或 CI 中拦截坏指令。

### 交互模式

使用 `--interactive` 可按提示输入任务描述和补充信息，由脚本循环分析缺失要素并生成最终指令：

```bash
python3 scripts/generate_goal.py --interactive
```

### 批量生成

当需要一次为多个编码任务生成 `/goal` 指令时，使用 `scripts/batch_generate.py` 从 JSON、JSONL、CSV、YAML 或 Markdown 表格读取任务列表。

JSON 输入格式：

```json
[
  {
    "name": "代码质量优化",
    "description": "对50个非测试Python文件做7维度代码质量优化",
    "fields": {
      "outcome": "对50个非测试 Python 文件做 7 维度代码质量优化",
      "verification": "每个改动后执行 python -m py_compile"
    }
  }
]
```

JSONL 输入格式适合流水线或脚本逐行追加任务，每行一个 JSON 对象：

```jsonl
{"name":"登录接口Bug修复","description":"修复登录 API 在空 token 场景下偶发 500 的问题","fields":{}}
{"name":"服务层单元测试","description":"为 src/services/ 补齐 pytest 单元测试，运行 pytest tests/services -q","fields":{"constraints":"不改业务逻辑"}}
```

CSV 输入格式包含以下表头：

```csv
name,description,outcome,verification,constraints,boundaries,iteration,blocked
```

YAML 输入支持常见的任务列表子集，适合复用项目配置或产品文档：

```yaml
- name: 服务层单元测试
  description: 为 src/services/ 补齐 pytest 单元测试，运行 pytest tests/services -q
  fields:
    constraints: 不改业务逻辑
```

Markdown 表格输入适合直接复用需求文档、Issue 或 README 中的任务清单，表头可使用英文或中文别名：

```markdown
| name | description | constraints |
| --- | --- | --- |
| 服务层单元测试 | 为 src/services/ 补齐 pytest 单元测试，运行 pytest tests/services -q | 不改业务逻辑 |
```

常用命令：

```bash
# 只分析 3 个示例任务的要素完整度，不生成指令
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run

# 从 JSON 批量生成并输出到控制台
python3 scripts/batch_generate.py --input examples/sample_tasks.json

# 从 JSONL 批量分析，适合脚本或流水线逐行生成任务
python3 scripts/batch_generate.py --input examples/sample_tasks.jsonl --dry-run

# 从标准输入读取 JSONL，适合 CI 或脚本管道临时生成任务
cat examples/sample_tasks.jsonl | python3 scripts/batch_generate.py --input - --stdin-format jsonl --dry-run

# 从 CSV 批量分析，适合用 Excel 编辑后检查
python3 scripts/batch_generate.py --input examples/sample_tasks.csv --dry-run

# 从 YAML 批量分析，适合复用项目配置或产品任务清单
python3 scripts/batch_generate.py --input examples/sample_tasks.yaml --dry-run

# 从 Markdown 表格批量分析，适合复用需求文档里的任务清单
python3 scripts/batch_generate.py --input examples/sample_tasks.md --dry-run

# 严格模式：缺失 6 要素的任务会跳过，不使用默认填充，适合 CI 或团队交付前检查
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --strict

# 校验清单快捷模式：等价于 dry-run + strict + summary-only + fail-on-skipped
python3 scripts/batch_generate.py --input examples/sample_tasks.json --check --report-json batch_report.json

# CI 门禁：只要存在跳过任务就返回非零退出码，同时仍输出摘要和报告
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --strict --fail-on-skipped --report-json batch_report.json

# 使用团队默认值覆盖缺失要素的默认填充，适合统一验证命令、约束和受阻规则
python3 scripts/batch_generate.py --input examples/sample_tasks.json --defaults-json team_defaults.json

# 也可以用环境变量配置团队默认值文件；命令行 --defaults-json 优先级更高
GOAL_GENERATOR_DEFAULTS_JSON=team_defaults.json python3 scripts/batch_generate.py --input examples/sample_tasks.json

# 输出结构化 JSON 报告，便于 CI、IDE 或脚本读取每个任务的缺失项和跳过原因
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --report-json batch_report.json

# 输出 Markdown 报告，便于贴到 PR、评审文档或团队群里人工审阅
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --report-md batch_report.md

# 输出缺失要素补全报告，便于团队只聚焦哪些任务还需要补信息
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --missing-report-md missing_report.md

# 在结构化报告中附加每个任务的类型、风险评分和追问策略，适合团队评审
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --include-profile --report-json batch_report.json

# 只输出任务类型、风险和缺失要素摘要，适合命令行快速评审批量清单
python3 scripts/batch_generate.py --input examples/sample_tasks.json --profile-summary --sort-by name

# 只输出 /goal 可执行度评分摘要，适合先找出最需要补信息的任务
python3 scripts/batch_generate.py --input examples/sample_tasks.json --score-summary --sort-by name

# 输出 /goal 可执行度 Markdown 报告，适合贴到 PR 或评审文档
python3 scripts/batch_generate.py --input examples/sample_tasks.json --score-report-md score_report.md

# 可执行度门禁：低于 65 分的任务会让命令返回非零退出码，适合 CI 或交付前检查
python3 scripts/batch_generate.py --input examples/sample_tasks.json --fail-below-score 65 --report-json score_gate.json

# 为每个任务导出可编辑的 6 要素字段 JSON 草稿，适合先分发补齐再生成 /goal
python3 scripts/batch_generate.py --input examples/sample_tasks.json --export-fields-json field_drafts/

# 只处理任务名或描述匹配正则的任务，适合从大清单中重跑某一类任务
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --filter "测试|Bug"

# 只处理前 2 个任务，适合大清单首次接入时快速试跑
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --limit 2

# 只预览将要处理的任务名称，适合检查 filter/sort/limit 是否命中正确范围
python3 scripts/batch_generate.py --input examples/sample_tasks.json --list-tasks --filter "测试|Bug"

# 按任务名稳定排序输出，适合多人维护的大清单审计和结果比对
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --sort-by name

# 跳过重复任务，适合多人维护或多来源合并后的任务清单
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --dedupe --report-json batch_report.json

# 只看最终摘要，不在终端打印每个任务正文，适合大清单或配合 output/report 使用
python3 scripts/batch_generate.py --input examples/sample_tasks.json --dry-run --summary-only

# 每个任务生成一个独立 .txt 文件，输出目录不存在时会自动创建
python3 scripts/batch_generate.py --input examples/sample_tasks.json --output-dir output/

# 为批量输出产物生成 Markdown 导航索引，方便在 PR 或文档中快速打开对应文件
python3 scripts/batch_generate.py --input examples/sample_tasks.json --output-dir output/ --index-md output/index.md

# 所有任务写入同一个文件
python3 scripts/batch_generate.py --input examples/sample_tasks.json --output-file all_goals.txt
```

输入文件推荐使用 `--input` 显式指定；脚本也兼容 `python3 scripts/batch_generate.py examples/sample_tasks.json --dry-run` 这种位置参数写法。传入 `--input -` 或位置参数 `-` 时，脚本会从标准输入读取任务流，并用 `--stdin-format jsonl`（默认）或 `--stdin-format json` 指定格式。`--output-dir` 和 `--output-file` 互斥，不能同时指定。任务中缺失的 6 要素会先尝试从 `description` 分析补齐；仍缺失时默认使用交互模式同款默认值填充，并在输出中标注默认填充的要素；若传入 `--defaults-json <path>`，可用 JSON 中的 6 要素字段覆盖这些默认值；如果没有传入该参数，脚本会读取 `GOAL_GENERATOR_DEFAULTS_JSON` 环境变量作为团队默认值文件。若传入 `--list-tasks`，脚本只输出当前筛选、排序和限制后的任务名称，便于预览处理范围；若传入 `--profile-summary`，脚本只输出任务名称、类型、风险和缺失要素摘要，便于命令行快速评审；若传入 `--score-summary`，脚本只输出任务名称、`/goal` 可执行度分数、等级、风险和缺失要素，便于先定位最需要补信息的任务；若传入 `--score-report-md <path>`，脚本会写出可分享的 Markdown 可执行度评分报告。若传入 `--fail-below-score <0-100>`，脚本会启用可执行度阈值门禁，任一任务低于阈值时返回退出码 1，并可通过 `--report-json` 或 `--score-report-md` 留存失败清单。若传入 `--export-fields-json <dir>`，脚本会为每个任务写出一份可编辑字段建议 JSON，批量输入中显式提供的字段会覆盖自动建议，适合先分发补齐再用 `--validate-fields-json` 和 `--generate --from-json` 生成最终指令。若传入 `--check`，脚本会启用 `--dry-run --strict --summary-only --fail-on-skipped` 的校验组合，适合 CI 或交付前检查。若传入 `--strict`，仍缺失要素的任务会被跳过，用于质量门禁或 CI 检查；再配合 `--fail-on-skipped` 可让跳过任务转成非零退出码。若传入 `--report-json <path>`，脚本会额外写出成功任务、缺失项、默认填充项、输出路径、跳过原因和修复建议，方便自动化集成；同时传入 `--include-profile` 时，每个成功任务会追加任务类型、复杂度、风险评分、风险因素、追问策略和推荐 6 要素模板。若传入 `--report-md <path>`，脚本会写出适合人工审阅的 Markdown 批量报告，包含成功任务和跳过任务表格。若传入 `--missing-report-md <path>`，脚本会写出聚焦缺失要素的 Markdown 补全报告，列出需补全任务、风险等级、默认填充和推荐补法。若传入 `--index-md <path>`，脚本会为批量输出产物生成 Markdown 导航索引。若传入 `--filter <regex>`，脚本只处理任务名或描述匹配正则的任务，便于从大清单中局部重跑。若传入 `--sort-by name`，脚本会按任务名排序，默认 `--sort-by input` 保持输入顺序。若传入 `--limit <N>`，脚本只处理筛选和排序后的前 N 个任务，适合首次试跑。若传入 `--dedupe`，脚本会按任务名和描述跳过重复任务，并在 JSON 报告的 `skipped` 中记录原因。若传入 `--summary-only`，脚本不会在 stdout 打印每个任务正文，只保留最终摘要，适合大批量检查。

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
- 批量任务指令生成（JSON、JSONL、CSV、YAML、Markdown 表格）
- 任务类型画像与 6 要素模板推荐
- 单任务 `/goal` 可执行度评分和下一步建议
- 单任务候选描述可执行度对比
- 单任务 Markdown 评审卡片
- 单任务可编辑 6 要素字段建议 JSON
- 单任务 6 要素字段 JSON 质量校验
- 批量可编辑 6 要素字段 JSON 草稿导出
- 批量 `/goal` 可执行度评分摘要
- 批量 `/goal` 可执行度阈值门禁
- 批量 `/goal` 可执行度 Markdown 报告
- 缺失 6 要素的原因解释、优先级和补全建议
- 内置任务模板库（测试、Bug 修复、重构、文档、通用任务）
- 机器可读能力清单输出
- 机器可读命令示例清单
- 已有 `/goal` 文件结构校验
- 单任务一句话草稿生成
- 一键生成可复制的缺失要素追问文案
- 单任务 analyze/profile/questions/generate 输出写入文件
- 单任务从 JSON 文件读取 6 要素生成 `/goal`

不适合非编码任务、主要依赖人工判断的设计决策，或只需要一次性小改动的场景。
