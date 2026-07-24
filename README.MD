# 你是一名优秀的AI工程师，请分析这个基于Ollama工具使用本地qwen2.5-coder:14b大模型开发的text…

> 你是一名优先的AI工程师，请分析这个基于Ollama工具使用本地qwen2.5-coder:14b大模型开发的text2sql项目，可以从哪些方面进行优化，如训练机制、准确性等方面？请提供相关建议，并给出落地方案。

整体判断：这个项目已经具备一个可用的 Text2SQL 原型框架，但当前主要瓶颈不在模型大小，而在“反馈样本可信度、SQL 语义校验、检索精度和评测闭环”。如果直接微调 `qwen2.5-coder:14b`，收益很可能低于先完善这些环节。

现有训练报告只有 6 条评测、通过 3 条，准确率约 50%；当前 `EVAL_SET.json` 已有 10 条，说明报告和评测集还存在版本不同步问题。

## 一、现状与主要问题

### 1. 当前所谓“训练”本质上是 RAG 知识构建

项目目前没有更新 Qwen 模型权重，训练流程主要是：

- 从实时数据库提取 Schema。
- 解析 `DDL.MD`、`QUESTION.MD`。
- 生成别名、时间、指标、Join、问题模板等记忆。
- 使用 `bge-m3` 写入 ChromaDB。
- 将正确执行过的 SQL 写入反馈文件。
- 在后续 Prompt 中注入相似样本。

这属于“离线知识索引 + 在线 few-shot 学习”，不是 SFT/LoRA 微调。

这种机制适合项目早期，但不应把“执行成功”视为“语义正确”。

### 2. 自动反馈可能污染知识库

当前执行成功、结果非空的 SQL 会获得较高质量分并自动进入反馈库，相关逻辑在 [sql_service.py](./src/services/sql_service.py:233) 和 [sql_service.py](./src/services/sql_service.py:304)。

这是目前最高优先级的问题。例如反馈库里已有一条：

> “统计宁波市2025年的全血采集人次与成分血采集人次分别是多少？”

生成的 SQL 没有关联机构表，也没有 `City='宁波市'` 条件，但因为 SQL 可以执行，仍以 `quality_score=100` 保存。后续这条错误 SQL 还可能作为 few-shot 注入 Prompt，形成错误强化。

另外，同一问题可能保存多个粒度不同甚至相互矛盾的 SQL，而当前去重依据是“问题 + SQL”，不会识别同一问题的冲突答案。

### 3. SQL 校验主要依赖正则表达式

当前表名、字段、别名、Join、过滤条件检查大量基于正则，[validate_sql](./src/services/schema_service.py:1209) 可以拦截简单幻觉，但难以可靠处理：

- CTE、子查询、派生表。
- `UNION/UNION ALL` 每个分支的字段与过滤条件。
- Schema 限定名和方括号标识符。
- 字段不带别名时的归属。
- 聚合粒度与 `GROUP BY` 一致性。
- `LEFT JOIN` 被 `WHERE` 条件隐式变成内连接。
- 问题要求的条件是否出现在每个 UNION 分支。
- “SQL 可执行，但回答的并不是用户问题”。

只读门禁位于 [schema_service.py](./src/services/schema_service.py:205)，方向正确，但建议也迁移到 AST 解析。

### 4. 准确率错误主要属于业务语义错误

最近一次训练报告为 3/6 通过。典型错误包括：

- “成分血”SQL 缺少 `BCType='1'`。
- “杭州市”生成了 `City='杭州'`，与数据库真实枚举值不一致。
- 模型选对表和聚合字段，但漏掉关键过滤条件。

说明模型基本具备 SQL 语法能力，主要短板是：

- 指标到字段、枚举条件的映射不稳定。
- 地名等实体没有经过规范化。
- 生成后缺少问题—SQL语义对齐检查。

此时更换更大的模型不一定能根治。

### 5. 候选表召回仍是启发式打分

候选表选择使用表名、字段、中文说明、记忆标签和固定权重，见 [schema_service.py](./src/services/schema_service.py:678)。

存在几个风险：

- 固定阈值对小库有效，扩展到几十、几百张表后会失效。
- 中文关键词按较长词块提取，召回容易受问法变化影响。
- Chroma 向量相似度与手工索引分数没有统一归一化。
- 最多保留 6 张表、每表最多 12 个字段，可能裁掉必要字段。
- Schema 只有 2 张表时得到的效果不能代表复杂数据库表现。

