# Text2SQL

面向企业数据问答的本地 Text2SQL 应用。项目使用 Ollama 运行 Qwen 生成模型和
Embedding 模型，以 SQL Server 实时 Schema、结构化业务知识和经过校准的候选表召回
为依据，生成并可选执行只读 T-SQL。

本项目的目标不是“让模型自由写 SQL”，而是让每次回答都经过可追溯的语义解析、
检索、生成、AST 校验、数据访问策略和评测门禁。

## 当前状态

- 代码质量、架构、鉴权、产物完整性和发布门禁已自动化验证。
- 当前本地生产检查仍为 `blocked`：SQL Server TLS 返回 `08001`，且冻结 Test 与
  Retrieval Calibration/Test 的样本量尚未达到生产门槛。
- 在 `text2sql-release-check` 生成 `status=ready` 的
  `production-release.json` 前，不应部署为生产服务。

## 核心链路

```text
用户问题
  -> 业务语义 IR（指标、维度、实体、时间、粒度）
  -> 校准后的候选表召回 + 外键图补全
  -> 实时 Schema、结构化规则、可信示例组成 Prompt
  -> Ollama / Qwen 生成 T-SQL
  -> SQLGlot AST + Schema + 语义 + 数据访问策略校验
  -> 仅返回 SQL，或使用只读账号执行并限制结果规模
  -> Pending / Negative 反馈
  -> 管理员服务端复核后晋升 Gold
  -> 候选知识版本训练、Dev 门禁、原子发布
```

## 主要能力

- 本地模型：默认 `qwen2.5-coder:14b`，数据和 Prompt 不必发送到外部模型服务。
- 语义中间层：显式表达指标、维度、实体过滤、时间范围、粒度、必需表和歧义。
- 学习型检索：Train、Calibration、Test 隔离；概率校准替代固定权重启发式打分。
- SQL 安全：只允许单条只读查询，并限制表、字段、Schema、JOIN、子查询、`SELECT *`
  和聚合专用表。
- 知识可信度：业务知识采用 JSON/JSONL 强类型模型；未知指标、字段或实体应拒答。
- 反馈治理：自动成功只能进入 Pending；Gold 必须由独立管理员权限在服务端重新校验。
- 版本化发布：知识索引、召回校准器、训练报告和清单作为一个不可变候选版本发布。
- 真实评测：报告绑定代码版本、模型摘要、Prompt、Schema、数据集、知识产物和依赖锁。
- 可运维性：`livez`、`readyz`、数据库预检、请求 ID、结构化阻断原因和原子文件写入。

## 技术栈

- Python 3.12 / 3.13
- FastAPI + Uvicorn
- Ollama + Qwen2.5-Coder + bge-m3
- SQL Server + Microsoft ODBC Driver 18
- Vanna + ChromaDB
- SQLGlot
- Pydantic
- uv、Ruff、mypy、pytest

## 架构

代码采用分层架构，业务用例依赖 Protocol，不直接依赖基础设施实现：

| 目录 | 作用 |
| --- | --- |
| `src/domain` | 语义 IR 与不依赖外部系统的 SQL/业务规则校验 |
| `src/application` | 查询、反馈、上下文构建等应用用例与 Protocol |
| `src/infrastructure` | Ollama、SQL Server、SQLite、Chroma 等适配器 |
| `src/bootstrap` | 配置注入、依赖装配和进程级资源生命周期 |
| `src/api` | FastAPI 路由、鉴权、错误契约、健康检查和训练报告接口 |
| `src/knowledge` | 结构化知识模型、Schema 指纹和版本化知识产物 |
| `src/retrieval` | Table Card、Schema 图、概率校准和召回训练 |
| `src/training` | 离线知识构建、画像、报告和候选版本发布 |
| `src/evaluation` | Dev/Test 数据契约、真实执行评测、汇总和质量门禁 |
| `src/release` | 生产配置、数据库、模型、产物和冻结报告的最终发布判定 |
| `knowledge` | 机器读取的 Schema、指标、维度、实体、关联、策略与示例 |
| `evaluation` | Retrieval Train/Calibration/Test 与 Text2SQL Dev/Test 数据 |
| `docs` | 人工维护和运维文档，不作为模型训练输入 |
| `tests` | 单元、契约、集成、端到端、架构与文档测试 |

