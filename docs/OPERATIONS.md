# 部署与运维

本文档说明当前版本的生产配置、启动、训练、评测和故障排查。历史优化过程不在
正式运维文档中维护。

## 运行依赖

- Python 3.12+
- Ollama，以及配置的生成模型和 Embedding 模型
- SQL Server ODBC Driver
- 可读取数据库元数据的只读 SQL Server 账号
- 持久化目录 `vanna_knowledge_db/` 和 `vanna_agent_memory/`

## 安全基线

- 数据库账号只授予 `SELECT` 和必要的元数据读取权限。
- 禁止使用具有写入、删除、建表或修改结构权限的账号。
- 对外服务应配置 `API_KEY`，客户端通过 `X-API-Key` 传递。
- `MAX_RESULT_ROWS` 必须设置合理上限，避免大结果集耗尽内存。
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
MSSQL_CONN_STR=DRIVER={ODBC Driver 18 for SQL Server};...
SCHEMA_CACHE_TTL_SECONDS=300
MAX_RESULT_ROWS=500
```

部署环境中的 ODBC Driver 版本必须与连接串一致。Docker 镜像默认安装 Driver 18。

## 启动

```powershell
text2sql-server
```

健康检查：

```text
GET /health
```

健康检查失败时依次确认 Ollama、模型、ODBC Driver、数据库网络和连接权限。

## 知识更新

数据库结构或业务知识发生变化后：

```powershell
text2sql-export-schema
text2sql-train
text2sql-train-retriever
```

`text2sql-train` 会重建向量知识库，执行前应备份生产产物或在独立发布目录中构建。
召回校准器未通过误报率门禁时，不得通过手工降低阈值强制发布。

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
docker build -t text2sql:0.5.0 .
docker run --env-file .env -p 8090:8090 text2sql:0.5.0
```

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
- 短期降级可以使用离线 Schema 快照，但发布前仍应恢复实时校验。

### 大量拒答

- 检查语义 IR 是否识别指标、维度和实体。
- 检查 Table Card 是否覆盖业务别名。
- 检查召回校准器是否存在且通过质量门禁。
- 不要通过取消 Schema 或 AST 校验解决拒答问题。

### SQL 可执行但结果错误

- 检查指标口径、时间区间、实体过滤和关联关系。
- 将错误标记为 Negative，并填写错误类型。
- 修复结构化规则或 Gold 后重新训练和评测。
