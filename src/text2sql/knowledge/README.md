# 加载、快照与产物治理

## 职责

schema_contract.py 拒绝不完整结构并区分有效单列 FK；governance.py 定义双人审核、内容/SQL/Schema摘要和执行证据；models.py 的指标/策略/Gold合同严格闭合。旧残缺快照与部分语义预期不能自动升级为权威资产。

models.py/structured.py 校验结构化记录，provenance.py 计算结构身份，snapshot.py 保存完整知识，artifacts.py 管理候选/指针/租约/保留，atomic.py 进行原子文件发布。

input_snapshot.py 固定源文件字节、目录清单及身份，解析与摘要不能分开重读；collection_evidence.py 的 v2 证据绑定实际 Chroma 文档/逐条 metadata/embedding、注册索引及集合级检索配置，configuration_sha256 单独标识配置，sha256 同时覆盖记录与配置。摘要不泄露配置原文，未知/不可验证 embedding 配置失败关闭。生产快照重新检查审核、粒度和单位，不能用文件存在或自算指纹替代审核。

## 边界

人工源目录在根 knowledge/；生产学习读取同一获批 Gold generation，在线只消费活动快照。stage不切ACTIVE，正式晋升由release入口独占执行。已批准资产不覆写，疑似过期锁也不自动抢占。候选失败不切换指针，不把待审材料当批准真值；实时 Schema 仍是结构权威。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run pytest -q tests/test_structured_knowledge.py tests/test_knowledge_artifacts.py tests/test_snapshot_approval.py tests/test_collection_evidence.py tests/test_training_pipeline.py
~~~

## 扩展模板

新增记录类型需字段、对象引用、批准状态、序列化快照与指纹测试；不补造旧产物缺失快照。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

恢复一整套版本/集合/报告/代码，不手工删活动目录求 ready。旧 v1 集合证据不能补字段复用，应保留追溯并重建候选、重评测、正式晋升。历史清理由受控保留策略管理。

[源码总览](../../README.md) · [专题说明](../../../docs/KNOWLEDGE_MAINTENANCE.md)
