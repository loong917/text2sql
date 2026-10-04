# 依赖安全扫描与风险处置

## 责任与边界

依赖锁、版本一致性和功能回归不能替代漏洞扫描。维护者负责修复与回归，安全负责人判断真实可达性并批准有期限的例外；程序不能自动替人接受风险。

扫描只发送公共包名和版本到漏洞服务，不发送 `.env`、SQL、数据库连接串或业务数据。扫描覆盖当前 Python/平台适用的运行锁；Windows 报告不能替代 Linux 镜像及系统包扫描。

## 2026-10-02 / 0.6.2 处置记录

最初扫描有 11 条发现，按公告 ID 去重为 9 项。已升级 `urllib3 2.7.0 → 2.8.0`、`oauthlib 3.3.1 → 4.0.0`，并在 uv 中添加安全下限约束，重新导出带哈希的 Docker 运行锁。

复扫仍有 7 条发现，去重后为 Chroma 的 4 项及 Vanna 的 1 项。重复条目不是新增漏洞；原始 JSON 保留，不改写成“通过”。截至本次查询，以下公告没有列出修复版本。

| 依赖/公告 | 涉及路径 | 本项目边界与待办 |
| --- | --- | --- |
| chromadb 1.5.9 / [CVE-2026-45829](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c)、[CVE-2026-45833](https://github.com/advisories/GHSA-36p7-vc44-83pf) | Chroma HTTP 集合接口、远程模型代码执行 | 当前是本地 PersistentClient，显式 Ollama embedding；不得部署 Chroma HTTP 服务或接受用户配置 embedding/model repository。安全人员复核目标镜像和数据权限 |
| chromadb 1.5.9 / [CVE-2026-45831](https://github.com/advisories/GHSA-xph7-9rjv-w5fr)、[CVE-2026-45830](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr) | 服务端租户/集合授权 | 当前没有 Chroma 多租户授权服务；若改为远程服务或多租户必须重新威胁建模，不能复用本次不可达判断 |
| vanna 2.0.2 / [CVE-2026-4229](https://github.com/advisories/GHSA-6mj8-jmp2-g8q7) | legacy/google/bigquery_vector.remove_training_data | 当前只用现代 MSSQL/Chroma 适配器，禁止引入 legacy、BigQuery 删除接口；继续跟踪上游修复或通过 Protocol 替换适配器 |

本项目可达性是对当前静态调用边界的判断，不是漏洞已经修复的证明，也不是正式风险豁免。当前没有获批例外，因此依赖安全门禁保持失败，生产不放行。

修复依据：[urllib3 维护者公告](https://github.com/urllib3/urllib3/security/advisories/GHSA-vxq7-64xx-v4gw)、[OAuthlib 公告](https://github.com/advisories/GHSA-xpv3-w29h-x7cv)。

## 可复用操作

~~~powershell
uv lock --check
uv pip check
uvx --from pip-audit==2.10.1 pip-audit --disable-pip --no-deps -r requirements.lock --format json --output dependency-audit.json
~~~

最后一条命令发现漏洞返回非零，这是预期门禁行为。网络或漏洞服务失败也不能当作零漏洞。CI 在 Ubuntu 重新执行并上传原始报告，没有 `--ignore-vuln` 或 `continue-on-error`。

修改依赖后依次执行：定向升级 → 同步锁文件 → 本地安装 → 回归 → 构建 wheel/镜像 → 目标环境扫描 → 重新评测与生成发布证据。依赖/Python 身份变化后旧评测与发布清单失效。

## 风险例外模板（不等于批准）

~~~text
公告 ID / 依赖版本 / 目标镜像 digest：
可达性证据（源码、运行配置、网络暴露、权限）：
影响与补偿措施：
上游跟踪链接 / 替代方案：
责任人 / 安全审批人 / 到期日 / 复查触发条件：
状态：pending_review
~~~

## 验收与失败处理

默认要求目标运行依赖和系统包扫描无未处置发现。确需例外时，由负责人单独审批、限定部署范围和有效期，再显式调整发布流程；不得在业务测试中默默屏蔽公告。当前仓库未添加任何豁免。

Chroma HTTP 服务、Vanna legacy、远程模型代码执行、多租户以及不可信知识文件均在当前部署能力边界之外。新增任一能力必须重新审计、加集成/安全反例、更新本文件和运行手册。
