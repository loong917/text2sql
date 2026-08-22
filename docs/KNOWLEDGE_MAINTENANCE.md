# 结构化知识维护指南

## 单一事实来源

- 数据库实时 Schema：表、字段、类型和真实外键。
- `knowledge/schema`：离线快照和候选表检索卡片。
- `knowledge/domain`：指标、维度、实体词典、业务关联和强制策略。
- `knowledge/examples/gold_sql.jsonl`：经过人工确认的正确示例。
- `knowledge/examples/negative_sql.jsonl`：已知错误模式。
- `knowledge/examples/refusal.jsonl`：拒答边界。

不要把本文档或其他 Markdown 文件作为训练输入。

## Schema 变更

数据库结构变更后执行：

```powershell
text2sql-export-schema
text2sql-train-retriever
text2sql-train
```

更新 Table Card 后，检查其中所有关键字段都存在于实时 Schema。

## 新增指标

1. 在 `metrics.json` 中定义唯一 ID、名称、别名、来源表、字段和聚合方式。
2. 如有必要过滤条件，在 `policies.json` 中增加可执行策略。
3. 增加覆盖该指标的 Gold、拒答和评测样本。
4. 通过 AST、Schema、只读执行和人工结果审核。
5. 重新训练知识索引和候选表召回器。

在线语义解析直接从经过 Schema 校验的 `metrics.json`、`dimensions.json`、
`entities.json`、`policies.json` 和 `joins.json` 构建语义目录。新增别名、实体值、维度或关联时应修改
结构化数据，不应在 `semantic_ir.py` 中增加业务表或字段判断。

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

运行期反馈统一写入 `FEEDBACK_DB_PATH` 指定的 SQLite 数据库。普通用户的“正确/错误”
只生成 `pending_review` 审核记录；只有独立管理员接口在服务端重跑 AST、Schema、语义
和只读执行后，才能事务性晋升 Gold 或 Negative。

在线 `correct` 反馈还必须由服务端重新完成 AST、实时 Schema 和语义一致性检查。
审核客户端不能自行声明这些检查已经通过。Gold 记录会保存审核者、审核时间、
Schema 指纹及完整晋升证据；旧格式或缺少证据的运行期记录不会进入 Few-shot 检索。
在线 Few-shot 检索还会比较当前实时 Schema 指纹。表或字段发生变化后，旧 Schema
下审核的 Gold 会自动隔离，必须针对新 Schema 重新校验后才能恢复使用。离线训练读取
Gold 时仍会执行完整结构化知识与 AST 门禁。

## 版本与审核

- 每条规则和样本使用稳定、唯一的 `id`。
- 修改指标口径时同步增加评测用例。
- 删除字段前先扫描 Table Card、规则、Gold 和评测集中的引用。
- 训练报告出现 rejected records 时不得直接发布。
- 定期清理重复语义样本，避免相同意图在向量索引中重复竞争。

## 历史候选归档

`knowledge/examples/migration_review.jsonl` 仅保留早期迁移记录用于人工追溯，训练器
不会读取它。确认正确的记录应重新完成当前版本的 Schema、AST、执行结果和业务审核，
再以新 ID 写入 `gold_sql.jsonl`；未审核记录不得直接复制到在线知识库。
