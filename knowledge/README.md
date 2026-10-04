# 结构化知识维护资产

## 职责与目录

本目录保存人工维护、可审核的业务事实，不是可自由解释的 Markdown Prompt。Markdown 只说明治理；JSON/JSONL 与真实 Schema 是机器契约。

| 资产 | 用途与维护入口 |
| --- | --- |
| manifest.json | 知识格式声明 |
| [schema](schema/README.md) | 冻结结构快照与审核业务表卡 |
| [domain](domain/README.md) | 指标、维度、关联、策略、实体别名与规范值 |
| [examples](examples/README.md) | Gold、负例、拒答及历史待审材料 |

## 信任与操作边界

实时 SQL Server Schema 是结构权威，离线快照不替代生产核验。加载验证表列引用，Gold 还核对计划与 AST；训练生成完整 knowledge_snapshot.json，在线只消费活动版本，不动态读本目录。

记录必须由独立业务/数据人员确认口径、事实 grain、单位与别名，review 绑定原内容和真实 Schema。Gold 使用共享 v3 语义真值、实际执行证据与模板隔离。当前历史样本已统一转为 canonical candidate，运行示例为空；见 [Gold 手册](../docs/GOLD_SET.md)。DDL.MD、QUESTION.MD 不再是运行输入；历史材料不作为无用缓存删除。

## 可复用变更模板

~~~text
业务口径/来源/负责人：
Schema与指标版本：
变更文件与记录ID：
新增/修改/废弃字段或别名：
批准状态与人工审核：
正例/反例/拒答/真实Case ID：
检索与评测模板隔离：
重训、真实Test、发布及回滚证据：
~~~

## 操作与验收

~~~powershell
uv run pytest -q tests/test_structured_knowledge.py tests/test_knowledge_artifacts.py
uv run text2sql-db-check
uv run text2sql-train-retriever
uv run text2sql-train
uv run text2sql-evaluate --split test --enforce-gate
~~~

测试是本地夹具；其它命令读取真实依赖并写本地版本/报告，不修改业务数据。先取得合格校准器再训练。生产还需独立 release-check 与目标环境验收。

## 失败处理

错误知识在构建前被拒绝；Dev失败不切 ACTIVE。修正审核数据并重建，不手工编辑冻结索引/清单求通过。历史被旧逻辑压缩或改写的已存 SQL 无法自动恢复，需重新审核 Gold、重建及评测后发布。

详见 [知识维护](../docs/KNOWLEDGE_MAINTENANCE.md)、[数据模型](../docs/DATA_MODEL.md)、[生成闭环](../docs/TEXT2SQL_FLOW.md)。
