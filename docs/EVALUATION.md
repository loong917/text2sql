# 评测数据维护

评测数据位于 `evaluation/`，采用一行一条记录的 JSONL 格式。

| 文件 | 用途 | 是否参与训练 |
|---|---|---|
| `retrieval_train.jsonl` | 候选表召回器训练 | 是 |
| `dev.jsonl` | Prompt、规则和阈值日常调试 | 否 |
| `test.jsonl` | 发布前冻结验收 | 否 |

三个文件的问题 ID 和规范化问题文本必须完全不重叠。召回训练器不会自动读取
Gold 反馈，只有明确放入 `retrieval_train.jsonl` 的样本才参与召回训练。

## 数据格式

所有记录必须包含稳定 `id`、`split`、`category`、`difficulty`、`question`、
`should_refuse` 和 `status=approved`。非拒答记录必须包含 `baseline_sql`；
开发集和测试集还必须包含 `expected_semantic_ir`。

## 日常评测

默认使用：

```dotenv
TRAINING_EVAL_SPLIT=dev
```

执行 `text2sql-train` 后，开发集结果写入训练报告。日常调整不能使用测试集结果
选择 Prompt、阈值或规则。

## 发布验收

发布候选版本使用独立命令，不重建知识库：

```powershell
text2sql-evaluate --split test --output ./vanna_knowledge_db/test-report.json
```

测试问题不得复制到训练集、Gold 召回来源或开发集。

## 新增用例

1. 根据用途选择唯一 split。
2. 使用稳定 ID，并填写分类和难度。
3. 由业务人员确认问题、语义 IR 和基准 SQL。
4. 使用只读数据库验证基准 SQL。
5. 运行全量测试，确认数据集不重叠且 AST 校验通过。
