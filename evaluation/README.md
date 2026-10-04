# 评测数据资产

## 职责与边界

本目录五个 JSONL 是不可变 generation 的派生视图，不是自动生成的业务真值。历史 38 条记录已归档并转为 [canonical candidate](gold_set/README.md)，当前 approved 为 0，运行视图为空；真实生产数据尚未就绪。审核与导出合同见 [Gold 手册](../docs/GOLD_SET.md)。

| 文件 | 用途 | 验收边界 |
| --- | --- | --- |
| retrieval_train.jsonl | 拟合表召回概率映射 | 不使用 held-out 模板 |
| retrieval_calibration.jsonl | 独立选择阈值 | 不同时拟合并声称独立 |
| retrieval_test.jsonl | 最终选择策略验收 | 不看结果后反复调参 |
| dev.jsonl | 候选知识 Dev 门禁 | 不替代冻结 Test |
| test.jsonl | 生产完整查询验收 | 正例必须实际执行并与独立基准比较 |

## 用例准入

真实问题/失败 → 业务口径确认 → 数据负责人验证基准与受控数据库快照 → 完整 v3 语义预期/AST 断言 → 双人审核与内容摘要 → 模板隔离 → 不可变导出。

每条用例需要 ID、split、template_id、category、difficulty、来源、question、明确 outcome、严格语义预期和当前 Schema 审核证据。正例需要基准 SQL 与真实执行证据；Test 正例还必须显式 must_execute=true。数值容差、排序、空结果及否定边界需明示。不自动标 approved，不只改快照版本号，不用重复模板凑生产规模。

同形模板检查按 Scope 绑定真实物理表与列来源，并规范化别名、CTE/派生关系和输出标签；修改日期、实体值或展示标签不会变成独立模板。generation 验证从 canonical 重算派生行，不能只改 JSONL 和 manifest 摘要。新增业务先修改并审核 canonical/知识源，再重新导出整代，不直接编辑本目录运行视图。

## 候选验收操作

~~~powershell
uv run pytest -q tests/test_evaluation_datasets.py tests/test_evaluation_semantic_contract.py
uv run text2sql-gold-set check
$candidateVersion = "替换为训练输出中的版本ID"
uv run text2sql-evaluate --artifact $candidateVersion --split dev --enforce-gate
uv run text2sql-evaluate --artifact $candidateVersion --split test --enforce-gate
uv run text2sql-release-check --artifact $candidateVersion
~~~

pytest 是本地合成合同检查，不代表真实准确率。后面的评测读取真实模型/只读数据库并写本地证据，不修改业务表。当前批准集为空，相关真实训练/评测应保持失败，不跳过审核直接运行旧记录。

Test 报告归所属版本目录的 test_report.json；--output 不允许转向公共或其它版本报告。Dev 输出不能覆盖受保护输入或 registry。选择版本的 release-check 自动使用该版本 Test 报告，检查通过也不切 ACTIVE；显式 --artifact VERSION --promote 才申请晋升。检查失败不删除旧审批。已批准版本不能通过覆写报告原地升级，应建立新候选版本。

评测、训练与晋升共用独占 lease，不自动抢占超时锁。生产指针绑定本版本 approved_release.json，审批再绑定本版本 test_report.json；独立可变的 latest 报告/发布归档不能作为运行时批准来源。

## 证据与失败处理

报告绑定一次读取的数据、冻结知识文件、实际集合 v2 内容/配置指纹和运行身份；actual QueryPlan 不独立重解析补造。可解析反例 SQL 也属于训练侧，不能泄漏 held-out 模板，syntax_error 标签必须与真实解析失败相符。旧 v1 集合证据需重建和重评测，不能补摘要冒充。缺失、截断、重复输出列、弱类型、错误物理绑定及基准失败不能声称完整通过。

Test 暴露的问题进入下一轮受审核开发周期，不修改本次冻结预期让报告通过。保留 Case ID、失败结果和审计历史；变更口径后重训、重新校准、评测、审批和晋升。正式最低规模为 Test 100/80/20、检索 held-out 各 30，详情见 [评测合同](../docs/EVALUATION.md) 与 [运维手册](../docs/OPERATIONS.md)。
