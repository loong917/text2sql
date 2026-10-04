# 准确性评测与冻结证据

## 数据来源与隔离

0.8.0 将候选评测与生产晋升分离。canonical Gold 是人工审核源，五个运行 JSONL 是不可变 generation 的派生视图，不是模型或用户反馈自动产生的真值。历史 38 条记录已隔离为 candidate，当前批准数据仍为 0；合成测试夹具不能替代真实审批。流程见 [Gold 手册](GOLD_SET.md)。

retrieval_train 用于拟合，retrieval_calibration 用于阈值选择，retrieval_test 只验收最终选择策略；dev 验证候选知识，test 是独立冻结验收。相同问题、template_id 或去除字面量后的 SQL 模板跨集合复用会被拒绝；Prompt 和已批准的完整可解析反例也不能泄漏 held-out 模板。syntax_error 标签不能绕过可识别主体的隔离；只有真实解析失败且没有表/完整投影主体的单查询反例才豁免 SQL 模板，问题与查询族仍隔离。空 GROUP BY 等不完整 AST 也拒绝，不自动修复或执行。

SQL 模板按查询 Scope 绑定关系与列来源，规范化表别名、CTE/派生关系及输出标签，但保留物理表/Schema 身份与查询结构。更换日期、值、别名或展示标签不能伪装独立难例；这不是任意 SQL 等价证明。generation 检查从 canonical 重算派生记录并逐项比较，不能通过重写文件摘要将人工修改的派生视图冒充审核结果。

每条真实用例由独立业务/数据审核人确认问题、Schema、口径、基准 SQL、语义预期及结果。审核摘要绑定 authored content，执行摘要绑定 SQL 与 Schema。数据库快照标识是证据声明，操作者仍须保证目标库处于相同受控冻结窗口；Schema 相同不证明业务数据未变化。

## 判定规则

查询 outcome 为 success、refused、clarification_required、infrastructure_error、validation_failed、generation_failed。基础设施不可用不算正确拒答；负例需要明确拒答/澄清、原因且没有 SQL 执行。

AST 断言检查表、列、过滤、连接、分组等结构。语义快照 version=3 是完整、严格类型的人工预期，包含阈值、排序指标、否定、计划状态和相对时间参考日；绝对日期不绑定评测当天。长期 Gold 只允许明确绝对日期。

actual 语义来自本次服务返回的 QueryPlan，缺失或畸形时不独立重解析补造。实际计划还要与冻结目录核对聚合、来源表/列、维度、实体值、日期列、关联及必需表，防止同一指标 ID 的错误物理绑定。目录本身的正确性仍依赖人工审核。

模型查询与独立基准都执行权限/Schema/AST 门禁及上限加一的限行。基准准入不依赖模型计划；TOP PERCENT/WITH TIES 不会被改写成另一种真值。截断不能作为完整结果一致证据。

结果比较保留类型、NULL、重复行及 Decimal。无序结果采用 bag，有序结果逐行比较，数值容差由用例显式声明。SQL 最终输出列与实际 DataFrame/行对象均必须唯一，不区分大小写；转换前阻断重复列，避免 to_dict 丢失数据。空结果保留可用列元数据；排序并列值及 TopN 边界需要明确业务处置，不能靠偶然返回顺序解释。

冻结 Test 正例必须显式 must_execute=true，并具备执行、基准执行、完整性、列、行数和结果一致性检查。执行覆盖率、完整基准一致率的分母都是全部正例；未执行、失败和截断不能从分母消失。

## 候选评测与报告归属

训练通过 Dev 后只登记候选版本，不修改 ACTIVE。从训练输出取得实际版本 ID 后运行：

~~~powershell
$candidateVersion = "替换为训练输出中的版本ID"
uv run text2sql-evaluate --artifact $candidateVersion --split dev --enforce-gate
uv run text2sql-evaluate --artifact $candidateVersion --split test --enforce-gate
uv run text2sql-release-check --artifact $candidateVersion
~~~

Test 报告只写所选版本目录的 test_report.json。--output 若不是该版本的这个文件即拒绝；Dev 可输出到独立审计目录，但不能覆盖受保护输入、registry 或 ACTIVE。Test 不使用任意公共报告路径；release-check 选择版本后自动读取该版本 Test 报告。

获批版本应作为不可变资产保留，重新评测/改口径须建立新候选版本，不覆盖正在服务版本的报告或审批。--enforce-gate 失败返回非零；未达标的候选报告不是批准，也不会自动发布。真正切换需显式执行 release-check --artifact ... --promote，见 [运维手册](OPERATIONS.md)。

评测与训练、晋升共用独占 training lease，避免受管任务一边评测一边切换/清理版本；锁疑似过期也不自动抢占。check-only 失败保留旧审批与活动指针。正式晋升在所属版本写 approved_release.json，再原子切换 ACTIVE；运行时只接受指针选中的本版本审批和本版本 Test 报告，不接受公共 latest 文件代替。

报告 schema_version=2，包含每条 Case ID、checks、outcome、summary、quality_gate 和 attestation。检查器拒绝缺失/重复/未知 Case ID、弱类型和自相矛盾 verdict，并从 results 重算统计与当前门槛。

## 输入与身份绑定

数据从同一次读取的字节解析、计算摘要。知识索引、校准器、训练报告、知识快照、训练 manifest 冻结为临时副本供评测装配使用，避免反复读取活动路径产生混合输入。

实际 Chroma 集合的 ID、document、逐条 metadata、embedding 稳定排序后计算 SHA-256，记录数量与向量维数；v2 将完整集合检索配置及集合级 metadata 纳入整体摘要，并单列 configuration_sha256。实际文本必须与索引登记一致，存储 embedding 配置必须匹配受控查询模型/地址/超时。评测前与发布证据写入前校验集合身份，配置变化即使记录不变也拒绝沿用证据。相同文本的不同 ID 也改变指纹；未登记文本不能进入受信上下文。旧 v1 必须重建候选、重评测，不原地补字段。

运行前后及证据写入前核对模型 digest、代码/Prompt/静态资源、全部适用依赖/锁文件、Python 实现/版本/平台、数据库公开身份及策略；晋升前再次核对实际模型。Windows 与 Linux、不同 Python 小版本的证据不能互相替代。

这些摘要是内容一致性检查，不是数字签名，也不能证明敌意并发 ABA 从未发生。生产必须隔离集合写者、保护 registry/审批文件权限，并固定模型 immutable tag；API 不会在每次生成调用中证明实际模型 digest。外部数据库、Ollama 和集合的冻结窗口由目标环境负责。

## 生产门禁与当前边界

Test 至少 100 条、正例 80 条、拒答 20 条；检索 calibration/test 各 30 条。总体/正例至少 85%、拒答 95%、语义 100%、执行 85%、检索召回 90%；安全/拒答回归额外阻止发布。不得通过复制模板、删失败用例、修改报告或降低下限补齐。

这些数字是门槛，不是本轮实测准确率。0.8.0 尚无足量人工批准、目标 SQL Server/Ollama 的完整 Test、TLS/权限及外部部署验收证据，仍为 blocked。依赖安全扫描也是独立放行条件，见 [生产就绪清单](PRODUCTION_READINESS.md)。
