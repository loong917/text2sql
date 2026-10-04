# 运行、部署与验收

0.8.0 部署前必须完成 [Schema/Gold 治理](GOLD_SET.md) 并挂载已审不可变 generation，配置同代知识和五个评测路径。历史 38 条记录均为 candidate、approved 为 0，当前运行视图为空，不能跳过审核直接运行历史训练命令。数据库账号只读；DDL/索引由 DBA 单独审批。

## 本地准备

在项目根运行 uv sync --frozen --extra dev。首次复制 .env.example 为 .env，已有私有配置保留。使用 Python 3.12/3.13，安装 ODBC Driver 18，并确认目标 Ollama 模型与 SQL Server 可用。

APP_DATA_DIR 是数据路径根目录；wheel 安装后显式设置到持久目录。配置修改后重启进程，不依赖全局 settings 热更新。服务默认监听 0.0.0.0:8090，开发环境不要直接暴露公网。

本地 HTTP Ollama 的 localhost/回环 IP 不使用环境代理，生成、检索/知识嵌入、模型预检保持同一策略，防止本地问题/Schema 被误送代理。远程地址及 HTTPS 仍保留代理/CA 环境行为；HTTPS 回环部署应配置正确证书与 NO_PROXY，不禁用证书校验。

~~~powershell
uv run text2sql-db-check
uv run text2sql-train-retriever
uv run text2sql-train
~~~

数据库检查只读探测真实 TLS 及数据库、Schema、对象、服务器级有效权限，不通过试写证明只读。失败先按 [TLS 排障](DB_TLS_RUNBOOK.md) 处理。

训练成功也不会切换活动知识版本。train 只构建候选、通过 Dev 并登记版本；从训练输出取得真实版本 ID 后，用 text2sql-evaluate --artifact VERSION 评测该候选。查看版本目录内 training_report.json 和服务日志。表结构改变、业务定义修改或模型更新后重新训练、评测，不能原地覆盖已批准版本。

text2sql-server 使用 ACTIVE；首次没有合格活动版本时，不能把进程存活或聊天页面可见当作知识就绪。候选可在离线评测入口验证，生产服务需完成下述明确晋升流程。

## 生产发布顺序

先准备独立查询/审核/会话密钥、Secure Cookie、HTTPS、明确表白名单及敏感列策略。设置 APP_ENV=production、APP_REVISION、真实模型 digest 和只读连接串，然后使用同一配置完成：

~~~powershell
uv run text2sql-db-check
uv run text2sql-train-retriever
uv run text2sql-train
$candidateVersion = "替换为训练输出中的版本ID"
uv run text2sql-evaluate --artifact $candidateVersion --split test --enforce-gate
uv run text2sql-release-check --artifact $candidateVersion
# 完成外部验收和签字后，才执行正式晋升。
uv run text2sql-release-check --artifact $candidateVersion --promote
uv run text2sql-server
~~~

| 操作 | 写入与服务影响 |
| --- | --- |
| train | 写版本产物和候选登记；不修改 ACTIVE |
| evaluate --artifact VERSION --split test | 只写版本/test_report.json；已批准版本禁止重写 |
| release-check [--artifact VERSION] | 默认仅检查并写诊断；不创建审批、不切 ACTIVE，失败也不删除旧审批 |
| release-check --artifact VERSION --promote | 全部门禁及最终复验通过后写版本审批，并原子切 ACTIVE |

--promote 必须与 --artifact 一起使用。Test 的 --output 只能为所选版本 test_report.json；Dev 可以输出到独立诊断路径，但不能覆盖受保护输入或 registry。不要将候选 Test 写到共享最新报告，避免影响正在服务的旧版本。

生产要求至少 100/80/20 条 Test/正例/拒答，以及检索校准、测试各 30 条。源数据不足会阻止发布；不要用重复模板、修改报告或降低阈值绕过。

