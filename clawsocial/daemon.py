#!/usr/bin/env python3
# scripts/clawsocial_daemon.py
"""
clawsocial daemon 主程序。

Usage:
    python clawsocial_daemon.py --workspace <path> [--port PORT]
"""
from __future__ import annotations

import argparse
import asyncio
import socket
import signal
import sys
from pathlib import Path

# 确保同目录的内部模块可导入
sys.path.insert(0, str(Path(__file__).parent))

LOCAL_HOST = "127.0.0.1"
PORT_RANGE_START = 18791
PORT_RANGE_END = 65535


def _try_bind_port(workspace: Path, preferred_port: int, _files) -> int:
    """
    尝试绑定 preferred_port，若被占用则自动从 18791 开始找空闲端口。
    绑定成功前就写入 .port 文件，让 CLI 能立即读到。
    """
    # 先尝试首选端口
    port = preferred_port
    while port <= PORT_RANGE_END:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((LOCAL_HOST, port))
            sock.close()
            _files.write_daemon_log(workspace, "INFO",
                f"Port {port} available (preferred={preferred_port})")
            return port
        except OSError:
            # 端口被占用，尝试下一个
            _files.write_daemon_log(workspace, "INFO",
                f"Port {port} in use, trying next...")
            port += 1

    # 找不到可用端口（几乎不可能）
    _files.write_daemon_log(workspace, "ERROR",
        f"No available port found between {preferred_port}-{PORT_RANGE_END}")
    raise RuntimeError(f"No available port found between {preferred_port}-{PORT_RANGE_END}")


def _main(workspace: Path, port: int | None) -> None:
    """同步入口，供 __main__.py 或直接调用"""
    import _config
    import _files
    import _websocket
    import _http_api

    # 1. 加载配置
    cfg = _config.load_config(workspace)
    base_url = cfg["base_url"]
    token = cfg["token"]

    # 2. 解析端口：优先用 --port，其次 config.json，最后自动分配
    if port is None:
        port = _config.resolve_port(workspace)

    # 3. 尝试绑定端口，若失败则自动找空闲端口
    actual_port = _try_bind_port(workspace, port, _files)

    # 4. 写 daemon.log 启动记录（此时 actual_port 已确定）
    _files.write_daemon_log(workspace, "INFO",
        f"Daemon starting — workspace={workspace} port={actual_port} base_url={base_url}")

    # 5. 写 .port 文件（CLI 会立即读取）
    _files.write_port_file(workspace, actual_port)

    # 6. 写 port 到 config.json（供后续 start 读取默认端口）
    _config.save_config(workspace, {"port": actual_port})

    # 7. 创建 shutdown 事件（SIGTERM 时触发优雅关闭）
    shutdown_event = asyncio.Event()

    # 8. 创建 WebSocket 客户端（带事件回调）
    ws_client = _websocket.WebSocketClient(
        base_url=base_url,
        token=token,
        workspace=workspace,
        on_ready=lambda d: _on_ready(d, workspace),
        on_snapshot=lambda d: _on_snapshot(d, workspace),
        on_message=lambda d: _on_message(d, workspace),
        on_other=lambda d: _on_other(d, workspace),
    )

    # 9. 创建 HTTP 服务器（使用 actual_port）
    http_server = _http_api.HTTPServer(actual_port, workspace, ws_client)

    # 10. 写 PID
    import os as _os
    _files.write_pid(workspace, _os.getpid())

    # 11. 注册 SIGTERM handler（Windows 不支持 signal.SIGTERM，跳过）
    def _sigterm_handler(sig, frame):
        _files.write_daemon_log(workspace, "INFO", "Received SIGTERM, shutting down...")
        shutdown_event.set()
        http_server.shutdown()

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _sigterm_handler)

    # 12. 启动事件循环
    async def _run() -> None:
        await asyncio.gather(
            http_server.run(),
            ws_client.run(shutdown_event),
        )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        _files.write_daemon_log(workspace, "INFO", "Daemon stopped")
        _files.remove_pid(workspace)


# ── Event handlers ──────────────────────────────────────────

def _on_ready(data: dict, workspace: Path) -> None:
    import _config
    import _files
    me = data.get("me", {})
    radius = data.get("radius", 300)
    user_id = me.get("user_id")
    _files.write_daemon_log(
        workspace, "INFO",
        f"Ready — user_id={user_id} radius={radius}"
    )
    _config.save_config(workspace, {"user_id": user_id})
    state = _files.read_world_state(workspace)
    state["me"] = me
    state["radius"] = radius
    _files.write_world_state(workspace, state)


def _on_snapshot(data: dict, workspace: Path) -> None:
    import _files
    _files.write_world_state(workspace, data)
    users = data.get("users", [])
    me = data.get("me", {})
    ts = data.get("ts", "")
    for u in users:
        uid = u.get("user_id")
        if uid and str(uid) != str(me.get("user_id")):
            _files.append_unread(workspace, {
                "type": "encounter",
                "user_id": uid,
                "user_name": u.get("name", ""),
                "x": u.get("x"),
                "y": u.get("y"),
                "ts": ts,
            })


def _on_message(data: dict, workspace: Path) -> None:
    import _files
    _files.append_unread(workspace, data)


def _on_other(data: dict, workspace: Path) -> None:
    import _files
    _files.append_unread(workspace, data)


# ── CLI entry ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="clawsocial_daemon")
    parser.add_argument("--workspace", required=True, type=str)
    args = parser.parse_args()

    import os
    workspace = Path(os.path.expanduser(args.workspace))
    # 确保 workspace/clawsocial/ 目录存在
    (workspace / "clawsocial").mkdir(parents=True, exist_ok=True)

    try:
        _main(workspace, None)
    except Exception as e:
        import _files
        _files.write_daemon_log(workspace, "ERROR", f"Fatal: {e}")
        raise
