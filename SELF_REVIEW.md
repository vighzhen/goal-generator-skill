# Self Review Report

## 第 1 轮

### 发现的问题

| 序号 | 优先级 | 文件 | 问题描述 | 处理状态 | Commit |
| --- | --- | --- | --- | --- | --- |
| 1 | P0 | scripts/batch_generate.py::_load_json_tasks | JSON 数组中出现非对象元素时，当前逻辑把错误文本当成 description 生成任务，导致解析失败任务没有被跳过，违背“解析失败的任务跳过并继续”的批量处理语义。 | 已修复 | 本提交 |
| 2 | P1 | scripts/generate_goal.py::_expected_commit_range | commit 数量识别只匹配包含 `commit` 的表达，无法识别“预期30-100个”这类中文常见写法，会把用户明确给出的范围误回退为默认 `4-12 个 commit`。 | 已修复 | 本提交 |
| 3 | P1 | scripts/batch_generate.py::_slugify | 文件名 slug 化只保留 ASCII 字母数字，中文任务名会退化成 `task-1`、`task-2`，没有真正基于 `name` 字段命名，用户难以对应输出文件和任务。 | 已修复 | 本提交 |
| 4 | P1 | scripts/generate_goal.py::INTERACTIVE_DEFAULTS | 默认约束写成“不改变既有功能行为”，当 Bug 修复任务缺少 constraints 时会与“修复错误行为”的 Outcome 冲突，生成自相矛盾的 /goal 指令。 | 待处理 | TBD |
| 5 | P2 | README.md | 适用场景列表尚未明确包含“批量任务指令生成”，而 SKILL.md 已声明支持该场景，文档入口信息不完全一致。 | 待处理 | TBD |

### 本轮总结

发现 5 个问题，修复 0 个，跳过 0 个。