当前训练索引虽然有 347 条记录，但只有 2 张真实表、34 个字段，且 342 条是业务记忆，结构型记录只有 5 条。知识数量较多，不代表覆盖面充足。

### 6. 实际没有使用 Ollama Tool Calling

虽然配置检查中强调模型需要支持工具调用，但在线主链路实际是直接调用：

```python
client.chat(
    model=settings.llm_model,
    messages=[{"role": "user", "content": message}],
    options={"temperature": 0},
)
```

见 [sql_service.py](./src/services/sql_service.py:476)。

因此当前系统是 Prompt 生成 SQL，而不是模型主动调用 `get_schema`、`search_examples`、`validate_sql`、`execute_sql` 等工具。现阶段这样反而比较稳定，但配置说明与实现需要统一。

## 二、优先级最高的落地方案

### P0：先修复反馈闭环

不要再把“执行成功”直接等价为正确样本。

建议把反馈分为四级：

| 等级 | 条件 | 用途 |
|---|---|---|
| Gold | 人工确认，且基准结果一致 | 可用于训练和 few-shot |
| Silver | 执行成功且通过全部语义校验 | 仅用于候选召回 |
| Pending | 执行成功但未经确认 | 等待人工审核 |
| Negative | 用户标错或结果不一致 | 用于负样本和错误分析 |

具体改造：

1. 自动执行成功只写入 `pending_feedback.jsonl`。
2. 只有用户点击“正确”，或与 baseline 结果完全一致，才能进入 Gold。
3. 同一规范化问题只能有一个活跃 Gold 答案。
4. 用户标记错误时，把错误类型记录为：
   - `wrong_table`
   - `missing_filter`
   - `wrong_metric`
   - `wrong_dimension`
   - `wrong_join`
   - `wrong_granularity`
   - `wrong_entity_value`
5. Prompt 默认只注入 Gold，不直接注入自动执行样本。
6. 修复或删除现有缺少宁波市过滤条件的反馈记录。

建议修改位置：

- [sql_service.py](./src/services/sql_service.py:253)
- [sql_service.py](./src/services/sql_service.py:374)
- [schema_service.py](./src/services/schema_service.py:1065)

验收指标：

- 错误自动入库率为 0。
- Gold 样本人工确认率 100%。
- 冲突样本能够被识别并进入审核队列。

### P0：引入 AST 级 SQL 校验

建议添加 `sqlglot`，以 `dialect="tsql"` 解析 SQL。

校验链路调整为：

```text
只读安全检查
→ T-SQL AST 解析
→ 表和字段解析
→ Join 路径检查
→ 指标/维度/过滤条件检查
→ SQL Server EXPLAIN/编译检查
→ 限行执行
→ 结果语义检查
```

需要检查：

- 只允许 `SELECT`、CTE 和集合查询。
- 禁止 DML、DDL、存储过程、`SELECT INTO`。
- 每个 `UNION` 分支都必须满足必要过滤条件。
- 字段必须能解析到唯一来源表。
- 非聚合输出字段必须与 `GROUP BY` 对齐。
- 用户问题中的城市、年份、血液类型必须出现在 AST 条件中。
- 问题要求“每个机构”时，结果必须包含机构维度。
- 问题要求“总量”时，不应意外按机构分组。

这些规则应由结构化语义对象驱动，而不是继续增加 SQL 正则。

### P0：增加问题语义中间层

在生成 SQL 前，先把问题解析成固定 JSON，例如：

```json
{
  "metric": "collection_count",
  "blood_type": "component",
  "dimensions": ["institution"],
  "filters": {
    "city": ["杭州市"],
    "date_start": "2025-01-01",
    "date_end": "2026-01-01"
  },
  "expected_granularity": "one_row_per_institution"
}
```

然后分别进行：

1. 语义解析。
2. Schema grounding。
3. SQL 生成。
4. SQL AST 与语义 JSON 对齐校验。

对于当前只有两个核心表的项目，业务语义解析甚至可以先用确定性代码完成：

- 全血 → `BCType='0'`
- 成分血 → `BCType='1'`
- 人次 → `COUNT(*)`
- 采集量 → `SUM(BCPVolume)`
- 每个机构 → `InstID + OrgName GROUP BY`
- 每个城市 → `City GROUP BY`
- 城市过滤 → 关联 `Pub_OrgAddress`

这样会显著降低 14B 模型的推理负担。

## 三、检索与 Prompt 优化

### 1. 使用混合检索和重排

建议将当前固定打分升级为：

```text
BM25/关键词召回
       +
bge-m3 向量召回
       ↓
候选合并
       ↓
bge-reranker-v2-m3 重排
       ↓
按知识类型和 token 预算选择上下文
```

