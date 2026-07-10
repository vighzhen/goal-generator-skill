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
│   ├── sample_tasks.csv
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

使用 `--profile` 可在追问前识别任务类型、复杂度、追问策略，并给出该类型下 6 要素的推荐填写方向：

```bash
python3 scripts/generate_goal.py --profile "修复登录 API 在空 token 场景下偶发 500 的问题"
```

该功能适合把一句话需求快速转成“应该追问什么、优先确认什么、每个要素怎么写”的模板化建议。

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

### 交互模式

使用 `--interactive` 可按提示输入任务描述和补充信息，由脚本循环分析缺失要素并生成最终指令：

```bash
python3 scripts/generate_goal.py --interactive
```

### 批量生成

当需要一次为多个编码任务生成 `/goal` 指令时，使用 `scripts/batch_generate.py` 从 JSON、CSV 或 Markdown 表格读取任务列表。

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

CSV 输入格式包含以下表头：

```csv
name,description,outcome,verification,constraints,boundaries,iteration,blocked
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

# 从 CSV 批量分析，适合用 Excel 编辑后检查
python3 scripts/batch_generate.py --input examples/sample_tasks.csv --dry-run

# 从 Markdown 表格批量分析，适合复用需求文档里的任务清单
python3 scripts/batch_generate.py --input examples/sample_tasks.md --dry-run

# 每个任务生成一个独立 .txt 文件，输出目录不存在时会自动创建
python3 scripts/batch_generate.py --input examples/sample_tasks.json --output-dir output/

# 所有任务写入同一个文件
python3 scripts/batch_generate.py --input examples/sample_tasks.json --output-file all_goals.txt
```

输入文件推荐使用 `--input` 显式指定；脚本也兼容 `python3 scripts/batch_generate.py examples/sample_tasks.json --dry-run` 这种位置参数写法。`--output-dir` 和 `--output-file` 互斥，不能同时指定。任务中缺失的 6 要素会先尝试从 `description` 分析补齐；仍缺失时使用交互模式同款默认值填充，并在输出中标注默认填充的要素。

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
- 批量任务指令生成（JSON、CSV、Markdown 表格）
- 任务类型画像与 6 要素模板推荐

不适合非编码任务、主要依赖人工判断的设计决策，或只需要一次性小改动的场景。
