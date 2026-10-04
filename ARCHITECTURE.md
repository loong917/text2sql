# 架构说明

0.8.0 使用标准 src/text2sql 布局。安装后导入 text2sql，不再使用 src.* 或工作目录 sys.path 补丁。各模块的扩展模板、验收与失败处理见 [包导航](src/text2sql/README.md)。

## 依赖边界

完整 v3 真值集中在 domain/semantic_contract.py，知识与评测共用定义，不保留复制模块；knowledge/schema_contract.py 管物理元数据，governance.py 管内容/Schema绑定的双人审核和实际执行。evaluation/gold_set.py 管生命周期、Prompt/Train/held-out隔离与不可变导出；CLI 仅作离线装配。见 [治理流程](docs/GOLD_SET.md)。

Domain 只表达业务无关的语义和 SQL 规则；业务指标、维度、实体及关联定义由结构化目录传入。Application 依赖 Protocol，不直接构造 Ollama、Chroma 或数据库客户端。Infrastructure 实现应用端口；Bootstrap 装配配置和资源；API 负责 HTTP 契约与鉴权。

离线 training、evaluation、release 是组合入口，可以装配适配器。递归 AST 架构测试检查普通导入、别名、相对导入与嵌套文件，不只检查单个文件中的字符串。

## 核心契约

| 契约 | 输入/输出与责任 |
| --- | --- |
| Retriever | 问题 → QueryContext，不返回随意拼接的 tuple |
| Generator | Prompt → SQL 字符串；Ollama 适配器先验证结构化输出 |
| Validator | SQL、Schema 与计划 → ValidationResult |
| SqlExecutor | 已校验且限行 SQL → 带列信息的结果；domain/result_contract.py 在行字典转换前统一核验列唯一性与行结构 |
| Repository | 保存执行证据，不自动晋升 Gold |
| SchemaRepository | 读取权威 SchemaSnapshot，隔离缓存 |
| ArtifactProvider | 提供冻结 ContextKnowledge |
| SemanticParser、SqlCompiler、TableSelector | 解析计划、编译有限能力、返回带诊断的选择结果 |

QueryPlan 明确 ready、clarification_required、unsupported；支持范围、语义槽与未映射条件不能由模型自行放宽。旧 QuestionSemanticIR 别名已删除。

## 在线链路

问题 → 实时 Schema 与活动快照一致性检查 → QueryPlan → 候选表/上下文 → 确定性编译或模型生成 → Scope/AST/语义门禁 → 限行执行 → 候选反馈。

