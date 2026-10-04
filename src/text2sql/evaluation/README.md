# 完整准确性与冻结证据

## 职责

dataset.py 校验审核/执行证据与 Case 隔离；gold_set.py 负责 canonical 生命周期和不可变导出；service.py 比较实际计划与限行只读基准；domain/semantic_contract.py 共享完整 v3 真值合同；comparison.py 保留类型/NULL/重复；report_contract.py/reporting.py 核验完整证据和重算门禁；run.py 固定输入与发布报告。

## 边界

只比较本次 response 实际 QueryPlan，缺失不重新解析兜底。物理绑定必须符合冻结目录。执行分母是全部正例，截断/未执行不能消失。数据/五个产物文件一次读取并从相同字节消费/摘要。

--artifact 显式选择登记候选，不改ACTIVE。Test固定写VERSION/test_report.json，已批准版本禁止重写；Dev输出必须是独立JSON，不能覆盖输入、产物或Chroma。全程持有训练/晋升共用lease，报告发布前复验模型、集合与文件；门禁失败报告不代表正式批准。

0.8.1 补齐可解析已批准反例的 held-out 模板隔离；仅真实单查询语法错误允许特殊处理，错误标签不能规避检查。集合 v2 证据包含记录与实际检索配置，配置变化即使记录未变也不能沿用 Test；旧 v1 必须重建和重评测。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run pytest -q tests/test_evaluation_accuracy.py tests/test_evaluation_runner.py tests/test_evaluation_report_integrity.py
uv run text2sql-evaluate --artifact VERSION_FROM_CANDIDATE_ARTIFACT --split test --enforce-gate
~~~

## 扩展模板

新语义槽更新快照/数据契约/报告和人工预期；比较规则显式配置，不能仅依赖字符串或浮点强制转换。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

基础设施失败不是正确拒答；Test只有真实目标库/模型执行并达到规模与门槛才能放行。报告写本地，不修改业务数据。

[源码总览](../../README.md) · [专题说明](../../../docs/EVALUATION.md)
