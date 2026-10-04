# 外部适配器与资源

## 职责

query_adapters.py 管理受限 SQL 线程、DBAPI 超时/取消；runtime.py 管理指定集合、SQL池与嵌入HTTP资源；context_adapters.py/schema_repository.py 提供结构；ollama_generator.py 约束模型输出；feedback_repository.py/migrations.py 管理 SQLite。

## 边界

资源有唯一所有者；借用资源不重复关。指定候选知识索引必须同时提供匹配 memory，不回落 ACTIVE 集合。关闭释放 owned SDK 池/传输和 Chroma 客户端引用，不直接 stop/reset Chroma 共享 System。

runtime.ChromaAgentMemory 是固定 Vanna SDK 的行为适配器，不是旧入口兼容层：已存在集合先检查存储配置，再显式传入 owned Ollama embedding function，避免 SDK 忽略注入而重建其他模型/传输。所有客户端使用相同禁遥测/禁 reset 设置；证据读取结束关闭临时客户端。HNSW 参数、集合级 metadata 或 embedding 模型/地址/超时变化会使 v2 集合证据失效。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run pytest -q tests/test_runtime_contracts.py tests/test_resource_lifecycle.py tests/test_collection_evidence.py tests/test_database_preflight.py
~~~

## 扩展模板

接新 Provider 实现既有 Protocol，写传输错误、弱类型、取消、线程/连接释放合同。涉及 SDK 私有关闭接口必须锁版本并回归。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

驱动/TLS/权限失败查真实库；不要关闭加密或删活动数据。历史被错误压缩的 SQL 不可自动还原，需重审。

[源码总览](../../README.md) · [专题说明](../../../docs/DB_TLS_RUNBOOK.md)
