# 配置、异常与发布身份

## 职责

http_policy.py 统一本地 HTTP Ollama 的回环直连策略；不改变远程或 HTTPS 的代理/证书环境行为。

config.py 解析显式 Settings；production.py 强制生产安全/质量下限；build_info.py 绑定代码、Prompt、锁文件、模型、访问策略和适用的全部运行依赖；logging.py/exceptions.py 固定基础边界。

## 边界

无全局 Settings 单例。身份不包含秘密值或安装目录；运行依赖版本必须匹配构建锁，不能只核对少数库。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run pytest -q tests/test_production_readiness.py tests/test_dependency_identity.py
~~~

## 扩展模板

新增环境变量同步 .env.example 与文档；影响查询/权限的配置进入身份；新增直接依赖更新 uv/运行/build 锁。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

无效 APP_ENV 不能当开发环境；缺失/漂移库存或弱生产配置保持 fail-closed。

[源码总览](../../README.md) · [专题说明](../../../docs/PRODUCTION_READINESS.md)
