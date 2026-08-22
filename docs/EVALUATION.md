# 评测数据维护

评测数据位于 `evaluation/`，采用一行一条记录的 JSONL 格式。

| 文件 | 用途 | 是否参与训练 |
|---|---|---|
| `retrieval_train.jsonl` | 候选表召回器训练 | 是 |
| `retrieval_calibration.jsonl` | 独立选择概率阈值 | 否 |
| `retrieval_test.jsonl` | 召回器冻结质量门禁 | 否 |
| `dev.jsonl` | Prompt、规则和阈值日常调试 | 否 |
| `test.jsonl` | 发布前冻结验收 | 否 |

五个文件的 ID、`template_id` 和规范化问题文本必须完全不重叠。加载器还会解析
`baseline_sql`，移除年份、城市、枚举值和输出别名后自动计算 AST 模板指纹；指纹
跨 split 重复时测试直接失败。因此 `template_id` 只用于人工分类，不能再作为防泄漏
的唯一依据。禁止仅替换实体值或结果别名后跨 split 复用。
召回训练器不会自动读取
Gold 反馈，只有明确放入 `retrieval_train.jsonl` 的样本才参与召回训练。

召回器只在 Train 上拟合 Platt 模型，在 Calibration 上选择满足目标召回率的阈值，
最终只使用 Retrieval Test 计算 Recall 和 False Positive Rate。Test 指标不允许参与调参。

## 数据格式

所有记录必须包含稳定 `id`、`template_id`、`split`、`category`、`difficulty`、`question`、
`should_refuse` 和 `status=approved`。非拒答记录必须包含 `baseline_sql`；
开发集和测试集还必须包含 `expected_semantic_ir`。

`expected_semantic_ir` 使用版本化规范快照。目前 `version=2`，字段包括 `metrics`、
`dimensions`、`entity_filters`、`date_range`、`granularity`、`required_tables` 和
`result_shape`、`time_granularity`、`comparison`、`ambiguities`。数据集只需声明需要断言的字段，但不得使用 `whole/component` 等评测层
二次映射值；实体值必须与领域 IR 的规范值一致。

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
text2sql-evaluate --split test --enforce-gate --output ./vanna_knowledge_db/test-report.json
```

测试问题不得复制到训练集、Gold 召回来源或开发集。

评测报告包含总体通过率、正向问题与拒答问题通过率、按类别和难度切片的指标，
以及语义 IR、候选表召回、执行成功等检查维度。`--enforce-gate` 使用 `EVAL_MIN_PASS_RATE`、
`EVAL_MIN_POSITIVE_PASS_RATE` 和 `EVAL_MIN_REFUSAL_PASS_RATE` 执行发布门禁，并使用
`EVAL_MIN_CASES`、`EVAL_MIN_POSITIVE_CASES`、`EVAL_MIN_REFUSAL_CASES` 防止空切片或
样本过少产生虚假高分；未达标时命令返回非零退出码。
`EVAL_MIN_SEMANTIC_IR_PASS_RATE`、`EVAL_MIN_EXECUTION_PASS_RATE` 和
`EVAL_MIN_RETRIEVAL_RECALL` 分别阻止语义契约、真实执行或召回质量回退。
评测文件损坏或为空时流程直接失败，不再生成空的成功报告。

正式发布还要满足 `PRODUCTION_EVAL_MIN_*` 数量门禁，默认至少 100 条，其中正向
问题不少于 80 条、拒答或越界问题不少于 20 条。小样本只用于研发回归，不能作为
生产准确率证明。冻结 Test 默认写入 `EVAL_TEST_REPORT_PATH`，随后由
`text2sql-release-check` 校验报告与当前代码、模型、数据和知识版本是否完全一致。
召回 Calibration 和 Retrieval Test 默认也分别要求至少 30 条，避免三五条样本得到
100% Recall 后被误判为可上线模型。

`TRAINING_EVAL_SPLIT` 固定为 `dev`。冻结 Test 只能通过上述独立命令执行，避免
训练过程读取 Test 指标并形成调参泄漏。

## 新增用例

1. 根据用途选择唯一 split。
2. 使用稳定 ID，并填写分类和难度。
3. 由业务人员确认问题、语义 IR 和基准 SQL。
4. 使用只读数据库验证基准 SQL。
5. 运行全量测试，确认问题、人工模板和自动 AST 指纹均不跨 split 重叠。
