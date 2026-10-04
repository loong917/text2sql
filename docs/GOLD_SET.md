# Schema、业务知识与 Gold Set 治理手册

## 当前事实与信任边界

历史 0.7.0 将 38 条记录迁为 `evaluation/gold_set/cases.jsonl` 中的 candidate：原 Gold 2、错误示例 1、拒答 1、历史 QUESTION 待审 12、五个评测集合 22。原文件字节在 `evaluation/gold_set/legacy/` 留档，摘要在 manifest.json；旧 payload 的 approved 只用于追溯，不是当前审批。0.8.0 沿用这份候选隔离，不补造审核，并将候选构建与显式发布分离。

已发现 `institution-whole-count-2024` Prompt 与 Dev 的同形 SQL 泄漏。更换年份、城市、问句或 template_id 不会消除 SQL 模板泄漏；物理表别名、CTE 名称及输出别名也按 Scope 规范化，真实 Schema、物理表与关联结构仍保留。保留其中一个用途，并为另一个集合设计真正独立的查询族，不能靠改别名隐藏泄漏。

旧 Schema 有 2 表/34 列，但类型和可空性不完整。已改名为 `knowledge/schema/legacy_schema_snapshot.json`，不再是默认运行快照。未猜测真实类型、主键、唯一键、单位或审核人。当前批准数据为 0，生产数据尚未就绪；空视图是显式隔离，不是删掉历史数据来掩盖问题。

## 一、取得权威结构

先处理 [数据库 TLS/账号前置条件](DB_TLS_RUNBOOK.md)，再在目标环境使用只读账号导出：

~~~powershell
uv run text2sql-db-check
uv run text2sql-export-schema --live --output knowledge/schema/schema_snapshot.json
~~~

`--live` 只读系统目录，不训练模型、不修改业务表；失败保留上一文件并关闭资源。默认导出仍来自完整 ACTIVE 快照，也可用 `--snapshot` 指定冻结产物。两个来源互斥。导出是本地文件写操作，请核对输出路径。

合同要求每表的 schema_name/qualified_name、非空字段类型、明确布尔可空性、primary_key、unique_keys、foreign_keys。空约束必须显式写 []/{}，不能把“未读取”当成“没有”。实时采集还保留长度、精度、scale、排序规则、identity/computed 及 FK 的 disabled/not_trusted/ordinal。字段与多列约束引用、指纹都必须一致。

仅未过滤、启用且非假设的唯一索引可证明全表唯一；禁用或不可信外键不进入可信路径。当前 Join IR 是单列关联，组合 FK 会完整显示在表卡，但不会被拆成单列训练/Prompt 关系。需要组合关联时先扩展计划、编译和校验，再加入 Gold。参考 [Microsoft sys.indexes](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-indexes-transact-sql) 与 [sys.foreign_keys](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-foreign-keys-transact-sql)。

## 二、数据库结构优化的实施顺序

| 检查 | 证据与落地方式 | 禁止行为 |
| --- | --- | --- |
| 事实粒度 | 明确一行究竟是采集事件、分项、修订还是有效事件；审核取消/作废/软删除状态 | 将 COUNT(*) 自动等同人数/人次 |
| 机构唯一性 | 验证 InstID 全量重复/NULL、历史版本及状态；确认自然键或代理键 | 未清洗重复就添加唯一约束，或用 DISTINCT 隐藏扇出 |
| 关联完整性 | 检查 BTSID 与机构键类型、缺失关联、FK 信任及 LEFT/INNER 的业务语义 | 只写 many_to_one 就认定不会放大/丢失结果 |
| 指标单位 | 确认 BCPVolume 的真实单位和跨采集类型可加性，必要时建设受控汇总视图 | 自动猜 ml/U 或直接跨类型求和 |
| 时间与状态 | 确认 BCDate 时区/精度、半开区间、业务有效状态及历史归属 | 擅自把 RUsingFlag/取消状态加入所有查询 |
| 查询性能 | 对真实 Gold 用实际计划、逻辑读、耗时和写入成本评估日期/类型/机构组合索引及 INCLUDE 列 | 不看数据分布和已有索引就部署“万能索引” |
| 数据服务边界 | 必要时由 DBA 发布脱敏、有效状态与标准单位视图，以只读白名单暴露 | 为提高准确率放开全部敏感列或写权限 |

