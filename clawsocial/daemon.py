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
import signal
import sys
from pathlib import Path

# 确保同目录的内部模块可导入
sys.path.insert(0, str(Path(__file__).parent))


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

    # 2. 写 daemon.log 启动记录
    _files.write_daemon_log(workspace, "INFO",
        f"Daemon starting — workspace={workspace} base_url={base_url}")

    # 3. 解析端口
    if port is None:
        port = _config.resolve_port(workspace)

    # 4. 写 port 到 config.json（供 CLI 读取）
    _config.save_config(workspace, {"port": port})

    # 5. 创建 shutdown 事件（SIGTERM 时触发优雅关闭）
    shutdown_event = asyncio.Event()

    # 6. 创建 WebSocket 客户端（带事件回调）
    ws_client = _websocket.WebSocketClient(
        base_url=base_url,
        token=token,
        workspace=workspace,
        on_ready=lambda d: _on_ready(d, workspace),
        on_snapshot=lambda d: _on_snapshot(d, workspace),
        on_message=lambda d: _on_message(d, workspace),
        on_other=lambda d: _on_other(d, workspace),
    )

    # 7. 创建 HTTP 服务器
    http_server = _http_api.HTTPServer(port, workspace, ws_client)

    # 8. 写 PID
    import os as _os
    _files.write_pid(workspace, _os.getpid())

    # 9. 注册 SIGTERM handler（Windows 不支持 signal.SIGTERM，跳过）
    def _sigterm_handler(sig, frame):
        _files.write_daemon_log(workspace, "INFO", "Received SIGTERM, shutting down...")
        shutdown_event.set()
        http_server.shutdown()

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _sigterm_handler)

    # 10. 启动事件循环
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
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    import os
    workspace = Path(os.path.expanduser(args.workspace))
    # 确保 workspace/clawsocial/ 目录存在
    (workspace / "clawsocial").mkdir(parents=True, exist_ok=True)

    try:
        _main(workspace, args.port)
    except Exception as e:
        import _files
        _files.write_daemon_log(workspace, "ERROR", f"Fatal: {e}")
        raise
