# 项目架构

项目采用渐进式分层架构。依赖方向由外向内，领域层不依赖数据库、Ollama、
FastAPI 或文件系统。

```text
api ───────────────┐
training ──────────┼─> application ─> domain
retrieval ─────────┘         │
                             └─> infrastructure

knowledge  提供结构化知识模型与加载门禁
core       提供配置、日志和基础异常
```

# 项目结构

```text
src/
├── api/                HTTP 入口和生命周期
├── application/        在线用例编排
├── domain/             语义模型和 SQL 校验
├── infrastructure/     Ollama、SQL Server、Chroma 和文件持久化
├── knowledge/          结构化知识加载门禁
├── retrieval/          候选表召回
├── training/           离线训练和评测
└── core/               配置、日志和基础异常

knowledge/              机器读取的业务知识
evaluation/             召回训练、开发和冻结测试数据
docs/                   人工说明文档
scripts/                迁移和运维命令
tests/                  单元、应用和架构测试
```

评测数据的隔离与发布流程见 [EVALUATION.md](./docs/EVALUATION.md)，知识维护流程见
[KNOWLEDGE_MAINTENANCE.md](./docs/KNOWLEDGE_MAINTENANCE.md)，生产部署见
[OPERATIONS.md](./docs/OPERATIONS.md)。

## `src/domain`

纯领域逻辑，可在不启动数据库、Ollama 和 Web 服务的情况下测试。

- `semantic_ir.py`：将问题归一化为指标、维度、实体、时间和必需表。
- `sql_validation.py`：执行 T-SQL AST、安全、Schema 和业务语义校验。

禁止依赖 `application`、`infrastructure`、`api` 和 `training`。

## `src/application`

实现在线用例和业务流程编排。

- `context_service.py`：读取 Schema、召回候选表、选择字段、检索规则并构建 Prompt 上下文。
- `text2sql_service.py`：组织 LLM 生成、重试、校验、只读执行和反馈采集。

应用层可以依赖领域层、检索模块和基础设施接口，但不能依赖 API。
`Text2SQLDependencies` 集中声明 SQL Runner、上下文、校验器、LLM 和反馈端口，
测试或新实现可以注入替代适配器，不需要修改主流程。

## `src/infrastructure`

封装外部系统和持久化细节。

- `runtime.py`：创建和复用 Ollama Embedding、Chroma Memory 和 SQL Runner。
- `feedback_repository.py`：保存 pending、gold、negative、review 四层反馈。

基础设施层不能反向导入应用层。资源清理由 API 或训练入口显式协调。

## `src/api`

- `server.py`：FastAPI 请求模型、路由、中间件、生命周期和 Uvicorn 启动。
- `create_app()`：只构建应用，便于单元测试和 ASGI 部署。
- `run_server()`：初始化运行时资源并启动 Uvicorn。

API 层不实现 SQL 语义或持久化规则。

## `src/retrieval`

- `table_card.py`：一表一卡和 Schema 快照。
- `dataset.py`：从 Gold、反馈和评测集构建训练样本。
- `calibrator.py`：学习概率校准参数。
- `schema_graph.py`：通过外键图补齐连接桥接表。
- `table_retriever.py`：语义召回、校准判断和 token 预算控制。
- `train.py`：离线训练入口。

## `src/knowledge`

- `structured.py`：加载 JSON/JSONL，执行格式去重、Schema 引用检查、
  Gold 状态门禁和 SQL AST 校验。

实际知识文件位于 `knowledge/`，人工文档位于 `docs/`。

## `src/training`

- `pipeline.py`：离线知识训练总流程，包括 Schema 索引、画像、结构化知识、
  Gold 反馈、去重、报告和评测。

训练属于离线用例，不应被在线 API 请求路径导入。

## `src/core`

- `config.py`：集中配置和环境变量解析。
- `logging.py`：日志初始化。
- `exceptions.py`：基础异常。

## 版本边界

从 `0.5.0` 开始不再提供 `src.services.*`、`src.core.agent` 和 `src.train`
兼容路径。调用方必须使用本文列出的分层模块或 `pyproject.toml` 中的 CLI。

## 工程质量

- Python 最低版本为 3.12。
- `pyproject.toml` 是依赖、CLI、构建与开发工具的单一配置入口。
- `dev` 可选依赖提供 pytest、pytest-asyncio、Ruff 和 mypy。
- 架构测试防止后续改动重新产生反向依赖。

## 架构约束

[`tests/test_architecture.py`](tests/test_architecture.py) 自动检查：

- 领域层不能依赖外层。
- 基础设施层不能依赖应用层。
- 应用层不能依赖 API 或训练层。
- 新分层模块不能重新引用兼容 `services` 或 `core.agent`。

## 在线调用链

```text
FastAPI endpoint
  -> application.text2sql_service
  -> application.context_service
  -> domain.semantic_ir
  -> retrieval.table_retriever
  -> Ollama generation
  -> domain.sql_validation
  -> SQL Runner
  -> infrastructure.feedback_repository
```
