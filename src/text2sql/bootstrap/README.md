# 配置注入与资源所有权

## 职责

container.py 持有独立配置、状态与惰性资源；wiring.py 将 Protocol 绑定到具体模型/存储/编译器。线程锁与缓存内复查保证并发首次初始化只有一个实例。

## 边界

惰性属性不在关闭时创建。Container、Service、Runtime各关闭自己拥有的资源，失败汇总并保留取消。禁止全局容器或跨实例 Schema 规则缓存。

## 操作与验收

所有命令从仓库根目录运行。

~~~powershell
uv run pytest -q tests/test_architecture_hardening.py tests/test_resource_lifecycle.py
~~~

## 扩展模板

新增资源注明 owned/borrowed、构造失败清理和并发 getter 合同；工厂显式传配置，不重新隐式 load_settings。

每次变更记录：输入/输出、权限与资源所有者、失败/取消语义、正反例、受影响知识/评测/发布身份。

## 失败处理

首次初始化失败保持未就绪，修依赖后按运维策略重启；不复用半成品。

[源码总览](../../README.md) · [专题说明](../../../ARCHITECTURE.md)
