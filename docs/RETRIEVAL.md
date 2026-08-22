# 候选表学习型召回

候选表召回不再使用表名、字段名、知识类型、置信度和 Join bonus 等人工权重。

## 运行链路

1. 实时 Schema 被转换成“一表一卡”的 Table Card。
2. 问题与语义 IR 共同生成查询向量。
3. 如果语义 IR 无法映射到指标、维度、实体或领域表，直接拒答，不进入向量阈值判断。
4. Ollama `EMBEDDING_MODEL` 对问题和 Table Card 编码。
5. cosine 分数经 Train 数据拟合的 Platt 模型转换成概率。
6. 使用校准产物中学习得到的阈值选择候选表。
7. 语义 IR 明确要求的表始终保留。
8. Schema 外键图只补充连接种子表的最短路径桥接表，不参与相关性加分。
9. 最终上下文受 `TABLE_RETRIEVAL_TOKEN_BUDGET` 控制，不再固定 Top 4/Top 6。

## 训练召回器

确保 SQL Server、Ollama 和 `bge-m3` 可访问，然后运行：

```powershell
text2sql-train-retriever
```

或：

```powershell
python -m src.retrieval.train
```

训练过程会：

- 只从 `evaluation/retrieval_train.jsonl` 的 `baseline_sql` 拟合概率模型。
- 使用 `retrieval_calibration.jsonl` 选择满足目标召回率的阈值。
- 只在 `retrieval_test.jsonl` 上计算最终 Recall 和 False Positive Rate。
- 生成侧 `dev.jsonl`、`test.jsonl` 和 Gold 反馈不会自动进入召回训练。
- 将其余真实表生成负样本。
- 将 embedding 排名靠前但标签为负的表标记为 hard negative。
- 拟合 Platt 概率模型。
- 在独立 Calibration 上按目标 Table Recall 99% 确定阈值。
- 在冻结 Retrieval Test 上验证召回率和负样本误召回率；超过
  `TABLE_RETRIEVAL_MAX_FALSE_POSITIVE_RATE` 或召回率不达标
  时拒绝发布校准器，线上继续使用安全的语义 IR fallback。
- 负例误报率按“语义拒答门禁 + 校准概率”端到端选择结果计算；确定无法落到领域目录
  的拒答问题不会被向量相似度重新放行。

产物：

```text
vanna_knowledge_db/
├── table_retrieval_calibrator.json
├── table_retrieval_calibrator.previous.json
├── table_retrieval_calibrator.rejected.<timestamp>.json
└── table_retrieval_dataset.jsonl
```

生效校准器绑定 Schema 指纹、Embedding 模型和三个召回数据集的联合内容指纹。运行时
会同时校验这三类指纹，任一输入变化时旧产物不会继续加载。新候选只有通过冻结
Retrieval Test 才会原子替换 active；
失败报告写入 `rejected`，不会覆盖上一可用版本。

## 未校准行为

默认 `TABLE_RETRIEVAL_REQUIRE_CALIBRATION=true`。

校准产物不存在时，运行时只使用语义 IR 明确要求的表以及必要的外键桥接表，不会退回旧人工打分。无法确定领域表的问题会拒答。

开发期间如需观察未校准 embedding 排序，可以临时配置：

```dotenv
TABLE_RETRIEVAL_REQUIRE_CALIBRATION=false
```

此模式会在 token 预算内保守保留 embedding 结果，不建议直接用于生产。

当前仓库的小规模数据只有两张表，而且多数问题同时使用两张表。首次离线训练的
负样本区分度可能不足；这时训练报告会显示 `calibrator_accepted=false`。应补充更多
拒答问题、单表问题、相似干扰表和线上 hard negative 后重新训练，不应人为降低阈值
绕过质量门禁。

## 评测指标

召回器主要关注：

- Table Recall
- Table Recall@K
- 平均候选表数量
- 候选表 Prompt token 数
- 错误拒答率
- 桥接表准确率

准确率评测必须按问题模板或业务意图划分训练集与验证集，避免同一模板仅替换年份、城市后同时进入训练和验证。
