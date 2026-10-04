# 测试与回归资产

## 职责与分层

| 资产 | 证明范围 |
| --- | --- |
| test_query_plan、test_semantic_ast、test_sql_semantics_regression | 计划与 SQL 支持子集、错误反例 |
| test_port_contracts、test_runtime_contracts、test_resource_lifecycle | 类型边界、取消、关闭与线程许可 |
| test_query_integration、test_application_service | 应用端口组合行为 |
| test_api_e2e、test_auth_security | HTTP outcome、鉴权与同源会话 |
| test_release_evidence、test_runtime_gate、test_evaluation_runner | 冻结输入、报告与运行门禁 |
| test_snapshot_approval、test_gold_generation_contract、test_template_scope_fingerprint | 生产知识审核、canonical派生一致性与同形/反例隔离 |
| test_retrieval_input_snapshot、test_collection_evidence | 单次输入、校准器并发发布、实际内容/向量/检索配置身份、owned 嵌入与客户端生命周期 |
| test_result_contract、test_release_promotion、test_evaluation_output_policy | 防重复列丢失、候选/上线隔离、报告路径保护 |
| test_architecture、test_architecture_hardening | 依赖方向、惰性单飞与阻塞边界 |
| test_distribution | 脱离源码安装真实 wheel |
| test_documentation | README、链接与流程节点一致性 |

这些测试使用本地夹具，不代表真实 SQL Server/Ollama 的生产准确率、TLS 或性能验收。Chroma SDK 契约使用独立临时空库、显式向量和 mocked Ollama 响应；不读取项目现有集合，不执行真实模型请求。完整 SQL 加损坏 WHERE/ORDER BY/GROUP BY/UNION 的反例必须在任何训练写入前拒绝，记录不变但检索配置漂移也必须阻止评测、晋升和运行。

## 命令

~~~powershell
uv run pytest -q
uv build --wheel --out-dir dist/0.8.1
$env:TEXT2SQL_WHEEL_DIR = (Resolve-Path dist/0.8.1).Path
uv run pytest -q tests/test_distribution.py
~~~

不设置 wheel 路径时分发测试会跳过；正式本地/CI 验收必须构建并设置路径，目录只能含一个待验 wheel。临时目录权限失败先确认执行环境，不把沙箱失败当代码回归。

## 新回归模板

~~~text
缺陷/风险：
最小输入/目录/Schema：
原行为与错误结果：
预期 outcome/SQL/证据：
必须不发生的执行/晋升/资源泄漏：
边界：NULL/空结果/重复/取消/类型：
对应真实业务 Case ID（如有）：
~~~

测试应同时包含成功合同与禁止反例，不能只断言 SQL 字符串“包含某词”。异步测试须关闭拥有的资源，副本/临时目录不能指向用户运行数据。

## 维护与失败处理

保留错误原因，缩小定位到单测；修复后跑相关测试、静态门禁和全量回归。禁止跳过失败、降低质量门槛、修改第三方包或吞掉取消信号求绿。
