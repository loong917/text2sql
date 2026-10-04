# 项目文档导航与维护资产

## 按角色使用

| 角色/任务 | 入口 |
| --- | --- |
| 新开发者 | [根 README](../README.MD)、[架构](../ARCHITECTURE.md)、[源码](../src/README.md) |
| 排查查询错误/扩展能力 | [在线链路](TEXT2SQL_FLOW.md)、[检索](RETRIEVAL.md)、[测试](../tests/README.md) |
| 业务知识审核 | [知识维护](KNOWLEDGE_MAINTENANCE.md)、[数据模型](DATA_MODEL.md)、[知识目录](../knowledge/README.md) |
| 准确率验收 | [Gold Set 治理与数据库整改](GOLD_SET.md)、[评测规范](EVALUATION.md)、[数据资产](../evaluation/README.md) |
| 运维/上线 | [部署](OPERATIONS.md)、[TLS](DB_TLS_RUNBOOK.md)、[生产放行](PRODUCTION_READINESS.md) |
| 依赖/安全审核 | [依赖安全扫描与例外模板](DEPENDENCY_SECURITY.md)、[CI](../.github/README.md) |
| 升级/回滚 | [四阶段升级](UPGRADE.md)、[CI](../.github/README.md) |
| 使用聊天界面 | [用户指南](USER_GUIDE.md) |

## 文档真值与更新规则

README 描述职责、操作边界、模板、验收与失败处理；JSON/JSONL 和源码是机器契约，不把 Markdown 再次喂给模型当运行真值。

一次变更至少同步：所属模块 README、根入口/链接、涉及的链路图和测试。在线流程图只维护根 README 一份；历史本地测试记录标注版本和日期，不覆盖为未执行的新结果。

## 发布记录模板

~~~text
版本/revision：
模型与embedding digest：
知识版本/Schema/依赖锁身份：
本地质量门禁及wheel摘要：
目标平台依赖/系统包扫描及未处置公告：
真实Test/检索指标与审核人：
TLS/只读权限/HTTPS/取消/压测：
镜像与部署记录：
备份恢复/回滚证据：
剩余阻塞项/批准人：
~~~

文档测试检查 README 覆盖、相对链接及关键流程节点；真实验收记录不可用夹具报告替代。