检索对象应拆分 collection：

- `schema_nodes`
- `business_rules`
- `gold_sql_examples`
- `entity_dictionary`
- `negative_examples`

不要让 DDL 文本、业务规则和 SQL 示例在同一相似度空间中竞争。

### 2. 动态选择字段，不使用固定 12 列

当前字段上限为 12，见 [schema_service.py](./src/services/schema_service.py:904) 附近的字段选择逻辑。

建议按角色保留：

- 主键。
- 与候选表相连的外键。
- 问题命中的字段。
- 指标字段。
- 时间字段。
- 过滤枚举字段。
- Gold SQL 示例中出现过的字段。

最后才按 token 预算裁剪，而不是固定列数。

### 3. 实体值词典

针对 `杭州`/`杭州市` 这类错误，建立数据库实体词典：

```json
{
  "杭州": "杭州市",
  "杭州市": "杭州市",
  "宁波": "宁波市"
}
```

实体值应来源于：

- 低基数字段 `SELECT DISTINCT`。
- 人工业务字典。
- 用户输入别名。
- 历史 Gold 样本。

在生成前先 canonicalize，Prompt 和 SQL 校验都使用规范值。

### 4. Prompt 分角色并结构化输出

建议使用 system/user 消息：

- System：SQL Server、安全规则、输出协议。
- User：语义 JSON、候选 Schema、业务规则、Gold 示例、用户原问题。

让模型先返回结构化对象：

```json
{
  "sql": "...",
  "used_tables": [],
  "applied_filters": [],
  "assumptions": [],
  "confidence": 0.92
}
```

服务端解析后只向最终用户展示 SQL。结构化输出能让校验层更容易发现遗漏条件。

## 四、评测体系改造

当前评测集只有 10 条，而且训练数据和评测问题高度相似，容易高估泛化能力。训练报告还只运行了其中 6 条，应首先修复评测版本同步。

建议建立至少四个集合：

- `train_gold.jsonl`：可进入检索和训练。
- `dev.jsonl`：调 Prompt、阈值和规则。
- `test.jsonl`：严格隔离，禁止进入 Chroma。
- `challenge.jsonl`：歧义、拒答、复杂查询。

至少准备 100～300 条，覆盖：

- 同义改写。
- 城市、年份等实体替换。
- 单表、多表、三表 Join。
- Top N、排序、同比、环比、占比。
- 空结果。
- NULL 处理。
- 多个城市、多种血液类型。
- 模糊问题和无法回答的问题。
- 提示注入与危险 SQL。
- SQL 能执行但语义错误的陷阱题。

推荐指标：

| 指标 | 说明 |
|---|---|
| Execution Accuracy | 能否成功执行 |
| Result Match Accuracy | 与基准结果是否一致 |
| Semantic Accuracy | 指标、维度、过滤、粒度是否正确 |
| Table Recall@K | 必要表是否进入候选集 |
| Column Recall@K | 必要字段是否进入 Prompt |
| Refusal Precision/Recall | 该拒答时是否拒答 |
| Correction Success | 重试后是否修复 |
| P50/P95 Latency | 本地推理延迟 |
| Prompt Tokens | 每次上下文规模 |

第一阶段可设目标：

- Result Match：50% → 85%。
- 必要表 Recall@5：≥98%。
- 必要字段 Recall：≥95%。
- 错误反馈入库率：0%。
- 合法问题错误拒答率：<5%。
- 危险 SQL 拦截率：100%。

## 五、是否需要微调 Qwen

建议在积累至少 500～2000 条人工确认 Gold 样本后再做 LoRA/QLoRA。当前样本规模和可信度不足，直接微调容易把错误规则固化到模型中。

训练样本格式应包含：

- 用户问题。
- 压缩后的候选 Schema。
- 业务语义 JSON。
- 正确 SQL。
- 必要表、字段、过滤和粒度标签。
- 错误 SQL 及错误类型。

训练分两步：

1. SFT：学习领域映射和稳定输出协议。
2. 偏好训练：使用正确 SQL 与错误 SQL 构造 chosen/rejected 对。

训练数据按“问题模板或业务意图”拆分，而不是随机拆分，否则同一模板替换城市、年份后落入训练集和测试集，会造成数据泄漏。

对于本地部署，可以先尝试：

- `qwen2.5-coder:14b` 继续作为 SQL 生成器。
- 小模型负责意图解析和拒答判断。
- `bge-m3` 做召回。
- reranker 做重排。
- AST 校验器负责确定性约束。

