# 语义计划与 SQL 正确性

## 职责

query_plan.py 定义类型计划；semantic_contract.py 共享完整版本化语义真值；semantic_ir.py/time_resolution.py 解析目录与时间；sql_compiler.py 编译支持子集；sql_scope.py/sql_semantics.py/sql_validation.py 校验作用域、安全和语义；plan_binding.py 核对冻结目录物理绑定。

result_contract.py 为执行、反馈和评测共享无损表格准入；拒绝大小写/规范化后重名列及行结构不一致，在DataFrame转字典前检查，防止静默丢列。AST也提前检查最终输出名，不能只依赖驱动转换后的结果。

## 边界

业务词汇/指标/实体由目录传入，不含本地业务硬编码。Domain 不依赖 API、模型、数据库或全局 Settings；未知能力澄清/拒答而不是猜测。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run pytest -q tests/test_query_plan.py tests/test_semantic_ast.py tests/test_sql_semantics_regression.py
~~~

## 扩展模板

能力必须同时补计划槽、编译、Validator、物理绑定及正反例；明确日期、NULL、重复、关联基数和字面量类型。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

禁止忽略未映射条件，不能把 SQL 可解析/可执行视为等价证明。复杂派生、窗口和多事实仍有边界。

[源码总览](../../README.md) · [专题说明](../../../docs/TEXT2SQL_FLOW.md)