更完整的依赖方向和模块说明见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

## 环境要求

1. Python 3.12 或 3.13。
2. Ollama，并已拉取生成模型和 Embedding 模型。
3. Microsoft ODBC Driver 18 for SQL Server。
4. 可读取业务白名单表和必要元数据的 SQL Server 只读账号。

```powershell
ollama pull qwen2.5-coder:14b
ollama pull bge-m3
```

## 安装

推荐使用仓库内的 `uv.lock` 创建可重复开发环境：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install uv==0.12.5
.\.venv\Scripts\uv.exe sync --frozen --extra dev
```

`requirements.lock` 是 Docker 使用的带哈希运行时锁；`pyproject.toml` 是唯一的直接
依赖声明，不再维护重复的 `requirements.txt`。

## 配置

```powershell
Copy-Item .env.example .env
```

至少核对以下配置：

```dotenv
LLM_MODEL=qwen2.5-coder:14b
EMBEDDING_MODEL=bge-m3
LLM_HOST=http://localhost:11434

MSSQL_CONN_STR=Driver={ODBC Driver 18 for SQL Server};Server=DB_FQDN;Database=DB_NAME;UID=READ_ONLY_USER;PWD=SECRET;Encrypt=yes;TrustServerCertificate=no;

SQL_ALLOWED_SCHEMAS=dbo
SQL_ALLOWED_TABLES=YOUR_ALLOWED_TABLES
SQL_DENIED_COLUMNS=*.Password,*.Token,*.Secret,*.Phone,*.Mobile,*.IDCard
SQL_AGGREGATION_ONLY_TABLES=YOUR_FACT_TABLES
```

生产环境还必须配置：

- `APP_ENV=production` 和真实 `APP_REVISION`；
- 三个互不相同且至少 32 字符的 `API_KEY`、`FEEDBACK_ADMIN_API_KEY`、
  `WEB_SESSION_SECRET`；
- `WEB_SESSION_COOKIE_SECURE=true`；
- Ollama 中实际模型对应的 `LLM_MODEL_DIGEST` 和 `EMBEDDING_MODEL_DIGEST`；
- 明确的表白名单和敏感字段黑名单。

不要提交 `.env`，也不要把数据库密码或 API Key 写入镜像、源码和日志。

## 首次构建与运行

先验证数据库、训练召回器和知识候选版本：

```powershell
.\.venv\Scripts\text2sql-db-check.exe
.\.venv\Scripts\text2sql-train-retriever.exe
.\.venv\Scripts\text2sql-train.exe
.\.venv\Scripts\text2sql-server.exe
```

浏览器打开 `http://localhost:8090`。服务不会在缺少完整活动知识版本时回退到旧索引。

健康检查：

```text
GET /livez   # 仅表示进程存活
GET /readyz  # 验证活动产物、Chroma、校准器、Ollama 和数据库
```

## API 与问答模式

| 接口 | 作用 |
| --- | --- |
| `POST /ask` | 生成、校验并按 `execute_sql` 决定是否执行 SQL |
| `POST /generate-sql` | 仅返回经过校验的 SQL，不访问业务数据 |
| `POST /feedback` | 普通用户提交 Pending/Negative 反馈候选 |
| `POST /admin/feedback-validation` | 管理员在服务端复核并晋升 Gold/Negative |
| `POST /auth/session` | API Key 换取短期 HttpOnly 浏览器会话 |
| `GET /training-report` | 查看当前活动知识版本及训练摘要 |

API 客户端使用 `X-API-Key`；Gold 审核使用独立的 `X-Admin-API-Key`。每个响应包含
`X-Request-ID`，请求方也可以传入合法的 `X-Request-ID` 关联服务端日志。

“仅生成 SQL”仍会执行语义、AST、Schema 和安全校验，只是不连接数据库执行生成结果；
适合人工审核、调试和低信任场景，不代表 SQL 已通过真实数据结果验证。

## 知识与反馈

- `knowledge/schema`：离线 Schema 快照和 Table Card。
- `knowledge/domain`：指标、维度、实体、关联和策略。
- `knowledge/examples/gold_sql.jsonl`：人工批准的离线 Gold。
- `knowledge/examples/negative_sql.jsonl`：已知错误模式。
- `knowledge/examples/refusal.jsonl`：明确拒答边界。
- `knowledge/examples/migration_review.jsonl`：只用于历史追溯，不进入训练。
- `FEEDBACK_DB_PATH`：运行期 Pending、Gold、Negative 和审核证据的事务数据库。

