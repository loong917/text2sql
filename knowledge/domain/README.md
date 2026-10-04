# 业务语义目录维护

## 职责与边界

metrics.json 定义指标 ID、聚合、物理表列、单位与输出别名；dimensions.json 定义维度/日期字段；joins.json 定义关联端点及基数；entities.json 定义别名到规范值；policies.json 声明治理约束。

业务词汇只在这里维护，不写入 Domain Python。真实 Schema 是结构权威；知识不能创造表列或假设唯一性/关联基数。

## 变更模板与验收

~~~text
业务名称/指标口径：
负责人/审核来源：
来源Schema表列：
别名/规范值/歧义：
聚合/粒度/去重键：
关联与基数证据：
成功/澄清/禁止反例：
真实评测Case ID：
~~~

修改后运行结构化知识与语义回归，再重新构建、评测与发布。只修 JSON 不能使已冻结运行快照即时生效。详情见 [知识治理](../../docs/KNOWLEDGE_MAINTENANCE.md)。
