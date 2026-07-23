# Text2SQL

基于 Vanna AI 的生产级 Text-to-SQL 服务（支持本地 Ollama + 执行反馈）。

## 特性

- 本地部署（Ollama + ChromaDB，默认推荐使用 qwen2.5-coder 系列）
- 支持 Execution Feedback Loop（自动修正错误 SQL）
- 增强 Schema 训练（整表结构聚合、枚举值离散抽取、业务文档解析）
- 支持 Microsoft SQL Server
- FastAPI 服务 + 健康检查

## 快速开始

### 1. 安装

```bash
pip install -e .
```

### 2. 训练知识库

#### training.py

- 负责离线构建索引和高价值语义记忆。
- 负责训练报告、评测、反馈样本回灌。
- 不再承担“把整份结构事实塞进向量库”的工作。

#### schema_service.py

- 负责实时 schema、候选表选择、拒答判断、prompt 结构约束、SQL 合法性校验。
- 现在是真正的“结构事实唯一可信来源”。

#### sql_service.py

- 负责模型调用、失败重试、执行结果回流。
- 不再承担结构知识组织，只消费 schema_service.py 已经整理好的上下文。

## 生产环境加固

运行时内置以下防线（相关阈值见 `.env.example` 的“生产加固”段）：

- **只读 SQL 闸门**：生成的 SQL 在校验和执行前都会经过 `enforce_readonly_sql`，仅允许单条 `SELECT`/`WITH` 查询，拦截 DML/DDL、`SELECT INTO`、多语句、存储过程调用。数据库账号仍应只授予 SELECT + VIEW DEFINITION 权限（纵深防御）。
- **LLM 并发闸门**：`LLM_MAX_CONCURRENCY` 限制同时打到 Ollama 的生成请求数，超出排队；共享单个异步客户端。
- **结果集截断**：`MAX_RESULT_ROWS` 限制单次返回行数，响应中通过 `result_truncated` / `result_total_rows` 标识。
- **Schema 缓存 TTL**：`SCHEMA_CACHE_TTL_SECONDS` 控制实时结构缓存过期时间，结构变更后无需重启即可生效。
- **反馈即时生效**：用户在线标记 correct 的问答对会按关键词相关度以 few-shot 形式注入后续 prompt（`PROMPT_FEEDBACK_EXAMPLES`），无需等待离线重训练。
- **接口校验与鉴权**：请求参数经 Pydantic 校验；配置 `API_KEY` 后业务接口需携带 `X-API-Key` 请求头。
- **日志脱敏**：不再打印数据库连接串与完整结果集，仅记录执行摘要。
- **容器安全**：镜像以非 root 用户运行，构建上下文排除虚拟环境与 `.git`。

注意：反馈文件写入使用进程内锁，服务应以单进程（默认 uvicorn 配置）部署；如需多副本，需将反馈存储迁移到数据库。