# 结构化知识维护指南

## 单一事实来源

- 数据库实时 Schema：表、字段、类型和真实外键。
- `knowledge/schema`：离线快照和候选表检索卡片。
- `knowledge/domain`：指标、维度、业务关联和强制策略。
- `knowledge/examples/gold_sql.jsonl`：经过人工确认的正确示例。
- `knowledge/examples/negative_sql.jsonl`：已知错误模式。
- `knowledge/examples/refusal.jsonl`：拒答边界。

不要把本文档或其他 Markdown 文件作为训练输入。

## Schema 变更

数据库结构变更后执行：

```powershell
text2sql-export-schema
text2sql-train
text2sql-train-retriever
```

更新 Table Card 后，检查其中所有关键字段都存在于实时 Schema。

## 新增指标

1. 在 `metrics.json` 中定义唯一 ID、名称、别名、来源表、字段和聚合方式。
2. 如有必要过滤条件，在 `policies.json` 中增加可执行策略。
3. 增加覆盖该指标的 Gold、拒答和评测样本。
4. 通过 AST、Schema、只读执行和人工结果审核。
5. 重新训练知识索引和候选表召回器。

## Gold 晋升门禁

只有满足以下条件的记录才能设置为 `approved`：

- 问题语义明确。
- SQL 只包含只读查询。
- AST 解析成功。
- 表和字段均存在。
- 声明的候选表与 SQL AST 一致。
- 必要业务过滤条件完整。
- 使用只读账号执行成功。
- 人工确认结果与问题语义一致。

自动执行成功的记录只能进入 pending，不得自动晋升 Gold。

## 版本与审核

- 每条规则和样本使用稳定、唯一的 `id`。
- 修改指标口径时同步增加评测用例。
- 删除字段前先扫描 Table Card、规则、Gold 和评测集中的引用。
- 训练报告出现 rejected records 时不得直接发布。
- 定期清理重复语义样本，避免相同意图在向量索引中重复竞争。

## 历史 Markdown 迁移

如需迁移外部旧版问题文档，可显式指定文件：

```powershell
text2sql-migrate-knowledge --question path/to/legacy-question.md
```

迁移结果是 `candidate`，必须人工审核后才能进入 Gold。