先完成 [依赖安全扫描](DEPENDENCY_SECURITY.md)，再评测与发布。当前版本尚有未获批的上游漏洞处置，不能只看 release-check 成功就认为全部上线门禁通过。

release-check 需要审核数据源、所选版本产物及真实依赖；线上服务器只需要已验证的活动产物、集合、版本专属 Test 报告和审批，不需要原始评测 JSONL。

版本目录至少保留 knowledge_index.json、knowledge_snapshot.json、table_retrieval_calibrator.json、training_report.json、training_manifest.json、artifact.json、test_report.json 和晋升后生成的 approved_release.json。ACTIVE.release_manifest_path 必须指向所属版本 approved_release.json；该审批的 test_report_path 必须是同版本 test_report.json。production_release_manifest_path 是发布归档/镜像，不是可独立覆盖的运行时批准源。

production_readiness_report_path 与 production_release_manifest_path 必须不同，且不能指向任何受保护输入、知识目录、注册产物或服务指针；仅归档允许等于当前所选版本的 approved_release.json。应用会拒绝危险配置，不应靠改路径到输入文件绕过。

晋升先持久化版本审批，再原子切换 ACTIVE。若最终复验/写入失败，旧 ACTIVE 和旧审批保留；未切换的版本不算发布成功。若失败候选已留下 approved_release.json，不手工覆盖审批或继续重写 Test，应保留故障证据并构建新候选。

活动版本切换后以新证据重启服务。回滚需恢复一套一致的活动指针、产物、集合、代码、模型与发布证据，不能只改版本字符串。

## 健康检查

/livez=200 只证明进程存活。/readyz 核对产物、集合、校准器、数据库、Ollama；生产还检查发布证据。未就绪返回 503，不能仅用页面可访问作为上线验收。

/ask 返回明确 outcome；基础设施错误不计为正确拒答。生产故障通过 request_id 对照日志，不把数据库异常细节直接发给浏览器。

## Docker

~~~powershell
docker build --target trainer -t text2sql-trainer:0.8.0 .
docker build --target server -t text2sql-server:0.8.0 .
docker run --rm --env-file .env -v text2sql-data:/app/vanna_knowledge_db --entrypoint text2sql-db-check text2sql-trainer:0.8.0
docker run --rm --env-file .env -v text2sql-data:/app/vanna_knowledge_db --entrypoint text2sql-train-retriever text2sql-trainer:0.8.0
docker run --rm --env-file .env -v text2sql-data:/app/vanna_knowledge_db text2sql-trainer:0.8.0
$candidateVersion = "替换为容器训练输出中的版本ID"
docker run --rm --env-file .env -v text2sql-data:/app/vanna_knowledge_db --entrypoint text2sql-evaluate text2sql-trainer:0.8.0 --artifact $candidateVersion --split test --enforce-gate
docker run --rm --env-file .env -v text2sql-data:/app/vanna_knowledge_db --entrypoint text2sql-release-check text2sql-trainer:0.8.0 --artifact $candidateVersion
# 仅在正式签字后晋升。
docker run --rm --env-file .env -v text2sql-data:/app/vanna_knowledge_db --entrypoint text2sql-release-check text2sql-trainer:0.8.0 --artifact $candidateVersion --promote
docker run --env-file .env -v text2sql-data:/app/vanna_knowledge_db -p 8090:8090 text2sql-server:0.8.0
~~~

示例仅说明训练与服务共享持久卷；生产还需要保存冻结报告/发布证据，或挂载它们所在的配置路径。Docker 中 APP_DATA_DIR=/app；宿主机 Ollama 地址需要改为容器能访问的地址，不能直接沿用 localhost。证书信任、HTTPS、备份和卷权限由目标环境配置。

本轮本机没有 Docker，镜像与启动检查由 CI 执行，尚不能视为目标环境验收成功。

Docker 从 requirements.lock 安装运行依赖，从 build-requirements.lock 校验并安装固定构建后端，再禁用构建隔离安装本项目。两类锁文件和 uv.lock 均进入发布身份。镜像基底、ODBC/系统包和最终镜像 digest 仍需由目标发布流程记录。

