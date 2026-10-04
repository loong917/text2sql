# Text2SQL · 本地可信数据问答

基于 Ollama、Qwen2.5-Coder、SQL Server 和 Chroma 的本地 AI 应用，使用标准 src/text2sql Python 包布局。当前整改版本为 **0.8.1**，补齐反例 SQL 的 held-out 隔离，并升级集合证据为 v2，绑定检索配置及查询嵌入身份；旧集合/发布证据不得直接复用。

系统先建立目录约束的 QueryPlan，再检索冻结上下文、确定性编译或调用模型生成 SQL；只读 AST、Schema 和语义校验全部通过后才允许查询。“训练”是知识构建、检索拟合与概率校准，不是微调模型权重。

## 上线结论与四阶段状态

四阶段的代码机制已经落地，但不能把实现完成视为生产验收全部完成。目前仍不批准生产上线，详见 [生产放行清单](docs/PRODUCTION_READINESS.md)。

| 阶段 | 已实现的核心机制 | 仍需取得的上线证据 |
| --- | --- | --- |
| 1. 可信基线 | 冻结知识、审核反馈、独立鉴权、TLS/权限预检、文件与发布身份门禁、依赖扫描 | 生产只读账号、证书、密钥、HTTPS、完整发布产物、未处置漏洞审批 |
| 2. 语义正确性 | QueryPlan、未知条件澄清、有限编译、Scope/AST/语义反例校验 | 真实业务口径签字、边界场景及查询取消验收 |
| 3. 准确率提升 | 表卡嵌入、独立概率校准、统一检索策略、Prompt 预算、完整基准对比 | 足量、隔离且人工审核的数据及真实 Test 指标 |
| 4. 架构与维护 | Protocol、配置注入、职责拆分、资源所有权、安装包与 CI 契约 | 目标镜像、压测、备份恢复、回滚和灰度记录 |

历史 38 条样本已完整归档并转为待审核，当前正式批准数据为 0；旧 Test 6 条与检索校准/测试各 3 条不能继续冒充可信基准。运行视图已隔离，完善数据与部署步骤见 [Gold Set 治理手册](docs/GOLD_SET.md)。多租户、任意复杂 SQL 和多实例高可用不是当前已验证能力。

此前已修复可升级的 urllib3/OAuthlib 依赖漏洞；历史扫描中的 Chroma/Vanna 5 项去重公告尚未获得处置/豁免证据。本轮未重新扫描，仍按未处置风险阻断上线，目标平台须重新核验，详见 [依赖安全处置](docs/DEPENDENCY_SECURITY.md)。功能回归通过不等于零漏洞。

## Text to SQL 生成链路流程图

下面是在线查询控制流，不是所有请求都调用模型。源码映射、失败与重试规则见 [链路维护手册](docs/TEXT2SQL_FLOW.md)。

~~~mermaid
flowchart TD
    request["用户问题与执行模式"] --> authGate["API Key 或同源会话鉴权"]
    authGate --> releaseGate["ACTIVE 所指版本审批与集合内容/配置门禁"]
    releaseGate --> schemaCheck["完整实时 Schema 与冻结快照核对"]
    schemaCheck --> semanticPlan["目录约束的 QueryPlan"]
    semanticPlan --> planReady{"计划可回答？"}
    planReady -->|"否"| clarification["澄清或拒答，不生成 SQL"]
    planReady -->|"是"| retrieval["必需表与校准概率召回"]
    retrieval --> promptBuild["精确登记知识与冻结示例"]
    promptBuild --> contextReady{"上下文完整且预算允许？"}
    contextReady -->|"否"| clarification
    contextReady -->|"是"| compiler["尝试确定性编译"]
    compiler --> compiledPlan{"可编译？"}
    compiledPlan -->|"是"| astValidation["只读 AST、Scope、Schema 与语义校验"]
    compiledPlan -->|"否"| modelGenerate["Ollama 结构化生成"]
    modelGenerate --> astValidation
    astValidation --> sqlValid{"校验通过？"}
    sqlValid -->|"否"| retryPolicy["模型候选有限重试或校验失败"]
    retryPolicy -->|"允许模型重试"| modelGenerate
    sqlValid -->|"是"| executionMode{"执行查询？"}
    executionMode -->|"否"| sqlOnly["返回已校验 SQL"]
    executionMode -->|"是"| boundedQuery["限行、超时与并发受限执行"]
    boundedQuery --> queryResult["类型化结果与截断标识"]
    queryResult -.-> candidateFeedback["采集候选反馈，不自动晋升 Gold"]
    candidateFeedback -.-> reviewedRelease["双人审核、隔离导出、候选评测与显式晋升"]
~~~