这通常比让一个 14B 模型承担全部步骤更稳定。

## 六、工程与性能优化

建议补充：

- 固定依赖版本，避免 `vanna>=2.0` 带来的行为漂移，见 [pyproject.toml](./pyproject.toml:12)。
- 为 Schema、检索结果、Prompt 和模型响应增加 trace ID。
- 记录各阶段耗时：Schema、检索、LLM、校验、执行。
- 缓存“规范化问题 → 语义 JSON”和“Schema hash → Schema prompt”。
- Ollama 配置 `num_ctx`、`num_predict`、`keep_alive` 和 stop tokens。
- 对生成 SQL 设置较小 `num_predict`，减少无效生成。
- 数据库执行增加超时、最大扫描量和查询计划风险检查。
- 将 JSONL 反馈迁移到 SQLite/PostgreSQL；当前进程锁只适合单进程。
- 增加 pytest 单元测试和离线 CI。目前仓库未发现独立测试目录。
- 将运行时健康检查拆为 `/live` 和 `/ready`，避免每次健康检查触发昂贵初始化。

## 推荐实施顺序

第一周：

1. 禁止自动执行样本直接进入 Gold。
2. 清理已有错误反馈。
3. 修复训练报告只评测 6/10 的问题。
4. 加入问题语义 JSON 和实体标准化。
5. 为现有 10 条用例补充语义断言。

第二至三周：

1. 引入 `sqlglot` AST 校验。
2. 增加 Gold/Pending/Negative 分层。
3. 混合检索和 reranker。
4. 将评测集扩展到 100 条以上。
5. 建立准确率、召回率、延迟报表。

一个月后：

1. 积累人工确认样本。
2. 增加复杂查询和拒答集。
3. 达到 500～2000 条 Gold 后评估 QLoRA。
4. 通过 A/B 测试比较“原模型 + RAG”和“微调模型 + RAG”，而不是直接替换。

最终建议是：先把项目从“SQL 执行成功驱动”升级为“语义正确驱动”，再考虑微调。当前最值得投入的不是换模型，而是反馈门禁、AST 校验、语义中间层和隔离评测集；这四项会直接决定后续训练数据是否可信。

> 请在修复反馈闭环、引入 AST 级 SQL 校验、增加问题语义中间层、检索与 Prompt 优化等方面进行优化升级

已完成四个方向的优化升级，并通过 11 项回归测试。

主要改动：

- 新增问题语义 IR：识别指标、维度、城市、血液类型、时间范围和结果粒度，并注入 Prompt。
- 引入 `sqlglot` T-SQL AST 校验：
  - 拦截写操作和多语句。
  - 校验真实表、字段。
  - 校验每个 UNION 分支的过滤条件。
  - 校验 `BCType`、城市规范值、日期范围、聚合函数及分组粒度。
- 修复反馈闭环：
  - 自动执行成功 → Pending。
  - 人工确认正确 → Gold。
  - 人工标错 → Negative。
  - 旧的未经人工确认样本不再进入 Prompt 或离线训练。
  - Gold 晋升时清理同问题 Pending 和冲突答案。
- 检索升级为 Chroma 向量检索 + 元数据词法检索 + 业务标签重排。
- Prompt 改为 system/user 分层，加入语义 IR、Gold 示例和相似错误案例。
- 增加 Ollama 上下文、最大生成长度和模型驻留配置。

核心文件：

- [semantic_ir.py](./src/services/semantic_ir.py)
- [ast_validator.py](./src/services/ast_validator.py)
- [schema_service.py](./src/services/schema_service.py:331)
- [sql_service.py](./src/services/sql_service.py:253)
- [config.py](./src/core/config.py:32)
- [OPTIMIZATION.md](./OPTIMIZATION.md)
- [AST 与语义测试](./tests/test_semantic_ast.py)
- [反馈分层测试](./tests/test_feedback_tiers.py)

验证结果：

```text
Ran 11 tests
OK
```

测试覆盖了仓库现有全部 baseline SQL，以及漏 `BCType`、错误城市值、缺失日期范围、危险写操作、UNION 分支漏过滤、Pending→Gold 和 Negative 流转。

`sqlglot 28.10.1` 已安装到项目的 `tutorial_env`，依赖也已加入 [pyproject.toml](./pyproject.toml:18)。

暂未运行完整 `python -m src.train`，因为该操作会连接 Ollama 和 SQL Server，并重建现有向量知识库。现有工作区中原本未提交的其他改动均已保留。
