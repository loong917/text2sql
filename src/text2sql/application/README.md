# 应用用例与端口

## 职责

text2sql_service.py 编排编译/生成/校验，query_execution.py 执行与反馈，context_service.py 组装冻结上下文，feedback_service.py 审核。ports.py/contracts.py 定义 Protocol 和类型对象。

## 边界

只依赖端口和 Domain，不直接构造模型/数据库。生产上下文不读取在线可变 Gold；必要约束不能预算截断。纯转发兼容包装已移除。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run pytest -q tests/test_application_service.py tests/test_query_integration.py tests/test_port_contracts.py
~~~

## 扩展模板

新增用例明确结果类型、拒答/基础设施错误、资源所有权和取消；不要通过 Any 或 tuple 破坏边界。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

校验失败不执行；执行失败不自动 SQL 试探；反馈写失败保留已完成结果。

[源码总览](../../README.md) · [专题说明](../../../docs/TEXT2SQL_FLOW.md)
