# 生产决策与运行门禁

## 职责

readiness.py 结合真实依赖、审核源、显式注册版本与 Test 检查放行条件；仅 --promote 成功时写版本审批并原子切 ACTIVE。runtime_gate.py 在启动/就绪/请求前核验 ACTIVE 所指版本；evidence.py 用同一字节计算摘要和解析，并在发布前复核。

## 边界

release-check 不自动修配置、不自动批准业务数据、不自动部署。默认检查 ACTIVE，--artifact VERSION 检查注册候选但不晋升；必须另加 --promote。旧版本发布证据不能直接复用。运行时版本被固定；活动指针变化需同一套新证据与重启。

审批写入 VERSION/approved_release.json；全部门禁通过后才原子切 ACTIVE，指针包含 release_manifest_path，审批记录包含 test_report_path。Test 默认位于 VERSION/test_report.json，runtime 只读并强校验该版本路径，不以 global EVAL_TEST_REPORT_PATH 或独立全局 ready 文件批准其他版本。training_manifest.outputs.collection_evidence v2 在评测前后、发布和运行时复核记录与检索配置；只更改集合配置也拒绝旧 Test，旧 v1 不兼容。生产快照重复验证知识审核并与同代 generation 匹配。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run pytest -q tests/test_production_readiness.py tests/test_release_evidence.py tests/test_runtime_gate.py
uv run text2sql-release-check
## 从 KNOWLEDGE_ARTIFACT_DIR/candidate_artifact.json 读取实际 version
$candidateVersion = 'VERSION_FROM_CANDIDATE_ARTIFACT'
uv run text2sql-release-check --artifact $candidateVersion
uv run text2sql-release-check --artifact $candidateVersion --promote
~~~

显式候选必须先用 text2sql-evaluate --artifact VERSION --split test --enforce-gate 取得版本内 Test 报告。检查通过不等于已晋升；晋升成功不等于已部署或完成目标环境验收。当前真实 Gold 38 candidate / 0 approved，正式发布条件尚未满足。

## 扩展模板

新增影响执行/安全的证据同步身份、冻结报告与运行门禁；测试缺失、变更、ABA、类型和少样本绕过。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

失败不切 ACTIVE；普通检查失败也不删除旧审批。晋升最后切换前可能留下未激活版本的审批文件，这不代表上线；保留诊断并重建独立候选，不原地覆写或手工补 ready。运行证据失效使就绪拒绝。不得编辑 status、复制 ready 文件或绕过模型/TLS/真实数据。保留旧 ACTIVE 不证明它在新配置下仍有效，运行时仍须复验。

[源码总览](../../README.md) · [专题说明](../../../docs/PRODUCTION_READINESS.md)
