# mcp-open

基于 Python/FastAPI 的 MCP 服务注册与聚合网关。它把本地 stdio 服务和远端 HTTP/SSE MCP 服务统一注册、鉴权、聚合为一个 MCP 端点，同时提供 WebSocket 网关供小智 AI 服务调用工具，并提供可视化管理后台。

## 功能

- 注册三类上游 MCP 服务：本地 `stdio`、HTTP Streamable HTTP、HTTP SSE。
- 管理后台：服务增删改查、连接测试、工具列表、访问令牌、用户、设置、审计日志。
- 统一 MCP 端点：`POST /mcp` 提供 Streamable HTTP，`GET /sse` + `POST /messages/` 提供兼容旧客户端的 SSE。
- 工具聚合：工具名统一为 `服务名__工具名`，避免多服务冲突。
- 小智 WebSocket 网关：`/ws/xiaozhi`（别名 `/ws/mcp`），支持 JSON-RPC 与简化工具调用消息。
- 自定义权限：Token scopes + 服务白名单 + 可插拔 Python Authorizer。
- 零前端构建：管理页面为原生 HTML/CSS/JS，随服务一起启动。

## 快速开始

在项目根目录执行（PowerShell）：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

建议使用 Python 3.11～3.13。Python 3.15 较新，部分第三方包可能还没有预编译 wheel，会退回到源码编译并提示安装 Microsoft C++ Build Tools。

打开 `http://127.0.0.1:8765`，默认账号 `admin`，默认密码 `admin123`。首次登录后请在“用户管理”中修改密码，并通过环境变量设置强 `MCPOPEN_SECRET_KEY`。

## Docker 部署

项目根目录已提供 `Dockerfile`、`docker-compose.yml` 和 `.dockerignore`。

```powershell
docker compose up -d --build
docker compose logs -f
```

访问：

```text
http://127.0.0.1:8765
```

数据持久化在宿主机的 `./data` 目录，包含数据库和会话密钥。重建容器不会丢数据：

```powershell
docker compose down
docker compose up -d --build
```

建议在项目根目录创建 `.env`，覆盖默认密码和密钥：

```text
MCPOPEN_ADMIN_PASSWORD=请修改为强密码
MCPOPEN_SECRET_KEY=请修改为随机长字符串
XIAOZHI_MCP_TOKEN=你的小智云token
UPSTREAM_TOKEN=你的上游服务token
```

Compose 会自动读取 `.env` 做变量替换。

注意：容器内的“本地 stdio 服务”必须使用容器内可见的命令和路径。项目自带的示例可以这样注册：

```text
启动命令：python
启动参数：["/app/examples/echo_server.py"]
工作目录：/app
```

如果想接入 Windows 宿主机上的本地 stdio 服务，建议先把它暴露成 HTTP/SSE MCP 服务，或者把源码挂载进容器后再用容器路径启动。

## 注册 MCP 服务

### 本地 stdio

使用内置示例服务可直接测试：

- 服务名称：`echo`
- 传输方式：`本地 stdio`
- 启动命令：`python`
- 启动参数：`["examples/echo_server.py"]`
- 工作目录：项目根目录，例如 `G:\WORK\mcp-open`

注册后点击“测试”，应显示发现 3 个工具：`echo__echo`、`echo__add`、`echo__now`。

MCP 服务列表会显示每个服务发现的能力数量，点击“N 个能力”可展开查看具体工具名；工具名带描述时，鼠标悬停可看到说明。点击“刷新工具缓存”可重新探测所有服务。

### HTTP / SSE

- HTTP Streamable：填写上游 MCP 地址，例如 `http://127.0.0.1:9000/mcp`。
- SSE：填写上游 SSE 地址，例如 `http://127.0.0.1:9000/sse`，网关会自动读取 `endpoint` 事件并向其发送消息。
- 自定义请求头支持 `${ENV_VAR}` 占位符，例如 `Authorization: Bearer ${UPSTREAM_TOKEN}`。

## 访问令牌与权限

Token 可配置以下 scope：

| Scope | 用途 |
| --- | --- |
| `admin` | 管理 API 及绕过普通 scope 检查 |
| `mcp:read` | 列出聚合工具 |
| `mcp:call` | 调用聚合工具 |
| `ws:connect` | 连接小智 WebSocket 网关 |
| `servers:read` | 预留：读取服务信息 |
| `tokens:read` | 预留：读取令牌信息 |

在“访问令牌”页面创建或编辑 Token 时，可以直接按**服务名称**勾选这个 Token 可访问哪些 MCP 服务，也可以勾选“全部服务”。API 层面等价于 `allowed_services`：`*` 表示全部服务，`[1,2]` 表示只允许 ID 为 1、2 的服务。

工具调用会先解析工具所属服务，再执行 Token scope、服务白名单和自定义 Authorizer 检查。因此不同 Token 可以只看到、也只调用自己被授权的服务工具。Token 列表会把服务 ID 显示成可读的服务名称。

Token 明文只在创建或重新生成时显示一次，数据库只保存 SHA-256 哈希。列表里的“复制前缀”用于辨认 Token，不能用于认证。如果忘记明文，可在 Token 列表点击“重新生成”，旧 Token 会立即失效，新 Token 同样只显示一次。

### 自定义 Authorizer

在“系统设置 -> 自定义 Authorizer”填写 `module:Class` 路径，例如：

```text
examples.custom_authorizer:AdvancedAuthorizer
```

类需实现 `authorize(context)`，可同步或 `async`。返回 `False` 表示拒绝；返回 `None` 表示沿用内置判断。示例见 `examples/custom_authorizer.py`。

