# HTTP 与会话边界

## 职责

server.py 提供 /ask、仅生成、候选/管理员反馈、健康与报告；auth.py 管理 Key、HttpOnly 会话和同源写请求；schemas.py/errors.py 固定协议。

## 边界

不存业务规则、不直接拼 SQL。异步路由中的同步初始化/证据读取进入 worker；管理员 Cookie 也执行同源校验。反向代理必须只信任指定代理并保留正确 Host/HTTPS 信息。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run pytest -q tests/test_api_e2e.py tests/test_auth_security.py
~~~

## 扩展模板

新增路由声明权限、输入上限、outcome、缓存/同源行为及测试；不得把数据库原始异常发给用户。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

401/403 查凭据和 Origin；503 查发布/依赖；用 request_id 对照日志。

[源码总览](../../README.md) · [专题说明](../../../docs/OPERATIONS.md)