结构或业务口径变化后执行：

```powershell
text2sql-export-schema
text2sql-train-retriever
text2sql-train
```

详细规则见 [知识维护指南](./docs/KNOWLEDGE_MAINTENANCE.md)。

## 评测策略

项目隔离两组数据生命周期：

| 数据集 | 用途 | 是否参与调参 |
| --- | --- | --- |
| `retrieval_train.jsonl` | 拟合候选表概率模型 | 是 |
| `retrieval_calibration.jsonl` | 选择阈值 | 是 |
| `retrieval_test.jsonl` | 冻结召回门禁 | 否 |
| `dev.jsonl` | 训练候选版本的开发门禁 | 是 |
| `test.jsonl` | 正式发布冻结验收 | 否 |

日常开发评测：

```powershell
text2sql-evaluate --split dev --enforce-gate
```

正式候选评测：

```powershell
text2sql-evaluate --split test --enforce-gate
```

执行成功只证明 SQL 可以运行，不证明指标口径、维度、过滤条件和聚合语义正确。

## 生产发布门禁

以下步骤必须使用计划部署的同一份代码、配置、模型和知识目录顺序执行：

```powershell
text2sql-db-check
text2sql-train-retriever
text2sql-train
text2sql-evaluate --split test --enforce-gate
text2sql-release-check
```

最终门禁会验证：

- Driver 18、真实 TLS 加密和只读数据库权限；
- 生产鉴权、Cookie、代码版本和 SQL 数据访问策略；
- 实际 Ollama 模型摘要；
- 活动知识版本、Chroma collection、召回校准器及产物 SHA-256；
- 冻结测试集规模、质量门禁和安全/拒答零失败；
- 评测报告与代码、Prompt、依赖锁、模型、Schema 和知识版本完全一致。

失败时只更新 `production-readiness.json` 并移除旧的当前发布标记；成功时才生成
`production-release.json`。

## 测试与质量检查

```powershell
.\.venv\Scripts\ruff.exe check src tests scripts
.\.venv\Scripts\ruff.exe format --check src tests scripts
.\.venv\Scripts\mypy.exe src
.\.venv\Scripts\pytest.exe -q -p no:cacheprovider
.\.venv\Scripts\uv.exe lock --check
```

CI 同时覆盖 Python 3.12 和 3.13，并构建 `server`、`trainer` 两个 Docker target。

## Docker

```powershell
docker build --target trainer -t text2sql-trainer:0.5.0 .
docker build --target server -t text2sql-server:0.5.0 .
docker run --env-file .env -p 8090:8090 text2sql-server:0.5.0
```

`server` 镜像不包含冻结评测集；训练和发布产物应通过受控任务生成，并通过持久卷或
不可变制品交付给服务。具体部署和故障处理见 [部署与运维](./docs/OPERATIONS.md)。

## 当前限制与后续重点

- 必须修复当前 SQL Server 证书/TLS 协商问题后重新运行真实 Dev/Test。
- 冻结评测和召回校准/测试数据仍需扩充到生产样本门槛，并覆盖长尾问法、拒答、越权、
  多表关联、时间边界和业务口径冲突。
- 本地单模型吞吐有限；上线前需要真实并发、超时、显存、进程恢复和容量压测。
- 后续可增加 OpenTelemetry、Prometheus 指标、分布式限流、密钥托管和知识审核后台，
  但这些能力不能替代当前 SQL 与评测门禁。

## 文档

- [架构说明](./ARCHITECTURE.md)
- [部署与运维](./docs/OPERATIONS.md)
- [数据库 TLS 整改](./docs/DB_TLS_RUNBOOK.md)
- [评测体系](./docs/EVALUATION.md)
- [候选表召回](./docs/RETRIEVAL.md)
- [知识维护](./docs/KNOWLEDGE_MAINTENANCE.md)
- [数据模型](./docs/DATA_MODEL.md)
- [用户指南](./docs/USER_GUIDE.md)
- [知识目录说明](./knowledge/README.md)
