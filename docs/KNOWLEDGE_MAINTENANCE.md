# 业务知识维护

知识源位于 knowledge/，Markdown 仅供说明。Schema 以 SQL Server 为准；机器记录经类型、表列引用和语义校验后进入候选产物。

历史 0.7.0 新增 [Schema/Gold 治理手册](GOLD_SET.md)：38 条原 approved/待审记录全部隔离为 canonical candidate，原字节完整归档；完整 Schema、内容摘要、双人审核、真实执行与模板隔离是正式准入前提。0.8.0 将候选登记与正式晋升拆开，检索指纹升级为 v2，旧证据需重建。当前没有可直接用于生产的 Gold，不能恢复旧标签求通过。

| 记录 | 作用 |
| --- | --- |
| schema/table_cards.jsonl | 审核表说明、业务别名与关键字段 |
| domain/metrics.json | 聚合口径、来源列、单位与输出别名 |
| domain/dimensions.json | 分组字段、时间字段与输出别名 |
| domain/joins.json | 业务关联与基数声明 |
| domain/policies.json | 明确业务策略与实体条件 |
| domain/entities.json | 业务名称到规范值的映射 |
| examples/gold_sql.jsonl | 人工批准并经过 AST/语义验证的正例 |
| examples/negative_sql.jsonl | 审核反例与错误类型 |
| examples/refusal.jsonl | 无法支持的问题边界 |

approved 不是免检标记：Gold 缺少可证明的指标定义或违反语义仍被拒绝。未知词不能自动变成数据库值；规则解析留下未映射语义会要求澄清。别名与业务词留在目录，不加入 Domain 硬编码。

Gold 的时间必须绝对化，不能把“今年/去年/本月”等会漂移的问句冻结为长期示例。生产只接受活动索引精确登记的记忆内容；Chroma 检索结果含表名不构成信任证据。生产不直接使用在线可变反馈作为 Prompt 示例，审核后需要重新构建、评测、发布。

普通反馈只进入候选区。管理员正向审核重新执行限行 SQL 并核对活动知识与实时 Schema；浏览器声明的行数/执行成功不作为信任证据。训练以同一份 Gold 快照计算摘要并构建内容。

0.6.2 起仅去除 SQL 外围空白/代码围栏，不压缩字面量和注释内部空白；签名不将不同字面量的 SQL 合并。此前已保存的 SQL 可能不可逆地改变，必须对照原始业务问题和基准结果重新审核历史 Gold，不能自动猜测恢复或继续沿用旧评测。

维护顺序：业务确认 → canonical 与知识双人审核 → 不可变 generation → 检索拟合/校准 → 知识候选与 Dev → 固定候选独立 Test → 发布检查 → 显式晋升。Schema、策略、集合或文件变化不能继续使用旧评测证据。

~~~powershell
uv run text2sql-train-retriever
uv run text2sql-train
## 从 candidate_artifact.json 读取实际 version，替换下面占位值
$candidateVersion = 'VERSION_FROM_CANDIDATE_ARTIFACT'
uv run text2sql-evaluate --artifact $candidateVersion --split dev --enforce-gate
uv run text2sql-evaluate --artifact $candidateVersion --split test --enforce-gate
uv run text2sql-release-check --artifact $candidateVersion
uv run text2sql-release-check --artifact $candidateVersion --promote
~~~

train 通过 Dev 仅 registry.stage：写 KNOWLEDGE_ARTIFACT_DIR 下的 candidate_artifact.json、VERSION/artifact.json，不切 ACTIVE。evaluate 默认测 ACTIVE，显式 --artifact VERSION 用于固定注册候选；Test 默认写该版本内 test_report.json。release-check --artifact 只检查；显式 --promote 且全部门禁通过才写 VERSION/approved_release.json，再原子切 ACTIVE，指针包含 release_manifest_path，审批记录包含 test_report_path。运行时只接受 ACTIVE 所指版本的审批与版本内 Test 报告，不以 global EVAL_TEST_REPORT_PATH 或独立 ready 文件批准其他版本。

training_manifest.outputs.collection_evidence 绑定实际 Chroma 集合内容与向量；评测前后、发布与运行时复核。同名、同数量的可变集合不能冒充冻结知识。生产快照重新核验业务审核，并与 generation 知识匹配。候选或门禁失败保留旧 ACTIVE；当前真实 Gold 仍为 38 candidate / 0 approved，必须补实际人工与执行证据，不为通过构造审批。

导出结构用 text2sql-export-schema：默认/--snapshot 来自完整知识快照，首次取证使用显式 --live 只读系统目录；不从残缺侧边索引猜结构。生产检索训练不自动回退历史快照。

原 DDL.MD、QUESTION.MD 不参与运行，无需恢复。可阅读 [数据模型](DATA_MODEL.md)、[评测规范](EVALUATION.md) 和 [知识目录说明](../knowledge/README.md)。
