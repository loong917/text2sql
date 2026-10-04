# Python 源码维护资产

## 职责与边界

src/ 只保存可安装源码，唯一运行包是 [text2sql](text2sql/README.md)。从仓库根目录运行工具；禁止 src.* 导入、sys.path 补丁、隐藏全局 Settings 和直接在 Domain 构造外部客户端。

依赖方向：API → Application → Domain；Infrastructure 实现 Application 端口，Bootstrap 注入配置。训练、评测和发布是受控组合入口，不是被在线 Domain 反向导入的模块。

## 新功能接入模板

1. 在 Domain 明确计划、支持范围、错误/澄清边界。
2. 在 Application 定义用例与类型端口，资源必须有唯一所有者。
3. 在 Infrastructure 实现适配器，异常不泄露凭据，取消不能提前释放运行中的许可。
4. 在 Bootstrap 注入配置与依赖，在 API 定义稳定请求/响应。
5. 补架构、契约、反例、集成/HTTP 测试和对应 README；需要真实依赖的验收单独记录。

## 操作与验收

~~~powershell
uv run ruff check src tests setup.py
uv run mypy src
uv run pytest -q tests/test_architecture.py tests/test_port_contracts.py
~~~

源码改动会使发布代码身份失效，必须重训/评测/发布适用的新证据。不要通过修改报告使旧版本继续显示 ready。

详见 [架构](../ARCHITECTURE.md)、[在线链路](../docs/TEXT2SQL_FLOW.md)、[测试资产](../tests/README.md)。