本轮未对真实 SQL Server 执行 DDL。约束、索引或视图变更需由 DBA 在备份/预发布环境验证，登记迁移、回滚与业务复核；重新导出 Schema 后重建/重评所有绑定产物。

## 三、规范业务知识

metrics 只支持 COUNT/SUM/AVG/MIN/MAX，`*` 仅 COUNT；维度列与别名一一对应，日期维度是单列；policies 是 entity_filter/required_join/refusal 的严格合同，不接受任意 kind 或未使用字段。required_join 必须引用已证明的 Join；many_to_one 需要目标单列唯一键，one_to_one 两侧都需要唯一键且类型一致。

生产每张 Schema 表都要有已审表卡及事实 grain，指标单位不能是“数据库原始单位”；指标、维度、策略、实体和表卡都须由独立业务/数据人员复核，绑定当前 Schema 指纹。别名不能把“数量”等歧义表达硬归类成人次，不能为通过校验补造真实事实。

review 字段必须包含 source、business_reviewer、data_reviewer、reviewed_at（带时区）、schema_fingerprint、content_sha256、notes。content_sha256 使用 `knowledge.governance.content_digest`：对原记录去掉 review/execution/status 后进行规范 JSON 摘要。记录内容、问题、断言或口径变化即使仍写 approved 也会失效。摘要用于版本绑定，不证明签名真实性；审核身份和来源应通过组织的 PR/审批流程确认。

## 四、形成完善的 Gold Set

canonical Gold 的 JSON Schema 可复用为编辑器/审核平台合同：

~~~powershell
uv run text2sql-gold-set schema --output evaluation/gold_set/record.schema.json
uv run text2sql-gold-set check
uv run text2sql-gold-set check --enforce-release
~~~

最后一条在当前数据不足时必须非零退出。check 不访问数据库，也不修改状态。规范结构如下；占位字段不能用于正式批准：

~~~text
id / question / purpose / query_family_id / category / difficulty
source / source_line / source_sha256
status = candidate | approved | rejected
payload = 最终用途的完整记录（历史草稿可以暂不完整）
review = 独立业务与数据审核，内容摘要及当前 Schema 指纹
execution = 正例实际基准执行证据；负例不靠服务报错冒充拒答
~~~

用途由人工划分：prompt_gold、negative、refusal、retrieval_train、retrieval_calibration、retrieval_test、dev、test。migration_review 必须先分配最终用途才可批准。candidate 允许保留不完整旧稿；approved 要有严格当前合同。

正例 execution 要记录真实环境、database_snapshot_id、时区执行时间、baseline_sha256、schema_fingerprint、success=true、truncated=false、行数、结果列、结果摘要。语义预期由审核人独立确认，不从本次模型响应生成；Prompt Gold 使用完整 v3 semantic_ir，Dev/Test 使用完整 expected_semantic_ir。SQL 修改后旧执行证据失效；Schema 改变后旧审核失效。Test 正例必须显式 must_execute=true。

冻结评测使用经确认的数据库副本或受控一致性窗口，记录快照/恢复编号和权限。schema_fingerprint 只证明结构，不证明数据内容未变化；不要将数据库名称或一次 COUNT 当成快照证据。实际跑测仍会重新执行只读、限行基准并与模型结果比较。

最低规模：独立 Test 100（80 正例/20 拒答），检索 Calibration/Test 各 30；Train/Dev 按真实业务族扩充。覆盖单表/机构/城市聚合、实体/否定、多指标、年度/月/季度边界、排序/Top-N/HAVING、NULL/空结果、敏感列、未知指标/实体、重复机构/孤儿关联。暂未支持的去重人头、同比/环比、窗口、多事实和复杂派生指标应作为拒答/澄清验收，不伪装成正例。

按查询族隔离再切分，同族年份/实体/同义问句只能留在同一用途组；Prompt 与检索 Train 是同一训练侧。held-out 不参与拟合、阈值选择或 Prompt。发现 Test 问题后进入下一版本开发，不能改本轮预期让报告通过。