完整流程图以 [根 README](README.MD#text-to-sql-生成链路流程图) 为单一来源；节点对应源码、重试/失败规则和离线审核流程见 [链路维护资产](docs/TEXT2SQL_FLOW.md)。Cookie 写请求先做同源校验；“仅生成 SQL”仍经过相同语义与 AST 门禁，不执行数据库查询，也不产生执行成功证据。

模型 JSON 输出错误是 generation_failed，传输不可用是 infrastructure_error；校验服务契约损坏不会继续执行 SQL。受支持计划编译出的 SQL 仍必须通过相同校验。

上下文包含完整必需列、约束与计划；可选记忆、Gold 和反例按预算加入。必需内容超出预算时要求缩小问题，不截断安全约束。UTF-8 字节上界是保守预算，不宣称精确 tokenizer 计数。

## 离线链路

training/pipeline.py 的 build_candidate 只负责候选版本编排。knowledge_builder.py 构建知识，records.py 负责记录与元数据，storage.py 负责原子文件/状态，fingerprint.py 绑定构建输入，profiling.py 负责白名单画像。通过 Dev 后由 registry.stage 登记 artifact.json 和候选指针，不调用 publish、不修改 ACTIVE。

knowledge/input_snapshot.py 将知识目录清单、知识内容、Gold/检索数据和源校准器固定为单次读取的字节；同一快照用于解析、摘要和构建。生产源知识与冻结快照都校验内容/Schema 绑定的人审，发布还比较冻结知识与已审核 generation。检索校准器绑定 Schema、业务表卡、数据集和实际 embedding 模型 digest，不仅比较模型名称。

evaluation --artifact VERSION 固定所选候选的集合、索引和校准器；Test 只写本版本 test_report.json，已批准版本拒绝重写。release-check 默认仅诊断，失败不删旧审批；显式 --artifact VERSION --promote 才申请晋升。训练、评测和晋升共用独占 training lease，不凭超时自动抢锁。

评测数据从同一字节快照解析并计算摘要；受控产物复制到临时只读输入路径，避免反复读取活动路径造成 ABA 混合证据。实际查询计划还与权威业务目录的物理表、列、聚合函数、实体值和关联逐项校验，不独立重解析来补造证据。

knowledge/collection_evidence.py 绑定实际 Chroma ID、document、metadata、embedding、数量及维数，并与非结构索引内容精确核对；训练 manifest 保存该证据，训练、评测、发布及运行时都检查。文件摘要不能代替实际集合内容校验，也不是防篡改数字签名。

## 资源与缓存

每个 ApplicationContainer 持有独立的 Schema 状态和适配器，没有全局 settings 或跨实例规则缓存。Schema 更新为完整替换，不混合旧表；表嵌入索引使用内容指纹及 single-flight 构建。

容器资源工厂在锁内延迟构建，关闭后不能重新创建；版本化知识集合显式注入，不后备读取 ACTIVE。冷知识索引单次加载放在线程边界，SQL 输出仅移除外围空白/代码围栏，不压缩字符串和注释中的空白。

SQL 使用受限线程池。调用方取消不会提前释放仍在工作的并发许可；真实 DBAPI 支持语句超时和 cursor.cancel。关闭时有界等待，数据库驱动仍可能无法立即结束，运维层必须设置进程终止预算。

训练、独立检索训练和评测入口均关闭自己持有的 SQL/HTTP 资源。API 使用公开 lifespan，不替换 Starlette 私有实现。

本地 HTTP 回环模型请求使用统一客户端策略，不继承外部环境代理；远程及 HTTPS 地址继续继承代理/证书环境。Chroma SDK 没有客户端注入入口时，仅替换并关闭本容器创建的 embedding 传输，公共配置与集合身份不变。

## 发布边界

发布身份包括 Python 源码与静态资源、Prompt、全部适用运行依赖/锁文件、Python 实现/版本/平台、模型 digest、数据库公开身份及查询策略，不包含秘密值。实际依赖偏离锁文件即拒绝生产配置。安装路径与数据路径不参与代码身份；跨 Python/平台部署必须在目标运行环境重新生成证据。

release/readiness.py 在目标配置下核对源数据、人审 generation、冻结产物、Test、实际集合、数据库和模型身份；诊断及归档路径不得覆盖输入、知识目录、注册产物或服务指针。promote_release 在独占 lease 内复验，先写所属版本 approved_release.json，再原子切换 ACTIVE；旧服务证据不随新候选检查而被覆盖。

生产运行门禁只信任 ACTIVE.release_manifest_path 指向的同版本 approved_release.json，以及其中绑定的同版本 test_report.json，不信任独立可变的公共归档。它验证完整文件摘要、严格审核的知识快照、实际集合、冻结报告、质量门槛和数据库证据；启动时再次做真实数据库预检。活动版本变更后需重启，不热切换为未经当前容器验证的资源。

前后复验并非跨数据库、Ollama、Chroma 的分布式事务，也不能证明外部 ABA 写入从未发生。生产需单写者/只读权限保护集合和注册目录、使用不可变模型 tag、约束业务数据冻结窗口；这些门禁不替代网络隔离、秘密管理、备份、审计或外部安全测试。

依赖安全扫描是独立 CI 门禁，已知漏洞不自动忽略；嵌入式 Chroma/现代 Vanna 适配器不等于上游所有模块都已修复。未修复依赖的可达性、补偿措施与正式审批见 [依赖安全资产](docs/DEPENDENCY_SECURITY.md)。

细节见 [运行指南](docs/OPERATIONS.md) 和 [升级验收](docs/UPGRADE.md)。
