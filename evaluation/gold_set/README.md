# Canonical Gold Set

## 职责与文件

- cases.jsonl：唯一候选/审批源，当前 38 条均 candidate，没有正式 approved。
- record.schema.json：由 GoldCase 生成的严格编辑器/审核平台合同。
- manifest.json：迁移来源与原文件摘要；其中 audit 是迁移时的历史快照，当前审计应运行 CLI。
- legacy/：原始 JSONL 的完整字节副本，只供追溯，不参与运行、训练或验收。

## 操作与验收

~~~powershell
uv run text2sql-gold-set check
uv run text2sql-gold-set check --enforce-release
~~~

第二条当前预期非零：100/80/20 的 Test 与各 30 条检索 held-out 缺口仍在。Prompt 与 Dev 有一组模板泄漏，必须在审核前重新分配用途，不能仅改问句、年份或模板名。

完整可解析的 negative SQL 也参加模板隔离，不能靠修改错误类型或 syntax_error 标签藏入 Test 同形 SQL。仅真实解析失败、明确标注语法错误且没有可识别查询主体的单查询反例豁免模板，问题和查询族仍隔离；完整 SQL 追加损坏后缀及不完整 AST 失败关闭，未批准反例不会进入学习视图。

## 扩展与失败处理

导入真实问题只创建 candidate；人工确认独立语义预期、只读基准及一致性快照，取得双人审核和真实执行证据后才改 approved。禁止模型自动批准、复制模板凑数或删除 candidate 掩盖覆盖缺口。

全部步骤、数据合同、数据库整改边界及不可变 generation 部署见 [Gold 治理手册](../../docs/GOLD_SET.md)。