生成/校验失败不会执行 SQL；数据库执行失败直接返回服务错误，不让模型反复试探数据库。已编译 SQL 校验失败不切换到模型逃避约束。max_retries 是额外重试次数，上限 3，因此最多 4 次模型候选。

## 能力与边界

- 单事实表聚合、审核维度与实体过滤、自然日期范围、时间分组、排序、Top-N 和有限 HAVING。
- 显式 schema.table；默认 dbo 可用短名。关联必须有结构与基数依据。
- 否定、比较符号、多个周期或未映射条件不能静默丢弃；无法确认时要求澄清。
- 未声明去重键、同比/环比、窗口、复杂派生指标、多事实聚合等尚不具备完整能力链，不能仅扩 Prompt 开放。
- 可执行、语义快照通过和离线测试通过都不能单独证明业务准确；词汇覆盖是保守门禁，不是开放式语言理解的完整证明。

## 开发环境启动

需要 Python 3.12/3.13、uv、Ollama、ODBC Driver 18 及 SQL Server。所有命令从仓库根目录执行。

~~~powershell
uv sync --frozen --extra dev
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
~~~

.env.example 是无秘密模板；.env 是本机私有配置，不覆盖已有文件、不提交 Git。安装 wheel 后显式设置 APP_DATA_DIR；相对数据路径以该目录为根，不以 site-packages 为根。

配置数据库、模型与业务目录后，按依赖顺序运行：

~~~powershell
uv run text2sql-db-check
uv run text2sql-export-schema --live --output knowledge/schema/schema_snapshot.json
uv run text2sql-gold-set check
~~~

先取得真实结构，审核业务知识与 Gold，再导出不可变 generation 并配置同代知识/五个评测路径。当前候选未获批，不能跳过此步骤直接训练；不得恢复旧 approved 标签。完成后运行：

~~~powershell
uv run text2sql-train-retriever
uv run text2sql-train
## 从 candidate_artifact.json 读取实际 version，替换下面占位值
$candidateVersion = 'VERSION_FROM_CANDIDATE_ARTIFACT'
uv run text2sql-evaluate --artifact $candidateVersion --split dev --enforce-gate
uv run text2sql-evaluate --artifact $candidateVersion --split test --enforce-gate
uv run text2sql-release-check --artifact $candidateVersion
uv run text2sql-release-check --artifact $candidateVersion --promote
uv run text2sql-server
~~~

先训练检索校准器，再构建知识候选；校准器缺失或过期时拒绝候选登记。训练通过 Dev 仅写 KNOWLEDGE_ARTIFACT_DIR 下的 candidate_artifact.json 与 VERSION/artifact.json，不切换 ACTIVE。首次启动也必须完成候选 Test 与显式晋升；没有获批 ACTIVE 时查询拒绝就绪。默认页面 http://localhost:8090。页面可打开、/livez=200 仅表示进程可响应，不代表查询就绪；检查 /readyz 及失败报告。

## 接口、会话与反馈

| 接口 | 用途与约束 |
| --- | --- |
| POST /ask | question、execute_sql、max_retries；返回 SQL、结果与明确 outcome |
| POST /generate-sql | 仅生成/校验，成功返回 SQL 文本，不查业务数据 |
| POST /feedback | 写入候选反馈，客户端声称正确不能直接晋升 |
| POST /admin/feedback-validation | 独立管理员凭据；服务端核验计划、Schema 与执行证据 |
| /auth/session | HttpOnly 短期会话；Cookie 写操作需同源证明，CLI 用显式 Key |
| /livez、/readyz | 存活与就绪，不得互相替代 |

outcome 分为 success、refused、clarification_required、infrastructure_error、validation_failed、generation_failed。模型/数据库不可用不能冒充语义拒答。

最多读取上限加一行，用于判断截断。result_total_rows 是已观察行数，不是完整 COUNT；截断结果不作为完整评测证据。反馈采集失败不改写已完成的查询结果。

生产只消费冻结产物的精确登记知识与示例，不读取在线可变 Gold/负例。相对日期问题不能晋升长期 Gold；需改写为绝对日期，并经过重建、评测及发布后生效。

## 目录与维护入口

| 资产 | 职责与维护说明 |
| --- | --- |
| [Python 源码](src/README.md) | 分层职责、依赖方向、扩展模板和模块 README |
| [结构化知识](knowledge/README.md) | 业务事实、表卡、实体、关联和审核示例 |
| [评测数据](evaluation/README.md) | Train/Calibration/Dev/Test 隔离、审核与用例准入 |
| [测试资产](tests/README.md) | 单元、契约、集成、HTTP、分发及回归模板 |
| [CI](.github/README.md) | 双 Python 版本、锁文件、wheel、容器门禁 |
| [文档导航](docs/README.md) | 按开发、审核和运维角色检索资产 |

