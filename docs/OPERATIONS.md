# 部署与运维

本文档说明当前版本的生产配置、启动、训练、评测和故障排查。历史优化过程不在
正式运维文档中维护。

## 运行依赖

- Python 3.12 或 3.13
- Ollama，以及配置的生成模型和 Embedding 模型
- SQL Server ODBC Driver
- 可读取数据库元数据的只读 SQL Server 账号
- 持久化目录 `vanna_knowledge_db/` 和 `vanna_agent_memory/`

## 安全基线

- 数据库账号只授予 `SELECT` 和必要的元数据读取权限。
- 禁止使用具有写入、删除、建表或修改结构权限的账号。
- 设置 `APP_ENV=production` 后，`API_KEY`、`FEEDBACK_ADMIN_API_KEY`、
  `WEB_SESSION_SECRET`、模型摘要和代码版本均为强制项。
- API 客户端通过 `X-API-Key` 传递凭据；同源浏览器先调用 `/auth/session` 获取
  短期 HttpOnly Cookie，访问密钥不会进入 localStorage 或前端源码。
- Gold 审核必须单独配置 `FEEDBACK_ADMIN_API_KEY`，并通过
  `X-Admin-API-Key` 传递；查询密钥不能晋升训练样本。
- 生产环境应显式配置 `SQL_ALLOWED_TABLES`、`SQL_DENIED_COLUMNS` 和
  `SQL_AGGREGATION_ONLY_TABLES`，避免只依赖 Schema 范围授权。
- `MAX_RESULT_ROWS` 会在执行前改写为顶层 `TOP (N+1)`，同时用于返回截断判断。
- 配置查询超时、JOIN/子查询上限及允许访问的 Schema。
- 自动执行成功的 SQL 只能进入 Pending，不能自动晋升 Gold。
- 日志不得记录数据库密码、完整连接串或大规模查询结果。

## Ollama 配置

常用配置：

```dotenv
LLM_MODEL=qwen2.5-coder:14b
EMBEDDING_MODEL=bge-m3
LLM_HOST=http://localhost:11434
LLM_TIMEOUT_SECONDS=180
LLM_MAX_CONCURRENCY=2
LLM_NUM_CTX=8192
LLM_NUM_PREDICT=1024
LLM_KEEP_ALIVE=15m
```

本地模型吞吐有限。出现超时或显存不足时，优先降低并发和上下文长度，不要盲目
增加重试次数。

## 数据库配置

```dotenv
MSSQL_CONN_STR=DRIVER={ODBC Driver 18 for SQL Server};...;Encrypt=yes;TrustServerCertificate=no
SCHEMA_CACHE_TTL_SECONDS=300
MAX_RESULT_ROWS=500
SQL_QUERY_TIMEOUT_SECONDS=30
SQL_MAX_JOINS=8
SQL_MAX_SUBQUERIES=6
SQL_ALLOWED_SCHEMAS=dbo
SQL_ALLOWED_TABLES=Stat_Collection,Pub_OrgAddress
SQL_DENIED_TABLES=
SQL_DENIED_COLUMNS=*.Password,*.Token,*.Secret,*.Phone,*.Mobile,*.IDCard
SQL_AGGREGATION_ONLY_TABLES=Stat_Collection
SQL_ALLOW_SELECT_STAR=false
```

部署环境中的 ODBC Driver 版本必须与连接串一致。Docker 镜像默认安装 Driver 18。
生产环境必须使用可验证的 SQL Server 证书，不允许通过 `Encrypt=no` 或
`TrustServerCertificate=yes` 绕过 TLS。`text2sql-db-check` 会验证 Driver、连接和
数据库主体是否具有危险的写入或控制权限。
服务器证书和账号整改步骤见 [DB_TLS_RUNBOOK.md](./DB_TLS_RUNBOOK.md)。

## 生产发布门禁

```powershell
text2sql-db-check
text2sql-train-retriever
text2sql-train
text2sql-evaluate --split test --enforce-gate
text2sql-release-check
```

