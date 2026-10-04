# 表召回与概率校准

## 职责

table_card.py/schema_graph.py 生成业务/结构文档与路径；table_retriever.py 管理嵌入和指纹缓存；calibrator.py 拟合Platt概率与阈值；dataset.py 隔离标签；train.py 发布检索产物。

## 边界

不手写表名/字段加分或固定特征权重。语义必需表是硬约束；可选表用独立校准概率与预算。线上/held-out共用 Domain 选择策略。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run pytest -q tests/test_table_retrieval.py tests/test_retrieval_policy.py
uv run text2sql-train-retriever
~~~

## 扩展模板

补真实难负例、独立 calibration 与 frozen Test；新评分器须校准、绑定模型/Schema/表卡/数据身份。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

没有合格校准器不能宣称概率可信；生产数据不足修审核集，不降低阈值。训练读真实模型/结构并写本地文件。

[源码总览](../../README.md) · [专题说明](../../../docs/RETRIEVAL.md)
