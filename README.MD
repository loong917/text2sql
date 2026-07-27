# Text2SQL

基于本地 Ollama、SQL Server 和结构化业务知识构建的 Text2SQL 服务。系统通过
语义 IR、候选表召回、实时 Schema 和 SQL AST 校验降低幻觉。

## 主要能力

- 本地 Qwen SQL 生成与只读执行。
- 学习型候选表召回和外键图补全。
- 指标、维度、实体和时间语义中间层。
- SQLGlot AST、安全、Schema 和业务规则校验。
- pending、gold、negative、review 四层反馈闭环。
- JSON/JSONL 结构化知识和隔离的评测数据集。

## 安装与配置

要求 Python 3.12、Ollama、SQL Server ODBC Driver 和只读数据库账号。

```powershell
python -m pip install -e .
python -m pip install -e ".[dev]"
```

复制 `.env.example` 为 `.env`，配置 Ollama、数据库连接和知识目录。

## 常用命令

```powershell
text2sql-server
text2sql-train
text2sql-train-retriever
text2sql-evaluate --split test
text2sql-export-schema
text2sql-migrate-knowledge --question path/to/legacy-question.md
```

训练会重建向量知识库，运行前应确认现有知识库可以被覆盖。

## 项目结构

```text
src/api/                HTTP 入口
src/application/        在线用例编排
src/domain/             语义模型和 SQL 校验
src/infrastructure/     外部系统和持久化
src/knowledge/          结构化知识门禁
src/retrieval/          候选表召回
src/training/           离线训练和评测
src/evaluation/         评测数据模型与隔离校验
knowledge/              机器业务知识
evaluation/             retrieval_train、dev、test 数据
docs/                   人工说明
tests/                  单元、应用和架构测试
```

详细资料：

- [项目架构](./ARCHITECTURE.md)
- [知识维护](./docs/KNOWLEDGE_MAINTENANCE.md)
- [评测数据维护](./docs/EVALUATION.md)
- [候选表召回](./RETRIEVAL.md)

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 安全原则

- 数据库账号只授予 `SELECT` 和必要元数据读取权限。
- 执行成功不代表语义正确，不能自动晋升 Gold。
- Gold SQL 必须通过 AST、Schema、执行结果和人工审核。
- 未知指标、表、字段或实体应拒答，不应猜测。
