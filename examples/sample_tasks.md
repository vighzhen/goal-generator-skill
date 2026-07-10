| name | description | constraints |
| --- | --- | --- |
| 代码质量优化 | 对50个非测试Python文件做7维度代码质量优化，范围仅限 src/、app/、services/ 目录。 | 不修改测试代码，不改变功能逻辑，不引入新依赖，不做大重构 |
| 服务层单元测试 | 为 src/services/ 的核心逻辑补齐 pytest 单元测试，运行 pytest tests/services -q，边界：仅处理 src/services/ 和 tests/services/，迭代策略：每个模块一个 commit，受阻停止条件：无法从代码推断断言时停下问人。 | 不改业务逻辑，不引入新依赖，不修改公共 API |