当前默认定位为受控单租户、单服务实例。生产通过 HTTPS 网关配置鉴权入口限速、请求体上限、全链路超时和并发预算；SQLite/本地 Chroma 不应未经验证直接扩成多实例共享写入。备份恢复、负载和优雅退出须按 [生产放行清单](PRODUCTION_READINESS.md) 实测。

训练、候选评测和晋升共用 registry 的 .training.lock。检测到超时锁只报错，不自动抢占；先核实锁 PID 所属进程已终止、没有相关训练/评测/晋升任务，并保留诊断，再按目标环境审批清理该明确锁文件。不删除整个 registry 或数据卷。

独立检索训练另有校准器发布 lease，使用唯一候选文件和诊断文件；只有固定输入、业务表卡、Schema、数据集及实际 embedding 模型身份复验通过后才发布。其失败不覆盖原校准器。不要同时用外部脚本改写源数据或模型。

实际集合的 ID、document、逐条 metadata、embedding、数量、维数及集合级检索配置进入 training manifest 的 v2 证据，训练/评测/发布/运行核对。运行查询须使用受控的 Ollama embedding 配置，不能由存储集合暗中替换模型/地址。旧 v1 产物不自动迁移，需新建候选、重新 Test 和显式晋升。配置摘要只发现可见变化，不提供跨系统事务隔离：生产需 Chroma 单写者、上线集合只读、registry/审批受控权限、不可变模型 tag/模型存储以及业务数据冻结窗口。即使前后 digest 相同，也不能声称每次模型请求都已证明无外部 ABA 变化。

Dev报告只允许独立JSON，不能写入Chroma、registry、知识目录或任何配置输入。readiness/发布归档同样禁止覆盖输入、源代码和Chroma数据文件；Chroma根目录仅保留约定的production-readiness.json与production-release.json两个诊断文件例外，版本审批由registry管理。自定义输出放在独立报告目录。当前每次运行门禁全量读取向量核验，成本随知识规模增长；目标p95/p99仍需实测，不能用简单TTL缓存绕开安全检查。

浏览器 Cookie 写请求校验真实同源 Origin 和 Fetch Metadata；查询/管理员 API Key 的显式请求头用于非浏览器调用。HTTPS 反向代理必须保留 Origin，且仅信任网关来源的 forwarded 信息，使应用看到正确协议/主机；不要全网信任转发头或通过禁用 CSRF 解决 403。操作流程依据 [FastAPI 反向代理说明](https://fastapi.tiangolo.com/advanced/behind-a-proxy/)。

发布身份包含全部适用运行依赖及 Python 实现/版本/平台。在 Windows 评测后移到 Linux、升级 Python 小版本或更改依赖，必须在目标运行环境重新评测与 release-check；单纯复制旧报告会被拒绝。

## 清理与故障恢复

旧 src 包、scripts 包和迁移 helper 不存在；切换为 text2sql.* 入口。缺少 knowledge_snapshot.json 的旧产物不自动迁移，需要重训；旧业务数据不会自动删除。

历史 artifact 清理由训练保留策略管理，活动版本与当前候选始终保留。日志、SQLite、Chroma、版本 Test/审批及发布身份均应成套备份；不要手工删除活动目录来“修复”就绪状态。生产回滚演练应恢复已经批准且内容一致的整套资产，再重启与核验就绪，不重新写旧版本审批。

历史版本可能压缩 SQL 字符串/注释中的空白。0.6.2 起不再如此处理，但已损坏的历史 SQL 无法自动恢复；审核历史 Gold 的原始 SQL 与执行结果，修正后重建、重评测、发布，不批量猜测替换。

本地 tutorial_env、旧 egg-info、构建缓存属于忽略的环境/生成物，不是新包使用的兼容代码；运行数据库、历史集合与日志不属于可随意删除的“无效目录”。本轮保留这些私有数据，清理应先确认归属与备份。
