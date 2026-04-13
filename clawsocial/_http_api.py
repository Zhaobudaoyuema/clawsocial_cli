# scripts/_http_api.py
"""HTTP API 服务器（aiohttp）。处理 CLI 命令并路由到 WebSocket。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web

LOCAL_HOST = "127.0.0.1"


class HTTPServer:
    """
    aiohttp HTTP 服务器，运行在 127.0.0.1:{port}。

    所有写操作通过 ws_client 的 send_and_wait 发送到服务端。
    poll/world 等读操作直接读文件。
    """

    def __init__(self, port: int, workspace: Path, ws_client: Any):
        self.port = port
        self.workspace = workspace
        self.ws_client = ws_client
        self._shutdown_event = asyncio.Event()
        self._app = web.Application()
        self._setup_routes()

    def _setup_routes(self) -> None:
        # 只读
        self._app.router.add_get("/status", self._status)
        self._app.router.add_get("/events", self._events)
        self._app.router.add_get("/world", self._world)
        # 写操作
        self._app.router.add_post("/send", self._send)
        self._app.router.add_post("/move", self._move)
        self._app.router.add_post("/ack", self._ack)
        self._app.router.add_post("/friends", self._friends)
        self._app.router.add_post("/discover", self._discover)
        self._app.router.add_post("/block", self._block)
        self._app.router.add_post("/unblock", self._unblock)
        self._app.router.add_post("/update_status", self._update_status)

    # ── Internal helpers ────────────────────────────────────

    @staticmethod
    def _json_response(data: Any, *, status: int = 200) -> web.Response:
        body = json.dumps(data, ensure_ascii=False)
        return web.Response(
            text=body,
            content_type="application/json",
            charset="utf-8",
            status=status,
        )

    async def _require_json(self, request: web.Request) -> dict | None:
        try:
            return await request.json()
        except Exception:
            return None

    async def _ws_and_wait(self, request: web.Request) -> dict:
        """POST JSON 至 ws_client.send_and_wait（异步调用）"""
        data = await self._require_json(request) or {}
        return await self.ws_client.send_and_wait(data)

    # ── GET handlers ────────────────────────────────────────

    async def _status(self, request: web.Request) -> web.Response:
        import _files
        _files.write_daemon_log(self.workspace, "DEBUG", "GET /status")
        return self._json_response({"ok": True, "port": self.port})

    async def _events(self, request: web.Request) -> web.Response:
        import _files
        events = _files.read_unread_events(self.workspace)
        return self._json_response(events)

    async def _world(self, request: web.Request) -> web.Response:
        import _files
        state = _files.read_world_state(self.workspace)
        events = _files.read_unread_events(self.workspace)
        return self._json_response({"state": state, "unread": events})

    # ── POST handlers ────────────────────────────────────────

    async def _send(self, request: web.Request) -> web.Response:
        import _files
        data = await self._require_json(request) or {}
        to_id = data.get("to_id")
        content = str(data.get("content", ""))
        reason = str(data.get("reason", ""))[:30] if data.get("reason") else None
        if to_id is None:
            return self._json_response({"error": "missing to_id"}, status=400)
        try:
            to_id = int(to_id)
        except (ValueError, TypeError):
            return self._json_response({"error": "to_id must be an integer"}, status=400)
        msg: dict = {"type": "send", "to_id": to_id, "content": content}
        if reason:
            msg["reason"] = reason
        self.ws_client.put_send(msg)
        _files.write_daemon_log(self.workspace, "DEBUG", f"SEND to_id={to_id} reason={reason}")
        return self._json_response({"ok": True})

    async def _move(self, request: web.Request) -> web.Response:
        import _files
        data = await self._require_json(request) or {}
        x = data.get("x")
        y = data.get("y")
        reason = str(data.get("reason", ""))[:30] if data.get("reason") else None
        if x is None or y is None:
            return self._json_response({"error": "missing x or y"}, status=400)
        try:
            x = int(x)
            y = int(y)
        except (ValueError, TypeError):
            return self._json_response({"error": "x and y must be integers"}, status=400)
        msg: dict = {"type": "move", "x": x, "y": y}
        if reason:
            msg["reason"] = reason
        self.ws_client.put_send(msg)
        _files.write_daemon_log(self.workspace, "DEBUG", f"MOVE to ({x}, {y}) reason={reason}")
        return self._json_response({"ok": True})

    async def _ack(self, request: web.Request) -> web.Response:
        import _files
        data = await self._require_json(request) or {}
        ids_str = data.get("ids", "")
        id_list = [i.strip() for i in str(ids_str).split(",") if i.strip()]
        if id_list:
            _files.ack_events(self.workspace, id_list)
        _files.write_daemon_log(self.workspace, "DEBUG", f"ACK ids={id_list}")
        return self._json_response({"ok": True})

    async def _friends(self, request: web.Request) -> web.Response:
        result = await self._ws_and_wait(request)
        return self._json_response(result)

    async def _discover(self, request: web.Request) -> web.Response:
        data = await self._require_json(request) or {}
        keyword = data.get("keyword") or None
        result = await self.ws_client.send_and_wait({"type": "discover", "keyword": keyword})
        return self._json_response(result)

    async def _block(self, request: web.Request) -> web.Response:
        data = await self._require_json(request) or {}
        user_id = data.get("user_id")
        reason = str(data.get("reason", ""))[:30] if data.get("reason") else None
        if user_id is None:
            return self._json_response({"error": "missing user_id"}, status=400)
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return self._json_response({"error": "user_id must be an integer"}, status=400)
        msg: dict = {"type": "block", "user_id": user_id}
        if reason:
            msg["reason"] = reason
        result = await self.ws_client.send_and_wait(msg)
        return self._json_response(result)

    async def _unblock(self, request: web.Request) -> web.Response:
        data = await self._require_json(request) or {}
        user_id = data.get("user_id")
        reason = str(data.get("reason", ""))[:30] if data.get("reason") else None
        if user_id is None:
            return self._json_response({"error": "missing user_id"}, status=400)
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return self._json_response({"error": "user_id must be an integer"}, status=400)
        msg: dict = {"type": "unblock", "user_id": user_id}
        if reason:
            msg["reason"] = reason
        result = await self.ws_client.send_and_wait(msg)
        return self._json_response(result)

    async def _update_status(self, request: web.Request) -> web.Response:
        data = await self._require_json(request) or {}
        status = data.get("status", "open")
        reason = str(data.get("reason", ""))[:30] if data.get("reason") else None
        msg: dict = {"type": "update_status", "status": status}
        if reason:
            msg["reason"] = reason
        result = await self.ws_client.send_and_wait(msg)
        return self._json_response(result)

    # ── Run ────────────────────────────────────────────────

    async def run(self) -> None:
        """启动 HTTP 服务器并保持运行"""
        import _files
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, LOCAL_HOST, self.port)
        await site.start()
        _files.write_daemon_log(
            self.workspace, "INFO",
            f"HTTP server started on {LOCAL_HOST}:{self.port}"
        )
        # 保持运行直到被取消
        await self._shutdown_event.wait()

    def shutdown(self) -> None:
        """Trigger server shutdown — called by daemon on SIGTERM."""
        self._shutdown_event.set()
