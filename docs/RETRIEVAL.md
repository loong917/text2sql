# 候选表召回与 Prompt

## 选择机制

审核业务表卡与实时 Schema 一起生成每表一个检索文档，包含业务别名、指标、维度、关键字段和结构事实。业务记录不能引入不存在的表列。

嵌入余弦分数经 Platt 校准转为概率；训练使用真实标签、难负例和类平衡样本权重，阈值只在独立 calibration 集选择，不手写表名/字段名加分或固定特征权重。

语义计划明确要求的表是硬约束，必要桥接表来自 Schema 图。其余表按校准概率与预算选择。没有校准器时仍可使用可证明的语义必需表，但不声称概率已校准；生产就绪要求有效校准产物。

在线与 held-out 检索评测调用同一个 select_candidates。拒答、语义未落地及预算限制都会反映在评测中，不以原始 pair 阈值通过率代替在线召回。

## 缓存与绑定

索引缓存绑定 Schema、业务表卡和嵌入身份；single-flight 构建失败保留上一完整快照，不暴露半成品。校准器校验 Schema、嵌入模型、训练数据与业务表卡指纹。

在线数据来自冻结 knowledge_snapshot.json，而不是修改后的知识目录或原始评测数据。改变输入后必须重新构建对应产物。

## Prompt

必需内容包含 QueryPlan、完整必需字段、关联和约束。可选的审核 Gold、相关记忆和负例在预算内加入；不存在相关候选时不退回无关记忆。

完整 Prompt 使用 UTF-8 字节上界控制预算，预留生成空间。必需内容过大时要求缩小范围，不截断安全约束。表卡选择仍使用字符成本估算，最终以完整 Prompt 门禁兜底。

## 训练与诊断

~~~powershell
uv run text2sql-train-retriever
~~~

独立训练支持 live、snapshot 和 auto Schema 来源。离线快照需指纹匹配；它只能用于训练诊断，不能替代生产数据库检查。

查看报告中的 held_out_table_recall、negative_false_positive_rate、refusal_pass_rate、question_results、fit_hard_negative_pairs 和 provenance。当前小型数据集及保守语义覆盖仍可能导致候选被拒绝，应补业务目录和独立评测，不降低门槛。
