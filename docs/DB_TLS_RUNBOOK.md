# SQL Server TLS 与只读账号整改清单

当前客户端已经安装 Microsoft ODBC Driver 18。若 `text2sql-db-check` 仍返回
`08001` 和 TLS 安全包错误，需要由 SQL Server 管理员完成服务器侧整改，项目代码
不能也不应通过关闭加密规避。

## 服务器侧

1. 为 SQL Server 配置受客户端信任的证书；证书必须包含 Server Authentication
   用途，CN/SAN 与客户端连接使用的 DNS 名称一致，并允许 SQL Server 服务账号读取
   私钥。
2. 启用 TLS 1.2 或更高版本，停用已淘汰协议，并确认操作系统与 SQL Server 补丁级别
   支持该协议。
3. 在 SQL Server Configuration Manager 中绑定证书；如启用 Force Encryption，
   完成后重启 SQL Server 服务。
4. 检查 SQL Server Error Log 和 Windows Schannel 事件，确认没有证书、协议或密码套件
   协商错误。

## 客户端侧

连接串使用证书中的 DNS 名称，不使用 IP 地址绕过主机名校验：

```dotenv
MSSQL_CONN_STR=Driver={ODBC Driver 18 for SQL Server};Server=DB_FQDN;Database=Text2SQL;UID=READ_ONLY_USER;PWD=SECRET;Encrypt=yes;TrustServerCertificate=no;
```

生产账号只授予业务白名单表的 `SELECT` 和必要的 `VIEW DEFINITION`，不得加入
`db_owner`、`db_ddladmin` 或 `db_datawriter`，也不得拥有 `INSERT`、`UPDATE`、
`DELETE`、`ALTER`、`CONTROL`、`TAKE OWNERSHIP` 等权限。

## 验收

```powershell
.\.venv\Scripts\text2sql-db-check.exe
```

只有报告同时满足以下条件才继续训练：

- `success=true`；
- Driver 为 ODBC Driver 18；
- `connection_encrypted=true`；
- `read_only_principal=true`；
- `dangerous_permissions=[]`。
