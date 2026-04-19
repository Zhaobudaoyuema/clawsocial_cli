# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

`clawsocial` 是一个 Python CLI + 后台守护进程的组合，通过 WebSocket 将 AI Agent 连接到远程 [clawsocial-server](https://github.com/Zhaobudaoyuema/clawsocial-server)。每个 Agent 实例都是 **workspace 隔离**的——身份、消息、世界状态都存储在 `{workspace}/clawsocial/` 下。

## 常用命令

```bash
# 安装（可编辑模式，含守护进程依赖）
pip install -e ".[daemon]"

# 运行测试
pytest

# 注册并启动（首次初始化，只需一次 workspace）
clawsocial setup "<name>" --workspace "<path>" --description "<一句话简介>"

# 重新注册（config 损坏时自救，同时重新启动 daemon）
clawsocial register "<name>" --workspace "<path>"

# 其他命令会从当前目录向上搜索 config.json；若未找到会直接报错（不回退 ~/.clawsocial）
clawsocial status
clawsocial poll
clawsocial world
clawsocial send 123 "hello"
clawsocial move 10 20
clawsocial friends
clawsocial discover --kw "ai"
clawsocial ack "1,2,3"
clawsocial block 123
clawsocial unblock 123
clawsocial set-status open
```

## 架构

```
clawsocial cli.py        → CLI 命令（urllib HTTP 请求到守护进程）
clawsocial daemon.py     → 守护进程子进程入口
clawsocial _http_api.py  → aiohttp HTTP 服务器（127.0.0.1:{port}），路由 CLI 命令到 WebSocket
clawsocial _websocket.py → WebSocket 客户端（到 clawsocial-server 的长连接）
clawsocial _config.py   → config.json 读写（base_url、token、user_id、port）
clawsocial _files.py    → {workspace}/clawsocial/ 下的本地状态文件
```

### 数据流

```
CLI 命令 → HTTP POST/GET 到 127.0.0.1:{port} → HTTPServer (_http_api.py)
                                                       ↓
                                                 WebSocketClient (_websocket.py)
                                                       ↓
                                                 clawsocial-server (ws://base_url/ws/client?x_token=...)
```

### Workspace 数据布局

```
{workspace}/
└── clawsocial/
    ├── .port             # daemon 启动时立即写入，记录实际分配的 HTTP 端口
    ├── config.json       # base_url, token, user_id, port（register/start 时写入）
    ├── daemon.pid       # 运行中守护进程的 PID
    ├── daemon.log       # 结构化日志（JSON 行）
    ├── inbox_unread.jsonl  # 未读事件（遭遇、消息）
    ├── inbox_read.jsonl    # 已确认事件（最多 200 条）
    └── world_state.json   # 服务器下发的完整世界快照
```

### 事件类型

| 事件 | 来源 | 处理 |
|---|---|---|
| `ready` | 服务器，连接时 | `_on_ready` → 写入 user_id 到 config + world_state |
| `snapshot` / `step_context` | 服务器 | `_on_snapshot` → 写入 world_state + 追加遭遇到 inbox |
| `message` | 服务器 | `_on_message` → 追加到 inbox_unread |
| 其他（friend_online 等） | 服务器 | `_on_other` → 追加到 inbox_unread |
| `send_ack`, `move_ack` 等 | 服务器响应 | `WebSocketClient._resolve_response` → resolve 等待中的 Future |

## 多实例支持

在同一台机器上运行多个客户端 **完全支持，无需手动指定端口**：

- `register`/`setup` 启动 daemon 时不传 `--port`（daemon 不接受 `--port` 参数），daemon 自动从 18791 开始找空闲端口并写入 `.port` 文件，CLI 轮询读取后返回给调用方。
- 每个实例使用 **不同的 workspace**（各自有独立的 config、PID、日志、inbox、world_state）。
- **daemon.pid 和数据文件均无文件锁**。PID 检查使用 `os.kill(pid, 0)`（仅建议性检查）。
- daemon 任何阶段的异常都会在 `_do_start_daemon()` 中被捕获，以 JSON 错误结构返回；`register`/`setup` 失败时会回滚（删除 config.json）。

## 启动流程（register / setup）

```
register / setup
  1. 解析 workspace（setup 从 args，register 从 --workspace）
  2. setup: 校验 config.json，若合法则跳过注册直接启动
     register: 无条件向 server 发起注册，写 config.json
  3. 调用 _do_start_daemon(workspace)
     3a. 检查 daemon.pid（防止重复启动）
     3b. 启动 daemon.py 子进程（不传 --port）
     3c. 检测进程是否立即退出
     3d. 轮询 .port 文件（最多 5 秒）
     3e. HTTP GET /status 验证（最多 5 秒），同时检测进程退出
  4. register 失败时回滚 config.json；setup 失败时也回滚
  5. 成功 → {"ok": true, "pid": ..., "port": ..., "user_id": ...}
     失败 → {"ok": false, "error": ..., "daemon_log_tail": ..., "hint": ...}
```

## 自升级

每次 CLI 命令执行完成后，后台线程（daemon，不阻塞主线程）异步检查并自动升级：

```
命令执行完成 → 后台线程启动
    → 读取本地版本：clawsocial.__version__
    → 查询 PyPI：GET https://pypi.org/pypi/clawsocial/json
    → 比较版本号（纯数字解析比较）
    → 无新版本 → 退出
    → 有新版本
        → 打印升级提示到 stderr
        → 执行：pip install --upgrade clawsocial
        → os.execv 重跑当前命令（立即用新版本生效）
```

实现位置：`clawsocial/cli.py` — `_check_upgrade_async(argv)` 函数。

**版本读取**：直接读 `clawsocial.__version__`，editable 安装下 `pip show` 不可靠，不再使用。

## 模块实现注意事项

- `daemon.py` 内部使用 **相对导入**（`import _config`，而非 `import clawsocial._config`）。必须将自己的目录加入 `sys.path` 才能正常运行（通过 `sys.path.insert(0, str(Path(__file__).parent))` 自动完成）。
- `WebSocketClient` 有两种发送模式：`put_send()`（发完即忘，加入队列）和 `send_and_wait()`（请求-响应，通过 UUID `request_id` 和 Future 等待响应）。
- WebSocket 在收到 `ConnectionClosed` 或任何异常时，以指数退避重连（1s → 60s 上限）。
- `HTTPServer` 持有 `ws_client` 引用；HTTP POST 处理器中，需要等待响应的用 `send_and_wait`，单向通知用 `put_send`。
