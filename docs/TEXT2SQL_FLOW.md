# Text to SQL 生成链路维护手册

## 责任与真值

本手册解释根 [README 的在线流程图](../README.MD#text-to-sql-生成链路流程图)。以当前源码、端口契约和回归测试为真值，不从早期“直接把问题发给模型”的设计推断行为。

| 节点 | 实现入口 | 必须保留的断言 |
| --- | --- | --- |
| 鉴权与发布门禁 | api/auth.py、api/server.py、release/runtime_gate.py | 管理员凭据独立；Cookie 写请求同源；ACTIVE 版本审批、Test 文件与集合证据失效不执行 |
| Schema 与知识快照 | application/context_service.py、infrastructure/context_adapters.py | 实时指纹匹配快照，禁止混合新旧 Schema |
| QueryPlan | domain/semantic_ir.py、domain/time_resolution.py、domain/query_plan.py | 目录绑定、状态明确、未知条件澄清，保留实际计划证据 |
| 表召回 | retrieval/table_retriever.py、domain/retrieval.py | 必需表硬约束；可选表用校准概率，不手写固定加分 |
| Prompt | application/context_rendering.py、application/context_service.py | 精确登记；必需内容不能截断；生产反馈冻结 |
| 编译/生成 | application/text2sql_service.py、domain/sql_compiler.py、infrastructure/ollama_generator.py | 编译优先；模型输出必须符合结构化契约 |
| SQL 校验 | domain/sql_validation.py、domain/sql_scope.py、domain/sql_semantics.py | 只读、对象授权、列绑定与查询语义 |
| 执行 | application/query_execution.py、infrastructure/query_adapters.py | 校验后执行；限行、超时、并发与取消资源安全 |
| 响应与反馈 | application/query_response.py、application/feedback_service.py | 结果类型与截断明确；候选不能自动晋升 |

上表路径相对于 src/text2sql/。新增或移动代码时，同步更新表格、流程图与测试。

## 失败与重试

| 条件 | 处理 | 是否执行/重试 |
| --- | --- | --- |
| 无效凭据/跨源 Cookie 写请求 | HTTP 401/403 | 不构建查询 |
| 生产配置/证据无效 | 配置错误或未就绪 | 不绕过门禁 |
| Schema、依赖或模型传输不可用 | infrastructure_error | 不伪装拒答 |
| 歧义、未知条件或上下文过大 | clarification_required | 不生成/执行 |
| 明确不支持或依据不足 | refused | 不执行 |
| 模型格式错误/空候选 | generation_failed | 次数预算内重试 |
| 模型 SQL 校验失败 | validation_failed | 上一候选与校验原因进入修复 Prompt，有限重试 |
| 编译或编译 SQL 校验失败 | validation_failed | 不切换模型规避约束 |
| 数据库执行失败 | infrastructure_error | 不做自动试探式 SQL 执行修复 |
| 查询成功、反馈写入失败 | 保留 success，记录反馈失败 | 不重复执行 |

max_retries=0..3 表示额外重试，最多 1..4 次；仅生成模式仍执行全部 SQL 校验。异常与取消不是通用可重试 SQL 错误。

## 可信反馈闭环

~~~mermaid
flowchart LR
    realFailure["真实失败与执行证据"] --> candidate["候选区与失败分类"]
    candidate --> review["人工口径审核与服务端复验"]
    review --> approved["审核知识与绝对日期 Gold"]
    approved --> isolation["正例与可解析反例模板隔离"]
    isolation --> generation["同代不可变 generation"]
    generation --> build["候选快照、校准、集合内容与配置证据 v2"]
    build --> devGate["Dev 门禁"]
    devGate --> staged["stage 候选，登记 VERSION，不切 ACTIVE"]
    staged --> frozenTest["--artifact VERSION 独立冻结 Test"]
    frozenTest --> releaseCheck["--artifact VERSION 只检查全部发布门禁"]
    releaseCheck --> promote["显式 --promote：写版本审批，再原子切 ACTIVE"]
    promote --> rollout["重启、运行复验与目标环境灰度"]
    rollout -.-> realFailure
~~~

在线反馈不即时改写生产 Prompt。执行成功、用户点击正确、客户端行数和模型自评都不能作为 Gold 真值；仍需人工口径审核。

0.7.0 的离线输入先进入 canonical candidate，经双人审核、内容/Schema摘要、真实完整基准及模板隔离后，导出同代不可变 generation 再训练。生产不直接吸收 SQLite 历史 Gold；完整 Schema 的旧缺省项不能靠指纹补成可信结构。当前真实 Gold 仍为 38 candidate / 0 approved。具体节点合同见 [Gold 治理](GOLD_SET.md)。

0.8.0 的 train 通过 Dev 仅 registry.stage，写 candidate_artifact.json 与 VERSION/artifact.json，不切 ACTIVE。evaluate 默认测 ACTIVE，--artifact VERSION 显式固定注册候选；候选 Test 默认写 VERSION/test_report.json，不覆盖线上证据。release-check --artifact VERSION 只检查；--promote 且全部门禁通过才写 VERSION/approved_release.json，再原子切 ACTIVE，指针带 release_manifest_path、审批带 test_report_path。runtime 只认该 ACTIVE 版本审批和版本内 Test 报告，global EVAL_TEST_REPORT_PATH 不再作为生产在线权威来源。检索指纹 v2 必须重新拟合与校准，旧发布/检索证据不自动迁移。

0.8.1 的 training_manifest.outputs.collection_evidence v2 绑定实际集合文档、逐条元数据、向量、集合级检索配置与受控查询嵌入身份，评测前后、发布、运行时均核验。已批准的完整可解析反例也参加 held-out 模板隔离；只有无可识别查询主体的真实单查询语法反例豁免 SQL 模板，问题/查询族仍隔离。完整主体加损坏后缀失败关闭，不自动修复或执行。生产快照重复检查独立审核，并与同代 generation 知识匹配。旧 v1 证据需重建候选并重新评测；候选失败保留旧 ACTIVE，晋升成功也不等于目标环境压测、灰度和真实业务验收已完成。

## 能力扩展模板

1. 在结构化目录声明指标/维度/实体/关联，指定责任人与真实案例。
2. 定义 QueryPlan 槽、歧义和拒答边界，不将业务词写入 Domain。
3. 补编译规则；模型路径接受同一个 Validator。
4. 写安全/语义反例，证明错误 SQL 会产生不同结果。
5. 补契约、集成、HTTP 和真实基准用例；训练、校准、Test 模板隔离。
6. 更新图与 README，固定身份后重训、评测、发布。

~~~text
能力/口径：
审核人与来源：
语义槽与歧义：
允许的 SQL 形状：
禁止的反例：
回归与真实 Case ID：
知识/Prompt/模型/策略是否变化：
评测、发布与回滚证据：
~~~

## 图的维护与验收

Mermaid 源码留在 Markdown，使用稳定 ID、显式分支与真实重试边。根 README 是在线图的唯一维护位置，本页只维护离线闭环图，避免多份在线图漂移。

文档测试检查链接、模块 README、关键门禁/模式/重试节点与源码路径；变更后仍应在 GitHub/Codex Markdown 预览检查排版。图不宣称任意自然语言/SQL 的完整等价证明。
