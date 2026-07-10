# Cleanup Report

## 功能盘点

| 功能名称 | 对应命令 | 代码行数 | 依赖功能 |
| --- | --- | --- | --- |
| 单任务 6 要素分析 | `generate_goal.py --analyze <description>` | `scripts/generate_goal.py:328-345,443-445,471` | 6 要素关键词规则、`AnalysisResult` |
| 单任务任务画像/模板推荐 | `generate_goal.py --profile <description>` | `scripts/generate_goal.py:152-222,401-403,472,1009-1027,1759-1842` | `--analyze`、内置 profile 规则 |
| 单任务可执行度评分 | `generate_goal.py --score <description>` | `scripts/generate_goal.py:404-406,473,1030-1052,1587-1656` | `--analyze`、`--profile`、风险启发式 |
| 单任务描述对比 | `generate_goal.py --compare <a> <b>` | `scripts/generate_goal.py:407-412,474,1055-1071,1499-1585` | `--score` |
| 单任务 Goal JSON 草稿 | `generate_goal.py --goal-json <description>` | `scripts/generate_goal.py:413-415,475,1074-1149` | `--suggest-fields`、`--score`、`--profile`、`--validate-fields-json` |
| 单任务敏感信息检查 | `generate_goal.py --redaction-check <description>` | `scripts/generate_goal.py:265-299,416-418,476,1152-1237` | 敏感正则、脱敏预览 |
| 单任务 Markdown 就绪度卡片 | `generate_goal.py --readiness-md <description>` | `scripts/generate_goal.py:419-421,477,1240-1308` | `--score`、`--profile`、`--analyze` |
| 单任务 Markdown 风险卡片 | `generate_goal.py --risk-card <description>` | `scripts/generate_goal.py:422-424,478,1341-1403` | `--score`、`--profile` |
| 单任务 Markdown 评审卡片 | `generate_goal.py --review-card <description>` | `scripts/generate_goal.py:425-427,479,1311-1338,1455-1484` | `--score`、`--suggest-fields`、`--explain-missing` |
| 单任务追问包 JSON | `generate_goal.py --questions-json <description>` | `scripts/generate_goal.py:428-430,480,1405-1452` | `--analyze`、`--profile`、`--score`、`--questions` |
| 单任务字段建议 JSON | `generate_goal.py --suggest-fields <description>` | `scripts/generate_goal.py:431-433,481,1659-1687` | `--analyze`、`--profile`、字段抽取 |
| 单任务缺失要素解释 | `generate_goal.py --explain-missing <description>` | `scripts/generate_goal.py:434-439,482,1690-1728` | `--analyze`、`--profile`、`--questions` |
| 内置模板列表 | `generate_goal.py --list-templates` | `scripts/generate_goal.py:386-388,483,515-519` | `PROFILE_RULES` |
| 机器可读能力清单 | `generate_goal.py --capabilities` | `scripts/generate_goal.py:389-391,484,522-598` | 参数清单、模板列表 |
| 机器可读命令示例 | `generate_goal.py --examples` | `scripts/generate_goal.py:392-394,485,601-773` | 参数清单、示例文本 |
| 指定模板 JSON | `generate_goal.py --template <id>` | `scripts/generate_goal.py:395-397,486,776-785` | `PROFILE_RULES` |
| 指定模板 Markdown | `generate_goal.py --template-md <id>` | `scripts/generate_goal.py:398-400,487,788-816` | `--template`、Markdown 表格渲染 |
| 一次性追问文本 | `generate_goal.py --questions <description>` | `scripts/generate_goal.py:440-442,488,1731-1740` | `--analyze` |
| 完整 `/goal` 生成 | `generate_goal.py --generate --outcome ...` / `--from-json` | `scripts/generate_goal.py:348-371,457-459,489,494,1845-1872,2117-2316` | 6 要素字段、模板渲染、分支/提交规则推断 |
| `/goal` 文件结构校验 | `generate_goal.py --validate-goal-file <file>` | `scripts/generate_goal.py:446-449,490,819-1006` | 固定分隔线和 5 段结构常量 |
| 6 要素字段 JSON 校验 | `generate_goal.py --validate-fields-json <file>` | `scripts/generate_goal.py:450-453,491,825-947` | `render_goal_text`、6 要素字段规则 |
| 单任务默认草稿 | `generate_goal.py --draft <description>` | `scripts/generate_goal.py:454-456,492,1743-1756` | 字段抽取、交互默认值、`render_goal_text` |
| 单任务交互式补全 | `generate_goal.py --interactive` | `scripts/generate_goal.py:460-461,493,1875-1902` | `--analyze`、`--questions`、默认值 |
| 单任务输出到文件 | `generate_goal.py --output-file <path>` | `scripts/generate_goal.py:495,505-512` | 所有单任务输出入口 |
| 批量 JSON/CSV/JSONL/YAML/Markdown/STDIN 输入 | `batch_generate.py [--input] <path>` / `--stdin-format` | `scripts/batch_generate.py:32-40,181,247-249,286-304,883-1125` | 任务加载器、字段映射 |
| 批量输出到目录/文件 | `batch_generate.py --output-dir/--output-file` | `scripts/batch_generate.py:250-252,220,1343-1369` | 批量核心处理、slug 生成 |
| 批量团队默认值 | `batch_generate.py --defaults-json` / `GOAL_GENERATOR_DEFAULTS_JSON` | `scripts/batch_generate.py:46,253,208-210,869-880` | 默认字段、JSON 加载 |
| 批量 JSON 报告 | `batch_generate.py --report-json` | `scripts/batch_generate.py:254,225-234,1372-1389,1648-1857,2300-2351` | 批量核心结果、跳过任务 |
| 批量 Markdown 报告 | `batch_generate.py --report-md` | `scripts/batch_generate.py:255,235-236,1434-1456,1594-1645` | 批量核心结果、Markdown 表格 |
| 批量缺失要素 Markdown 报告 | `batch_generate.py --missing-report-md` | `scripts/batch_generate.py:256,237-238,1459-1483,1548-1584` | 批量核心结果、`--profile` |
| 批量字段状态 CSV | `batch_generate.py --field-status-csv` | `scripts/batch_generate.py:47-59,257,213-214,672-691,1486-1545` | 批量 dry-run、`--score` |
| 批量输出索引 Markdown | `batch_generate.py --index-md` | `scripts/batch_generate.py:258,223-224,1392-1431` | 输出目录/文件路径 |
| 批量 JSON 报告附加画像 | `batch_generate.py --include-profile` | `scripts/batch_generate.py:259,233,344-345,2300-2339` | `--profile`、`--report-json` |
| 批量任务过滤 | `batch_generate.py --filter <regex>` | `scripts/batch_generate.py:260,182,307-318` | 任务加载结果 |
| 批量排序 | `batch_generate.py --sort-by input/name` | `scripts/batch_generate.py:261,183,321-326` | 任务加载结果 |
| 批量 limit 试跑 | `batch_generate.py --limit <N>` | `scripts/batch_generate.py:262,184,329-334` | 任务加载结果 |
| 批量任务列表预览 | `batch_generate.py --list-tasks` | `scripts/batch_generate.py:263,206-207,337-365,1648-1663` | 任务加载、去重、JSON 报告 |
| 批量画像终端摘要 | `batch_generate.py --profile-summary` | `scripts/batch_generate.py:264,204-205,368-381,764-785,833-854` | `--profile` |
| 批量可执行度摘要/报告/门禁 | `batch_generate.py --score-summary` / `--score-report-md` / `--fail-below-score` | `scripts/batch_generate.py:265-266,273,202-203,384-442,787-807,1683-1720,1860-1891,2268-2297` | `--score` |
| 批量 Markdown 评审看板 | `batch_generate.py --review-board-md` | `scripts/batch_generate.py:267,196-197,444-458,2205-2265` | 批量 score summary |
| 批量 Markdown 风险报告 | `batch_generate.py --risk-report-md` | `scripts/batch_generate.py:268,198-199,478-492,1921-1944,2089-2146` | 批量 profile summary |
| 批量追问 Markdown 包 | `batch_generate.py --questions-md` | `scripts/batch_generate.py:269,194-195,461-475,810-830,1836-1857,1894-1918,2149-2203` | `--questions-json` |
| 批量敏感信息 Markdown 报告 | `batch_generate.py --redaction-report-md` | `scripts/batch_generate.py:270,192-193,510-524,544-564,1763-1783,1947-2087` | `--redaction-check` |
| 批量 Goal JSONL 草稿 | `batch_generate.py --draft-jsonl` | `scripts/batch_generate.py:271,188-189,495-507,619-656,1746-1760,1822-1833` | `--goal-json` |
| 批量 Markdown 就绪度矩阵 | `batch_generate.py --readiness-matrix-md` | `scripts/batch_generate.py:272,190-191,527-542,567-617,1786-1819,1974-2049` | `--score`、`--profile`、6 要素状态 |
| 批量字段建议导出 | `batch_generate.py --export-fields-json` | `scripts/batch_generate.py:274,200-201,659-761,1722-1743` | `--suggest-fields` |
| 批量去重 | `batch_generate.py --dedupe` | `scripts/batch_generate.py:275,219,353-365,1172-1181` | 任务名和描述归一化 |
| 批量跳过失败门禁 | `batch_generate.py --fail-on-skipped` / `--check` | `scripts/batch_generate.py:276,279,215-241` | 批量核心处理、strict |
| 批量摘要输出 | `batch_generate.py --summary-only` | `scripts/batch_generate.py:277,217,1343-1357` | 批量 stdout 输出控制 |
| 批量 dry-run | `batch_generate.py --dry-run` | `scripts/batch_generate.py:278,215,1311-1319` | 批量核心处理 |
| 批量 strict 模式 | `batch_generate.py --strict` | `scripts/batch_generate.py:280,216,1229-1244` | 批量核心处理、缺失要素检查 |
| 批量 verbose 日志 | `batch_generate.py --verbose` | `scripts/batch_generate.py:281,219,2388-2390` | 批量核心处理 |

