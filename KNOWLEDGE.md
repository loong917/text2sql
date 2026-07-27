# 结构化知识库

`knowledge/` 是机器读取的唯一业务知识源。人工说明统一存放在 `docs/`，
不会进行全文切块训练。

## 文件职责

- `schema/schema_snapshot.json`：数据库不可用时的离线结构快照，由工具生成。
- `schema/table_cards.jsonl`：一表一卡，用于候选表语义召回。
- `domain/*.json`：指标、维度、关联和强制业务策略。
- `examples/gold_sql.jsonl`：仅 `status=approved` 的 SQL 可进入训练。
- `examples/migration_review.jsonl`：从旧 Markdown 提取的待审核样本。
- `examples/negative_sql.jsonl`：错误模式，不作为正向 Prompt 示例。
- `examples/refusal.jsonl`：明确的拒答边界。

## 迁移与更新

```powershell
text2sql-migrate-knowledge --question path/to/legacy-question.md
text2sql-export-schema
text2sql-train
```

迁移记录默认是 `candidate`。审核 SQL、执行结果和业务语义后，才能复制到
`gold_sql.jsonl` 并设置 `status` 为 `approved`。

训练器只读取 `STRUCTURED_KNOWLEDGE_DIR`。旧 Markdown 不参与运行时训练；
如需迁移历史示例，应显式运行 `text2sql-migrate-knowledge`。