旧 src.* 导入、根 scripts 包与兼容别名已移除，不恢复工作目录导入补丁。DDL.MD、QUESTION.MD 不再是运行输入；Markdown 不能替代结构化权威事实。

## 生产发布与验收

先配置 APP_ENV=production、生产只读连接、独立强密钥、Secure Cookie、访问策略、模型 digest 和 APP_REVISION，再运行同一身份的训练与评测：

~~~powershell
uv run text2sql-db-check
uv run text2sql-train-retriever
uv run text2sql-train
$candidateVersion = 'VERSION_FROM_CANDIDATE_ARTIFACT'
uv run text2sql-evaluate --artifact $candidateVersion --split dev --enforce-gate
uv run text2sql-evaluate --artifact $candidateVersion --split test --enforce-gate
uv run text2sql-release-check --artifact $candidateVersion
uv run text2sql-release-check --artifact $candidateVersion --promote
~~~

生产最低规模为 Test 100 条（正例至少 80、拒答至少 20），检索校准/测试各至少 30 条。总体/正例至少 85%、拒答至少 95%、语义 100%、执行覆盖和完整基准一致率至少 85%、检索召回至少 90%；执行分母是全部正例，并禁止拒答回归。

阈值是门禁，不是已获得的准确率。不得降阈值、复制模板凑数或伪造批准记录。evaluate 默认评测 ACTIVE；--artifact 显式固定注册版本，Test 报告默认写入该版本的 VERSION/test_report.json，不能覆盖或冒充活动版本证据。release-check --artifact 只检查，不晋升；仅显式 --promote 且全部门禁通过才写 VERSION/approved_release.json，再原子切换 ACTIVE，指针包含 release_manifest_path。发布记录包含 test_report_path；运行时只接受 ACTIVE 所指版本的审批和版本内 Test 报告，不接受独立复制的全局 ready 文件，EVAL_TEST_REPORT_PATH 也不再是生产在线的权威来源。

training_manifest.outputs.collection_evidence v2 绑定实际 Chroma 集合的内容、向量、集合级配置及查询嵌入身份，评测前后、晋升及运行时均需核验；同名集合、相同行数或仅记录摘要相同不等于同一检索行为。可解析反例 SQL 与 Prompt/检索 Train 一起参加模板隔离，不能靠更换问题、family 或错误类型绕过 held-out。生产快照重新执行审核校验，并与同代 generation 知识逐项匹配。代码、模型、访问策略、文件或集合变化后旧证据失效；活动版本切换后重启服务。

已批准版本的 Test/审批不能原地覆盖；同输入需新候选时临时设置 TRAINING_SKIP_UNCHANGED=false。评测与晋升使用共享独占锁，不自动抢过期锁。check-only失败不删旧审批；Dev报告和发布诊断/归档不能覆盖受保护输入、产物或Chroma文件。集合全量核验的成本、真实服务SLO和模型逐次请求不可变性仍须目标环境验证，不能以本地通过替代。

正式上线还需真实镜像、HTTPS 网关限速/请求体限制、目标硬件压测、慢查询取消、备份恢复及整套回滚记录。当前优先受控单租户/单实例部署；共享 Key 不提供个人身份或租户隔离。

证据绑定所有适用运行依赖和 Python 实现/版本/平台；源环境与部署环境不一致时必须在目标环境重新取得报告。旧版本压缩过的 SQL/Gold 不自动恢复，需重新审核。

## 可复用质量检查

~~~powershell
uv run ruff check src tests setup.py
uv run ruff format --check src tests setup.py
uv run mypy src
node --check src/text2sql/static/index.js
uv run pytest -q
uv lock --check
uv build --wheel --out-dir dist/0.8.1
$env:TEXT2SQL_WHEEL_DIR = (Resolve-Path dist/0.8.1).Path
uv run pytest -q tests/test_distribution.py
~~~

分发目录只放本次版本的一个 wheel。测试在仓库外安装包，验证资源、代码/锁文件身份、页面以及缺少产物时 readyz=503。正式数据验收不能用测试夹具通过数代替。

## 关键文档

- [架构与端口](ARCHITECTURE.md)
- [Text to SQL 链路维护](docs/TEXT2SQL_FLOW.md)
- [部署运行](docs/OPERATIONS.md) / [数据库 TLS](docs/DB_TLS_RUNBOOK.md)
- [知识治理](docs/KNOWLEDGE_MAINTENANCE.md) / [检索与 Prompt](docs/RETRIEVAL.md)
- [评测与证据](docs/EVALUATION.md) / [四阶段整改](docs/UPGRADE.md)
- [依赖安全扫描与处置](docs/DEPENDENCY_SECURITY.md)
- [上线阻塞项与放行清单](docs/PRODUCTION_READINESS.md)
