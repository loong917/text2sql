# Schema 与业务表卡资产

## 职责

schema_snapshot.json 是完整权威元数据的离线快照；table_cards.jsonl 是审核业务检索描述。旧不完整文件已归档为 legacy_schema_snapshot.json，仅保留追溯，不作为默认输入。离线快照不替代生产实时核验；表卡不授权模型访问不存在或敏感字段。

## 操作与边界

~~~powershell
uv run text2sql-export-schema --live --output knowledge/schema/schema_snapshot.json
uv run text2sql-train-retriever
~~~

显式 --live 读取 SQL Server 系统目录并关闭全部资源；默认或 --snapshot 从完整知识快照导出。失败不覆盖已有输出。生产检索训练只允许 live，不会因数据库不可用自动退到历史快照。

## 验收与失败处理

检查表列类型/可空性、Schema 身份、明确主键/唯一键/FK及指纹；生产表卡须有 grain 和绑定内容/Schema 的双人审核。组合 FK 完整展示，但当前单列计划不学习部分约束。元数据不可见时修账号权限，不用缓存猜结构。详见 [结构与 Gold 治理](../../docs/GOLD_SET.md)。

关联资料：[Schema 导出模块](../../src/text2sql/cli/README.md)、[检索](../../docs/RETRIEVAL.md)。
