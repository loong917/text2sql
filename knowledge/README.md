# 结构化知识库

本目录是机器读取的唯一业务知识源。人工说明统一存放在 `docs/`，
不会进行全文切块训练。

## 文件职责

- `schema/schema_snapshot.json`：召回器离线训练的唯一 Schema 回退，工具生成并校验
  `schema_version` 指纹；运行时不会从历史 knowledge index 猜测结构。
- `schema/table_cards.jsonl`：一表一卡，用于候选表语义召回。
- `domain/*.json`：指标、维度、关联和强制业务策略。
- `examples/gold_sql.jsonl`：仅 `status=approved` 的 SQL 可进入训练。
- `examples/migration_review.jsonl`：历史离线审核归档，不会被训练器加载。
- `examples/negative_sql.jsonl`：错误模式，不作为正向 Prompt 示例。
- `examples/refusal.jsonl`：明确的拒答边界。

## 更新流程

```powershell
text2sql-export-schema
text2sql-train-retriever
text2sql-train
```

训练器只读取 `STRUCTURED_KNOWLEDGE_DIR` 中已定义的结构化文件。旧 Markdown 和
`migration_review.jsonl` 均不参与运行时训练。历史候选必须重新完成当前版本的
Schema、AST、执行结果和业务审核，才能以新记录进入 `gold_sql.jsonl`。

详细审核、版本和发布规则见
[`docs/KNOWLEDGE_MAINTENANCE.md`](../docs/KNOWLEDGE_MAINTENANCE.md)。