## 价值评估

| 功能名称 | 评估结果 | 理由 | 处理方式 |
| --- | --- | --- | --- |
| 单任务 6 要素分析 | 保留 | 核心入口，直接回答“缺什么信息”，所有流程依赖。 | 保留 |
| 单任务任务画像/模板推荐 | 待定 | 模板推荐对补齐 6 要素有帮助，但复杂度/风险部分是启发式；删除会影响 explain-missing。 | 暂保留，后续可拆除风险打分 |
| 单任务可执行度评分 | 删除 | 启发式分数看似精确但不可作为可靠决策依据，且衍生大量展示/门禁功能。 | 删除 |
| 单任务描述对比 | 删除 | 只是基于不可靠评分的包装，真实价值低。 | 删除 |
| 单任务 Goal JSON 草稿 | 删除 | 中间产物仍需人工复核，和字段建议/校验重复。 | 删除 |
| 单任务敏感信息检查 | 保留 | 共享任务前避免泄露 token/邮箱/URL，有独立真实痛点。 | 保留 |
| 单任务 Markdown 就绪度卡片 | 删除 | 展示包装，信息来自 analyze/score/profile，低价值重复。 | 删除 |
| 单任务 Markdown 风险卡片 | 删除 | 展示包装，风险启发式不够可靠。 | 删除 |
| 单任务 Markdown 评审卡片 | 删除 | 展示包装，组合多个已有输出，维护成本高。 | 删除 |
| 单任务追问包 JSON | 删除 | 中间结构化产物，主要服务 IDE/机器人假想集成，核心用户可用 `--questions`。 | 删除 |
| 单任务字段建议 JSON | 删除 | 中间草稿仍需人工复核，容易被误当事实。 | 删除 |
| 单任务缺失要素解释 | 保留 | 比 `--analyze` 更适合新用户理解为何缺失，仍是补信息核心链路。 | 保留 |
| 内置模板列表 | 保留 | 低复杂度，支持模板复用。 | 保留 |
| 机器可读能力清单 | 删除 | 维护成本高且与实际参数易漂移，文档和 `--help` 已覆盖。 | 删除 |
| 机器可读命令示例 | 删除 | 与 README 重复，容易过期。 | 删除 |
| 指定模板 JSON | 保留 | 模板库核心能力。 | 保留 |
| 指定模板 Markdown | 删除 | 与模板 JSON 同信息不同格式，只保留 JSON。 | 删除 |
| 一次性追问文本 | 保留 | 核心交互体验，直接可用。 | 保留 |
| 完整 `/goal` 生成 | 保留 | 核心产物，必须保留并修正最终验证命令兼容性。 | 保留/简化 |
| `/goal` 文件结构校验 | 保留 | 可验证最终产物结构，价值明确。 | 保留 |
| 6 要素字段 JSON 校验 | 待定 | 对 `--from-json` 有价值，但中间 JSON 功能删除后使用频率下降。 | 暂保留 |
| 单任务默认草稿 | 删除 | 中间草稿自动填默认值，容易产生未确认事实。 | 删除 |
| 单任务交互式补全 | 保留 | 核心交互方式，适合终端用户。 | 保留 |
| 单任务输出到文件 | 保留 | 通用输出能力，低复杂度。 | 保留 |
| 批量 JSON/CSV 输入 | 保留 | 批量处理核心；JSON 最核心，CSV 对表格用户实用。 | 保留 |
| 批量 JSONL/YAML/Markdown/STDIN 输入 | 删除 | 格式变体过多，增加解析维护成本；保留 JSON/CSV 即可覆盖主场景。 | 删除 |
| 批量输出到目录/文件 | 保留 | 批量核心输出。 | 保留 |
| 批量团队默认值 | 保留 | 团队可统一缺失默认策略，真实痛点。 | 保留 |
| 批量 JSON 报告 | 保留 | 最适合自动化消费；作为唯一批量报告格式。 | 保留 |
| 批量 Markdown 报告 | 删除 | 与 JSON 报告同信息不同格式，保留 JSON。 | 删除 |
| 批量缺失要素 Markdown 报告 | 删除 | 与 dry-run/JSON 报告/问题追问重复。 | 删除 |
| 批量字段状态 CSV | 删除 | 低频导出格式，和 JSON 报告重复。 | 删除 |
| 批量输出索引 Markdown | 删除 | 展示/导航包装，低频。 | 删除 |
| 批量 JSON 报告附加画像 | 删除 | 画像启发式且会膨胀报告，核心报告保留基础字段。 | 删除 |
| 批量任务过滤 | 保留 | 大清单局部处理常用。 | 保留 |
| 批量排序 | 保留 | 稳定输出顺序，低复杂度。 | 保留 |
| 批量 limit 试跑 | 保留 | 大清单试跑常用。 | 保留 |
| 批量任务列表预览 | 保留 | 可先确认过滤/排序/限制结果，低复杂度。 | 保留 |
| 批量画像终端摘要 | 删除 | 画像启发式摘要，与核心 dry-run 重复。 | 删除 |
| 批量可执行度摘要/报告/门禁 | 删除 | 依赖不可靠分数，门禁误导性强。 | 删除 |
| 批量 Markdown 评审看板 | 删除 | 展示包装，依赖评分。 | 删除 |
| 批量 Markdown 风险报告 | 删除 | 展示包装，依赖启发式风险。 | 删除 |
| 批量追问 Markdown 包 | 删除 | 展示包装，保留单任务 `--questions` 和批量核心报告即可。 | 删除 |
| 批量敏感信息 Markdown 报告 | 删除 | Markdown 包装低频；保留单任务 redaction-check。 | 删除 |
| 批量 Goal JSONL 草稿 | 删除 | 中间产物仍需人工复核，和批量生成主流程重复。 | 删除 |
| 批量 Markdown 就绪度矩阵 | 删除 | 展示包装且依赖评分，低价值。 | 删除 |
| 批量字段建议导出 | 删除 | 中间产物仍需人工复核。 | 删除 |
| 批量去重 | 保留 | 多来源任务合并常见痛点，低复杂度。 | 保留 |
| 批量跳过失败门禁 | 保留 | 基于真实缺失/跳过而非启发式分数，适合 CI。 | 保留 |
| 批量摘要输出 | 保留 | 大批量时抑制正文，低复杂度。 | 保留 |
| 批量 dry-run | 保留 | 批量核心验证入口。 | 保留 |
| 批量 strict 模式 | 保留 | 真实缺失要素门禁。 | 保留 |
| 批量 verbose 日志 | 保留 | 调试批量处理，低复杂度。 | 保留 |

## 清理执行

| 功能名称 | 处理方式 | Commit | 验证结果 |
| --- | --- | --- | --- |
| 待执行 | 待执行 | - | - |

## 文档同步

| 文件 | 改动内容 | Commit |
| --- | --- | --- |
| 待执行 | 待执行 | - |

## 最终总结

删除 0 个功能，保留 0 个功能，简化 0 个功能
代码减少 0 行
保留功能清单：（清理完成后更新）
剩余风险：（清理完成后更新）
