# 知识候选构建与 Dev 门禁

## 职责

pipeline.py 编排候选；knowledge_builder.py 构建训练文本；records.py 管元数据；profiling.py 做安全白名单画像；fingerprint.py/storage.py 绑定输入、原子存储；reporting.py 产出报告。

## 边界

不微调模型权重。候选构建读真实只读库/模型并写本地 Chroma、候选产物和状态；通过 Dev 仅调用 registry.stage，写 candidate_artifact.json 与 VERSION/artifact.json，不切 ACTIVE。正式晋升由 release-check --promote 在全部门禁通过后完成。检索指纹 v2 要求重新拟合与校准，不能复用旧证据。长期 Gold 禁止相对日期。

生产构建要求同一不可变 Gold generation、完整实时 Schema 和已审业务知识；快照中的审核重新校验，知识须与该 generation 匹配。运行 SQLite 的 Gold 不直接吸收，须转 canonical 审核再导出。Prompt/反馈/可解析已批准反例/召回 Train 与 held-out 的问题及 SQL 模板一并隔离；syntax_error 标签不能豁免可识别模板，未批准反例不训练。组合 FK 不拆成错误单列关联。training_manifest.outputs.collection_evidence v2 绑定实际集合文档、逐条元数据、向量与检索配置，供评测前后、发布及 runtime 复验。旧 v1 需新候选和新 Test，不原地补证据。详见 [Gold 手册](../../../docs/GOLD_SET.md)。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run text2sql-db-check
uv run text2sql-train-retriever
uv run text2sql-train
## 从 KNOWLEDGE_ARTIFACT_DIR/candidate_artifact.json 读取实际 version
$candidateVersion = 'VERSION_FROM_CANDIDATE_ARTIFACT'
uv run text2sql-evaluate --artifact $candidateVersion --split dev --enforce-gate
uv run text2sql-evaluate --artifact $candidateVersion --split test --enforce-gate
uv run text2sql-release-check --artifact $candidateVersion
uv run text2sql-release-check --artifact $candidateVersion --promote
~~~

evaluate 默认测 ACTIVE，--artifact 显式测注册候选；Test 默认写候选版本内 test_report.json。release-check --artifact 只检查；--promote 成功后写版本 approved_release.json，再原子切 ACTIVE，指针包含 release_manifest_path。当前真实 Gold 38 candidate / 0 approved，不能用测试夹具或旧 approved 标签启动正式训练。

## 扩展模板

扩展构建阶段补输入指纹、安全采样、候选失败保留、资源清理及 Dev 反例；不能借用 Test 调训练参数。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

缺失校准器/错误知识/Dev失败时保留上一 ACTIVE；候选报告若构建早期失败可能不存在，查日志。通过 Dev 也不会自动上线。已批准版本不能重测覆写；相同输入需新候选时临时设置 TRAINING_SKIP_UNCHANGED=false。锁老化不证明进程结束，不自动抢锁。历史版本清理不得删 ACTIVE 或当前登记候选；回滚仍需对应集合、审批和运行时身份复验，不能仅改指针。

[源码总览](../../README.md) · [专题说明](../../../docs/KNOWLEDGE_MAINTENANCE.md)
