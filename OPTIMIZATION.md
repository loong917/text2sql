# Text2SQL 准确性升级说明

## 新运行链路

1. `semantic_ir.parse_question_semantics` 将问题解析为确定性的语义 IR。
2. `schema_service.retrieve_memories` 合并 Chroma 向量结果与知识索引词法命中并重排。
3. Prompt 同时包含语义 IR、实时 Schema、业务规则、Gold 示例和相似负样本。
4. Ollama 生成单条 T-SQL。
5. `ast_validator.validate_tsql_ast` 使用 `sqlglot` 的 T-SQL AST 校验安全、表字段和业务语义。
6. SQL 执行成功后只写入 Pending；人工确认正确后晋升 Gold，错误反馈进入 Negative。

## 反馈分层

- Gold：`FEEDBACK_EXAMPLES_PATH`。仅人工确认的样本参与 few-shot 和离线训练。
- Pending：`FEEDBACK_PENDING_PATH`。自动执行成功的候选答案，不能进入 Prompt。
- Negative：`FEEDBACK_NEGATIVE_PATH`。人工标错样本及错误类型，可作为相似错误提醒。
- Review：`FEEDBACK_REVIEW_PATH`。保留完整人工审核流水。

旧版 `feedback_examples.jsonl` 中没有 `feedback_tier=gold` 且未经人工确认的自动执行记录会被自动忽略，不需要立即删除。重新人工确认后会写入规范 Gold 记录。

错误反馈的 `comment` 可包含以下标签：

- `wrong_table`
- `missing_filter`
- `wrong_metric`
- `wrong_dimension`
- `wrong_join`
- `wrong_granularity`
- `wrong_entity_value`

## AST 校验内容

- 只允许单条 `SELECT`、CTE 或集合查询。
- 拒绝 DML、DDL、MERGE 和命令节点。
- 表和带来源限定的字段必须存在于实时 Schema。
- 每个事实查询分支必须落实血液类型和日期范围。
- 城市实体必须使用规范值，例如“杭州”映射为“杭州市”。
- 人次必须使用 `COUNT`，采集量必须使用 `SUM(BCPVolume)`。
- 按机构统计必须按 `InstID, OrgName` 分组；按城市统计必须按 `City` 分组。

## 配置

新增配置：

```dotenv
LLM_NUM_CTX=8192
LLM_NUM_PREDICT=1024
LLM_KEEP_ALIVE=15m
FEEDBACK_PENDING_PATH=./vanna_knowledge_db/feedback_pending.jsonl
FEEDBACK_NEGATIVE_PATH=./vanna_knowledge_db/feedback_negative.jsonl
```

新增依赖：`sqlglot>=27.0,<29.0`。

## 验证

```powershell
python -m unittest discover -s tests -v
```

运行完整数据库评测仍使用：

```powershell
python -m src.train
```

完整评测会连接 SQL Server 和 Ollama，并更新训练报告；单元测试不依赖这两个外部服务。
