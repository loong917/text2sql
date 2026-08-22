# 项目架构

项目采用渐进式分层架构。依赖方向由外向内，领域层不依赖数据库、Ollama、
FastAPI 或文件系统。

```text
api ───────────────┐
training ──────────┼─> bootstrap ─> application ─> domain
evaluation ────────┘      │
                          └─> infrastructure

knowledge  提供结构化知识模型与加载门禁
core       提供配置、日志和基础异常
```

# 项目结构

```text
src/
├── bootstrap/          进程级依赖组合与资源所有权
├── api/                HTTP 入口和生命周期
├── application/        在线用例编排
├── domain/             语义模型和 SQL 校验
├── infrastructure/     Ollama、SQL Server、Chroma 和文件持久化
├── knowledge/          结构化知识加载门禁
├── retrieval/          候选表召回
├── training/           离线知识构建
├── evaluation/         隔离评测、断言与质量门禁
├── release/            生产发布证明与最终阻断门禁
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
  `SqlSafetyPolicy` 同时限制 Schema、表、字段、直接 `SELECT *` 和聚合专用表。

禁止依赖 `application`、`infrastructure`、`api` 和 `training`。

## `src/application`

实现在线用例和业务流程编排。

- `context_service.py`：组合实时 Schema、候选表、字段和知识规则并构建 Prompt 上下文。
- `context_rendering.py`：选择有依据的字段/记忆并渲染受 token 预算约束的上下文块。
- `context_state.py`：保存每个容器独立的 Schema、索引、语义目录和 Retriever 缓存。
- `schema_repository.py`：读取并按 TTL 缓存实时 SQL Server 元数据。
- `feedback_service.py`：在正确反馈进入 Gold 前执行实时 Schema、AST 和语义晋升门禁。
- `sql_policy.py`：SQL AST 校验所需的最小独立配置，不依赖 Prompt/检索配置。
- `ports.py`：定义 Repository、Retriever、Generator、Validator 和 SqlExecutor Protocol。
- `query_config.py`：查询用例所需的最小不可变配置。
- `query_prompt.py`：生成与纠错 Prompt 策略。
- `query_response.py`：稳定响应契约与 JSON 安全序列化。
- `text2sql_service.py`：仅组织生成、重试、校验、只读执行和反馈采集。

`src/bootstrap/container.py` 是进程级资源容器；`src/bootstrap/wiring.py` 把应用端口与
基础设施适配器组装为在线用例。容器持有不可变 `Settings`、`RuntimeResources`、
SQLite 反馈仓储、查询服务和 `ContextRuntimeState`。Schema、知识索引、语义目录及
Retriever 缓存均为容器级状态，不再通过模块全局变量跨应用实例共享。配置只在
CLI/API 入口加载一次，应用用例与基础设施不读取全局 `settings`。

应用层可以依赖领域层、检索模块和应用端口，但不能依赖 API、训练或基础设施实现。
`Text2SQLDependencies` 集中声明执行器、召回器、校验器、生成器和反馈仓储端口，
测试或新实现可以注入替代适配器，不需要修改主流程。
上下文每个请求只构建一次；生成/校验可以重试，但 SQL 执行失败不会再次生成并重复
执行，反馈候选写入失败也不会改变已经成功的查询响应。

## `src/infrastructure`

封装外部系统和持久化细节。

- `runtime.py`：`RuntimeResources` 创建、复用并显式释放 Ollama Embedding、
  Chroma Memory 和 SQL Runner。
- `feedback_repository.py`：使用 SQLite 事务保存 pending、gold、negative 和审核记录。
- `feedback_migrations.py`：通过 `PRAGMA user_version` 管理反馈数据库结构迁移。
- `ollama_generator.py`：Generator Protocol 的 Ollama 实现。
- `query_adapters.py`：Retriever/Validator 函数适配器与非阻塞 SQL Runner 适配器。
- `database_preflight.py`：只读执行 Driver、TLS、连通性与危险数据库权限预检。

基础设施层不能反向导入应用层。资源清理由 API 或训练入口显式协调。

## `src/api`

- `server.py`：FastAPI 路由、中间件、生命周期和 Uvicorn 启动。
- `auth.py`：API Key、短期 HttpOnly 会话和管理员审核授权。
- `schemas.py`：稳定的 HTTP 请求与响应模型。
- `health.py`：活动制品、Chroma、校准器、数据库和 Ollama 就绪检查。
- `training_report.py`：只读取并汇总活动制品内的训练报告。
- `errors.py`：统一请求校验和未处理异常响应。
- `create_app()`：只构建应用，便于单元测试和 ASGI 部署。
- `run_server()`：初始化运行时资源并启动 Uvicorn。

API 层不实现 SQL 语义或持久化规则。

## `src/retrieval`

- `table_card.py`：一表一卡、带指纹 Schema 快照读取及显式旧索引迁移工具。
- `dataset.py`：从 Gold、反馈和评测集构建训练样本。
- `calibrator.py`：学习概率校准参数。
- `schema_graph.py`：通过外键图补齐连接桥接表。
- `table_retriever.py`：语义召回、校准判断和 token 预算控制。
- `train.py`：离线训练入口。

## `src/knowledge`

- `structured.py`：加载 JSON/JSONL，执行格式去重、Schema 引用检查、
  Gold 状态门禁和 SQL AST 校验，并将业务知识投影为领域语义目录。
- `provenance.py`：生成稳定 Schema 指纹，用于隔离 Schema 变更后的过期在线 Gold。
- `artifacts.py`：原子发布包含知识索引、召回校准器、Manifest 和报告的不可变版本。

实际知识文件位于 `knowledge/`，人工文档位于 `docs/`。

## `src/training`

- `pipeline.py`：只编排版本构建、质量门禁和原子发布。
- `profiling.py`：只对显式 allowlist 字段执行有界画像，默认不采样业务值。
- `reporting.py`：纯函数构建训练报告和版本 Manifest。

训练属于离线用例，不应被在线 API 请求路径导入。

评测用例编排与断言位于 `src/evaluation/service.py`，依赖由 `evaluation/wiring.py`
显式装配。汇总和质量门禁位于 `src/evaluation/reporting.py`。训练、独立评测 CLI 和报告展示
共享同一套指标口径，避免重复统计逻辑发生漂移。训练只读取 Dev；冻结 Test 由独立
评测命令执行。

`src/release/readiness.py` 汇总生产配置、数据库预检、模型摘要、活动知识版本和冻结
Test 证明。只有这些证据完全绑定且达到生产样本数量门禁时，才生成
`production-release.json`；失败仅生成带阻断原因的 readiness 报告，并移除可能误用的
旧版当前发布标记。训练清单还保存知识索引、召回校准器和训练报告的 SHA-256，发布
检查会逐文件复核，防止活动版本内容被静默替换。

## `src/core`

- `config.py`：集中配置和环境变量解析。
- `build_info.py`：生成代码、Prompt、模型和依赖发布身份。
- `production.py`：生产配置的 fail-closed 规则。
- `logging.py`：日志初始化。
- `exceptions.py`：基础异常。

## 版本边界

从 `0.5.0` 开始不再提供 `src.services.*`、`src.core.agent` 和 `src.train`
兼容路径。调用方必须使用本文列出的分层模块或 `pyproject.toml` 中的 CLI。

## 工程质量

- Python 支持版本为 3.12 和 3.13；CI 对两个版本分别验证。
- `pyproject.toml` 声明依赖与 CLI，`uv.lock` 和带哈希的 `requirements.lock` 固定解析结果。
- `dev` 可选依赖提供 pytest、pytest-asyncio、Ruff 和 mypy。
- 架构测试防止后续改动重新产生反向依赖。
- 契约测试验证基础设施适配器满足 Protocol；集成测试覆盖完整查询用例；E2E 测试覆盖
  FastAPI 请求到响应契约。

## 架构约束

[`tests/test_architecture.py`](tests/test_architecture.py) 自动检查：

- 领域层不能依赖外层。
- 基础设施层不能依赖应用层。
- 应用层不能依赖 API、Bootstrap、基础设施实现或训练层。
- 新分层模块不能重新引用兼容 `services` 或 `core.agent`。
- 核心在线用例不能读取全局 `settings`，配置只能由组合根注入。
- Domain 不允许重新出现内置业务目录；指标、维度、实体和策略来自 `knowledge/domain`。
- `training/pipeline.py` 不允许重新内嵌评测断言实现。

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