0.8.1 起，已批准的 negative 示例也属于训练侧：完整可解析只读 SQL 计算 Scope 模板指纹，参与与 Calibration/Dev/Test 的隔离。更换问题、family 或错误类型不能让同形反例进入知识集合。只有真实解析失败、明确标注 syntax_error，且单一只读恢复树没有物理表或完整投影主体的反例才可豁免 SQL 模板指纹，问题与查询族仍参与隔离。完整查询追加损坏 WHERE/ORDER BY/GROUP BY/UNION 不获豁免；空 GROUP BY 等解析器宽松接受的不完整 AST 也拒绝。不自动修复错误 SQL 为另一个模板。可解析写入、多语句、危险 SQL 或无法可靠隔离的完整主体反例保留在候选追溯或独立安全测试中，不进入此学习视图。未批准反例不进入训练，错误 SQL 不被执行。

## 五、不可变导出、重训与验收

完成知识与 Gold 审核后导出到一个不存在的新目录；STRUCTURED_KNOWLEDGE_DIR 此时指向已审核的知识源：

~~~powershell
uv run text2sql-gold-set export --schema knowledge/schema/schema_snapshot.json --output data/gold/v1
uv run text2sql-gold-set verify --root data/gold/v1
~~~

导出包含知识基础文件、三个示例视图、五个评测视图、canonical 字节及 gold-manifest.json。清单最后写入；没有清单的中间目录不得使用。verify 不只比较摘要，还解析全部机器文件合同，并用与 export 相同的派生逻辑核对视图确实来自 approved、最终用途正确的 canonical 记录；空审批、非法 JSON、候选混入或用途错配即使重算摘要也被拒绝。失败不覆盖已有 generation，检查失败目录后选新的目录重试；切勿编辑冻结文件。

将 STRUCTURED_KNOWLEDGE_DIR 指向 `data/gold/v1/knowledge`，五个 *_SET_PATH 指向该 generation 下对应 evaluation 文件；生产训练和放行拒绝混用不同代文件。生产快照会重新审核其知识，并与同代 generation 知识逐项匹配。然后固定候选版本完成评测与显式晋升：

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

train 通过 Dev 只调用 registry.stage，写入 KNOWLEDGE_ARTIFACT_DIR 下的 candidate_artifact.json 和 VERSION/artifact.json，不切换 ACTIVE。evaluate 默认测 ACTIVE，--artifact VERSION 才显式测注册候选；候选 Test 默认写 VERSION/test_report.json，不覆盖活动版本证据。release-check --artifact 只检查；只有显式 --promote 且全部门禁通过，才写 VERSION/approved_release.json 并原子切 ACTIVE，指针包含 release_manifest_path，发布记录包含 test_report_path。runtime 仅认可 ACTIVE 所指版本的审批及版本内 Test 报告，不使用 global EVAL_TEST_REPORT_PATH 作为生产在线权威来源。候选通过 Dev 不代表正式 Test 通过，数据规模达标也不代表准确率达标。

training_manifest.outputs.collection_evidence v2 绑定实际 Chroma 集合的文档、逐条元数据、向量内容、数量及维度，以及集合级检索配置和查询嵌入身份；评测前后、发布和运行时都核验该证据。同名集合、同数量或仅记录不变不能替代完整身份绑定。旧 v1 必须重建候选并重新评测，不能原地补摘要。失败保留旧 ACTIVE；不得手改状态、证据或指针求通过。

运行 SQLite 的历史 Gold 不会直接进入生产训练；需经 canonical 导入、独立审核、冻结导出后发布，原反馈库不被删除。rollback 必须恢复整套数据、知识、校准、集合、模型/代码身份与对应版本审批，并通过运行时复验。当前 38 candidate / 0 approved 不变，测试夹具不是实际业务审批或正式上线验收。

## 可复用验收记录

~~~text
Schema 真实导出时间/数据库/指纹/元数据权限：
事实粒度、键、关联完整性、单位、状态、时区审核：
Gold canonical 摘要/来源/双人审批/一致性快照：
场景和难度分布、独立查询族、泄漏检查、待审缺口：
不可变 generation 路径/摘要、知识/校准版本：
真实 Dev/Test 指标、截断/基准/拒答/完整执行证据：
目标环境压力、TLS/只读权限、依赖风险与批准：
回滚版本/验证人/剩余问题：
~~~
