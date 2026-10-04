# text2sql 包导航

## 职责

本目录是 wheel 中的正式包，导入名 text2sql。源码、静态资源和 Prompt 参与发布身份；安装路径和数据目录不参与代码身份。

| 模块 | 职责与维护入口 |
| --- | --- |
| [api](api/README.md) | HTTP、会话、鉴权、健康检查 |
| [application](application/README.md) | 查询/反馈用例、上下文与 Protocol |
| [domain](domain/README.md) | QueryPlan、日期、编译、Scope/AST/语义 |
| [infrastructure](infrastructure/README.md) | SQL/Ollama/Chroma/SQLite 适配器 |
| [bootstrap](bootstrap/README.md) | 配置注入、资源所有权、惰性装配 |
| [core](core/README.md) | 配置、异常、日志、运行依赖/发布身份 |
| [knowledge](knowledge/README.md) | 结构化加载、快照、原子产物治理 |
| [retrieval](retrieval/README.md) | 表卡、嵌入、校准与选择策略 |
| [training](training/README.md) | 候选构建与 Dev 质量门禁 |
| [evaluation](evaluation/README.md) | 完整语义/结果证据与冻结评测 |
| [release](release/README.md) | 上线决策与运行时失效门禁 |
| [cli](cli/README.md) | 可安装 Schema 导出入口 |
| static、templates | 聊天界面，仍属于代码与安全边界 |

## 扩展与验收

新增模块必须声明依赖方向、资源所有者和对应测试，不创建空兼容包。前端改动执行 node --check；运行资源配置通过 APP_DATA_DIR，禁止写入安装目录。

参见 [源码规则](../README.md) 与 [生成流程](../../docs/TEXT2SQL_FLOW.md)。
