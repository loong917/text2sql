# CI 与依赖维护资产

## 职责

workflows/ci.yml 是源码/分发/容器质量门禁，不负责生产自动放行。dependabot.yml 提供依赖更新入口，更新仍需审核与完整回归。

质量矩阵覆盖 Python 3.12/3.13；检查 uv.lock 与 requirements.lock 同步、Ruff、Mypy、pytest、源码编译、wheel 安装及容器启动。容器烟测故意没有有效活动产物，必须 livez=200、readyz=503，不能误写为生产成功。

另运行 canonical Gold 只读审计，显示候选/批准、覆盖缺口与模板泄漏；允许仓库保留 pending 草稿并不意味着允许生产放行。目标部署须另执行 Gold --enforce-release、真实 Test 与 release-check。

Actions 固定到已核对的完整提交 SHA，令牌只授予 contents:read，checkout 不保留凭据。质量任务使用 Node 24 检查前端语法，并保存 wheel 与 SHA256SUMS（14 天）；artifact 不是生产自动发布。维护依赖更新时显式复核固定 SHA，参考 [GitHub 官方安全规范](https://docs.github.com/en/actions/reference/security/secure-use)。

dependency-security 独立任务使用固定 pip-audit 2.10.1 扫描目标平台运行锁，原始 JSON 即使失败也上传。没有忽略名单或失败放行；当前 Chroma/Vanna 未处置风险会使此任务失败，这是明确阻断而非 CI 脚本故障。处置、可达性与审批模板见 [依赖安全](../docs/DEPENDENCY_SECURITY.md)。

## 本地复现与验收

~~~powershell
uv lock --check
uv export --frozen --no-dev --no-emit-project --output-file requirements.lock
git diff --exit-code -- requirements.lock
uv sync --frozen --extra dev
uv run pytest -q
~~~

导出命令写本地锁文件，不访问业务库。正式 wheel 检查见 [测试资产](../tests/README.md)；容器构建需要本地 Docker 与依赖网络。

## 变更模板

~~~text
变更依赖/Action/镜像及原因：
锁文件是否更新：
Python双版本/本地质量结果：
wheel与容器构建摘要：
真实环境是否复验：
安全/弃用/兼容风险：
回滚版本与负责人：
~~~

## 失败与发布边界

任一门禁失败先修复，不能去掉检查。远程 CI 只有真实 run 链接可证明通过；本机源码测试不等于 GitHub 执行成功。生产另外需要冻结 Test、TLS/权限、模型 digest、镜像摘要、压力/取消、恢复/回滚和人工批准。
