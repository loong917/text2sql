# 可安装 Schema 导出入口

## 职责

export_schema_snapshot.py 从实时系统目录（显式 --live）或完整 knowledge_snapshot.json 导出结构和指纹；gold_set.py 提供 schema/check/migrate/export/verify 资产治理入口。

## 边界

默认是离线冻结产物导出；--live 只读真实元数据，供首次审核，不训练模型。无完整元数据拒绝覆盖，不从残缺知识索引猜 Schema。Gold check/schema 不连接模型或数据库；export 只写不存在的新 generation，不自动批准。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run text2sql-export-schema --help
uv run text2sql-export-schema --live --output knowledge/schema/schema_snapshot.json
uv run text2sql-gold-set check
uv run text2sql-export-schema --snapshot PATH_TO_KNOWLEDGE_SNAPSHOT --output PATH_TO_SCHEMA_JSON
~~~

## 扩展模板

新工具放正式包中，提供 --help、显式输入/输出、安全默认和测试；不恢复根 scripts 兼容层。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

导出写本地目标文件，运行前核对路径。首次建库仍需真实Schema检索/训练，不能用导出绕过元数据核验。

[源码总览](../../README.md) · [专题说明](../../../knowledge/schema/README.md)
