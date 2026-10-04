# 审核示例与候选材料

## 职责与准入

三个运行 JSONL 是 canonical Gold Set 的派生视图，目前为空以隔离旧未审核数据。38 条原始样本在 [Gold 源](../../evaluation/gold_set/README.md) 待审；原字节完整留档。migration_review.jsonl 仅为旧 QUESTION 追溯材料，后续候选只维护 canonical 源。

approved 需要独立业务/数据审核、内容摘要及当前 Schema；正例还需真实完整执行证据与 v3 语义真值。仅状态标签不构成准入，修改内容使审核失效。相对日期不能冻结为长期 Gold。生产运行反馈需经过 canonical 再次审核/导出，不直接吸收 SQLite 历史 Gold。

## 审核记录模板

~~~text
来源与Case ID：
问题/业务口径：
Schema及指标版本：
正确SQL/错误类型/拒答理由：
真实执行与基准证据：
审核人/状态/时间：
受影响评测模板及隔离检查：
~~~

## 验收与失败处理

通过知识回归后重建候选、Dev 与独立 Test；生产不会即时读取在线修改。保留 rejected/candidate 和历史材料供追溯，不将待审文件视为无用缓存删除。

详见 [知识维护](../../docs/KNOWLEDGE_MAINTENANCE.md) 与 [评测数据](../../evaluation/README.md)。