冻结 Test 报告绑定活动知识版本、Schema 指纹、测试集哈希、代码版本、Prompt 哈希、
模型摘要和依赖版本。任一输入发生变化后旧报告自动失效。召回校准器也会复制到
版本化知识目录，在线服务不再把可变全局校准器与已发布知识版本混用。

## 启动

```powershell
text2sql-server
```

健康检查：

```text
GET /livez
GET /readyz
```

`/livez` 只说明进程存活；`/readyz` 只有在活动知识产物完整、对应 Chroma collection
可读取、召回校准器与活动 Schema/数据集一致、Ollama 可访问且数据库查询成功时才返回
200。不存在 `active_artifact.json` 时服务不会回退旧版知识索引，必须先完成训练发布。
健康检查失败时根据响应中的 `checks` 和 `actions` 定位具体依赖。

## 知识更新

数据库结构或业务知识发生变化后：

```powershell
text2sql-export-schema
text2sql-train-retriever
text2sql-train
```

`text2sql-train` 会在独立 Chroma 集合和版本目录中构建候选产物，Dev 门禁通过后才
原子切换 `active_artifact.json`；失败时线上版本与 Agent 对话记忆保持不变。
训练使用跨进程租约防止并发发布；异常退出形成的陈旧锁会在配置时间后回收。发布后
仅保留 `KNOWLEDGE_ARTIFACT_RETENTION_COUNT` 个最新版本，并始终保留活动版本。
历史版本目录和对应 Chroma collection 通过同一回收流程清理，不得直接删除 Chroma
内部 UUID 目录。
召回校准器必须同时通过独立 Retrieval Test 的目标召回率和误报率门禁；不得通过
读取冻结测试集反复调整阈值来强制发布。

训练先完成带版本 Manifest 的强类型知识校验；发现无效表、字段、Join 或 Gold 时
直接失败。活动指针只有在索引、召回校准器、报告和 Manifest 四项完整时才有效，
并使用同目录临时文件原子替换；发布门禁还会复核三类产物内容摘要。

## 评测与发布

日常开发使用 Dev：

```powershell
text2sql-evaluate --split dev
```

发布候选版本使用冻结 Test：

```powershell
text2sql-evaluate --split test --output ./vanna_knowledge_db/test-report.json
```

发布前应确认：

- 单元测试和架构测试全部通过。
- 结构化知识没有 rejected records。
- Test 通过率满足发布标准。
- 候选表召回器校准产物通过质量门禁。
- Gold、Pending 和 Negative 数量变化符合预期。

## Docker

```powershell
docker build --target server -t text2sql-server:0.5.0 .
docker build --target trainer -t text2sql-trainer:0.5.0 .
docker run --env-file .env -p 8090:8090 text2sql-server:0.5.0
```

`server` 镜像只包含在线服务所需的结构化知识，不包含评测集和人工文档；`trainer`
镜像额外包含评测数据，用于离线构建与发布知识制品。

生产环境应为知识库、反馈文件和日志配置持久卷。镜像中不应写入真实数据库密码。

## 常见故障

### Ollama 连接失败

- 检查 `LLM_HOST`。
- 确认生成模型和 Embedding 模型已经拉取。
- 容器访问宿主机 Ollama 时不要默认使用容器内的 `localhost`。

### Schema 获取失败

- 检查 ODBC Driver 与连接串。
- 检查数据库网络和证书参数。
- 确认账号具有元数据读取权限。
- Windows 出现 `08001`、`SSL 提供程序` 或“客户端不支持加密”时，先核对实际安装的
  Driver 版本以及 SQL Server 强制加密/TLS 配置；不要在代码中跳过实时 Schema 门禁。
- 召回器训练可短期使用离线 Schema 快照；主知识发布和上线前仍必须恢复实时校验。

### 大量拒答

- 检查语义 IR 是否识别指标、维度和实体。
- 检查 Table Card 是否覆盖业务别名。
- 检查召回校准器是否存在且通过质量门禁。
- 不要通过取消 Schema 或 AST 校验解决拒答问题。

### SQL 可执行但结果错误

- 检查指标口径、时间区间、实体过滤和关联关系。
- 将错误标记为 Negative，并填写错误类型。
- 修复结构化规则或 Gold 后重新训练和评测。