`context` 字段包括：`principal`、`action`、`service_id`、`service_name`、`tool_name`、`request_path`、`method`、`client_ip`。

## 接入统一 MCP

创建 Token 后，在 WorkBuddy 等 MCP 客户端中配置 Streamable HTTP：

```json
{
  "mcpServers": {
    "mcp-open": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "Authorization": "Bearer mcp-open_你的Token"
      }
    }
  }
}
```

使用 curl 验证：

```powershell
curl.exe -X POST http://127.0.0.1:8765/mcp `
  -H "Content-Type: application/json" `
  -H "Accept: application/json, text/event-stream" `
  -H "Authorization: Bearer mcp-open_你的Token" `
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

旧版 SSE 客户端可连接 `GET /sse?token=...`，网关会先下发 `endpoint` 事件，再接收 `POST /messages/?session_id=...` 的 JSON-RPC 请求。

## 小智 WebSocket 接入

连接地址：

```text
ws://127.0.0.1:8765/ws/xiaozhi?token=mcp-open_你的Token
```

Token 需要 `ws:connect` 权限；调用工具还需要 `mcp:call`。连接成功后网关会发送 `hello` 消息。

列出工具：

```json
{"type":"tools/list","id":"1"}
```

调用工具：

```json
{"type":"tool_call","id":"2","name":"echo__echo","arguments":{"text":"你好"}}
```

返回：

```json
{
  "type": "tool_result",
  "id": "2",
  "name": "echo__echo",
  "content": [{"type": "text", "text": "你好"}],
  "isError": false
}
```

网关同时接受标准 JSON-RPC 消息，例如 `tools/list`、`tools/call`、`initialize`、`ping`，方便复用同一套 MCP 语义。

若小智侧要求先认证再发送业务消息，也可以不传 query token，连接后先发送：

```json
{"type":"auth","token":"mcp-open_你的Token"}
```

## 小智云端主动桥接

上面是“小智主动连你”的方式。如果你拿到的是小智云提供的地址，例如 `wss://api.xiaozhi.me/mcp/?token=...`，方向相反：需要 mcp-open 主动连到小智云，并把聚合工具提供给小智。

配置位置：管理后台 → **小智云端** → 新增桥接。可以同时配置多个地址和多个小智助手，每个桥接独立连接、启停和重连。

建议不要把 token 明文写进数据库。先在本机设置环境变量：

```powershell
$env:XIAOZHI_MCP_TOKEN = "你的小智云token"
```

然后在后台填：

```text
wss://api.xiaozhi.me/mcp/?token=${XIAOZHI_MCP_TOKEN}
```

保存后网关会自动连接。概览页显示 `已连接数/总数`，管理页可查看每个桥接的连接状态、最近错误，并单独重连或停用。日志和列表里的 token 会被脱敏，不会完整打印。

旧版本“系统设置”中的单个桥接地址会自动迁移为一条名为“默认小智云端”的桥接记录。

注意：如果你曾在聊天、工单或截图中粘贴过完整 token，建议立即到 xiaozhi.me 控制台重新生成。桥接地址中的 token 属于敏感凭据，请按泄露处理。

## 主要端点

| 端点 | 说明 |
| --- | --- |
| `GET /` | 管理后台 |
| `POST /api/auth/login` | 后台登录 |
| `GET /api/servers` | 服务列表 |
| `POST /api/servers` | 注册服务 |
| `POST /api/servers/{id}/test` | 测试服务连接 |
| `GET /api/bridges` | 小智云端桥接列表 |
| `POST /api/bridges` | 新增桥接 |
| `POST /api/bridges/{id}/reconnect` | 重连指定桥接 |
| `GET /api/tokens` | Token 列表 |
| `POST /api/tokens/{id}/regenerate` | 重新生成 Token（旧值立即失效） |
| `POST /mcp` | 统一 MCP Streamable HTTP |
| `GET /sse`、`POST /messages/` | 统一 MCP 兼容 SSE |
| `GET /ws/xiaozhi` | 小智 WebSocket 网关 |
| `GET /health` | 健康检查 |

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCPOPEN_HOST` | `0.0.0.0` | 监听地址 |
| `MCPOPEN_PORT` | `8765` | 监听端口 |
| `MCPOPEN_ADMIN_USER` | `admin` | 初始管理员 |
| `MCPOPEN_ADMIN_PASSWORD` | `admin123` | 初始管理员密码 |
| `MCPOPEN_SECRET_KEY` | 自动生成并写入 `data/secret.key` | 会话签名密钥 |
| `MCPOPEN_DB` | `data/mcp-open.db` | SQLite 数据库路径 |
| `MCPOPEN_TIMEOUT` | `30` | 上游默认超时 |
| `MCPOPEN_AUTH_AUTHORIZER` | 空 | 默认自定义授权器 |
| `MCPOPEN_DEBUG` | `false` | 调试模式与自动重载 |

完整示例见 `.env.example`。本项目不自动读取 `.env` 文件，请在启动前设置到当前 shell。

## 测试

```powershell
pytest -q
```

## 目录结构

```text
app/
  main.py           FastAPI 路由与生命周期
  api.py            管理 API
  auth.py           Token、会话、自定义授权
  gateway.py        MCP JSON-RPC 聚合网关
  mcp_client.py     stdio/HTTP/SSE 上游客户端
  registry.py       服务注册与工具缓存
  xiaozhi.py        WebSocket 网关
  static/           管理后台页面
examples/           示例 MCP 服务与自定义授权器
tests/              基础测试
data/               运行时数据库与密钥
```
