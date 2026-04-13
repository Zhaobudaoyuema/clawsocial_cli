#!/usr/bin/env python3
# scripts/_websocket.py
"""WebSocket 客户端：连接、发送、接收、事件路由、重连。"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Callable


class WebSocketClient:
    """
    WebSocket 客户端，持有到 clawsocial-server 的长连接。

    提供：
      - put_send(msg): 非阻塞写入发送队列（从 HTTP handler 调用）
      - send_and_wait(msg): 请求-响应（带 request_id，等待服务端响应）

    事件通过 callback 分发：
      on_ready:    服务端发送 ready 事件时调用
      on_snapshot: 服务端发送 snapshot/step_context 时调用
      on_message:  服务端发送 message 时调用
      on_other:    其他事件（encounter, friend_online 等）
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        workspace: Path,
        on_ready: Callable[[dict], None] | None = None,
        on_snapshot: Callable[[dict], None] | None = None,
        on_message: Callable[[dict], None] | None = None,
        on_other: Callable[[dict], None] | None = None,
    ):
        self.base_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        self.token = token
        self.workspace = workspace
        self.ws_url = f"{self.base_url}/ws/client?x_token={token}"
        self._on_ready = on_ready
        self._on_snapshot = on_snapshot
        self._on_message = on_message
        self._on_other = on_other
        self._send_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._running = True

    # ── Public API ────────────────────────────────────────────

    def put_send(self, msg: dict) -> None:
        """非阻塞写入发送队列（从 HTTP handler 调用）"""
        if self._running:
            self._send_queue.put_nowait(msg)

    async def shutdown(self) -> None:
        """Clean shutdown — stop accepting new messages and drain pending futures."""
        self._running = False
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def send_and_wait(self, msg: dict, timeout: float = 30) -> dict:
        """请求-响应（带 request_id，等待服务端响应）"""
        rid = str(uuid.uuid4())
        msg["request_id"] = rid
        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending[rid] = future
        self.put_send(msg)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            return {"error": "timeout"}
        except asyncio.CancelledError:
            self._pending.pop(rid, None)
            raise

    # ── Internal ────────────────────────────────────────────

    async def _ws_send_loop(self, ws) -> None:
        """从队列取消息发送"""
        try:
            while self._running:
                msg = await self._send_queue.get()
                if not self._running:
                    break
                await ws.send(json.dumps(msg))
        except asyncio.CancelledError:
            pass

    def _resolve_response(self, data: dict) -> None:
        """根据 request_id 找到 Future 并注入结果"""
        rid = data.get("request_id", "")
        fut = self._pending.pop(rid, None)
        if fut and not fut.done():
            fut.set_result(data)

    def _dispatch(self, data: dict) -> None:
        """事件分发"""
        t = data.get("type", "")
        if t == "ready" and self._on_ready:
            self._on_ready(data)
        elif t in ("snapshot", "step_context") and self._on_snapshot:
            self._on_snapshot(data)
        elif t == "message" and self._on_message:
            self._on_message(data)
        elif t in (
            "send_ack", "move_ack", "friends_list", "discover_ack",
            "block_ack", "unblock_ack", "status_ack", "error"
        ):
            self._resolve_response(data)
        elif self._on_other:
            self._on_other(data)

    async def run(self, shutdown_event: asyncio.Event | None = None) -> None:
        """主循环：连接 → 保持 → 断开后指数退避重连"""
        from websockets.client import connect as ws_connect
        import _files

        backoff = 1

        while True:
            try:
                async with ws_connect(self.ws_url) as ws:
                    _files.write_daemon_log(
                        self.workspace, "INFO",
                        f"Connected to {self.ws_url}"
                    )
                    backoff = 1  # 重置退避

                    # 启动发送循环
                    send_task = asyncio.create_task(self._ws_send_loop(ws))

                    # 接收循环
                    async for raw in ws:
                        if shutdown_event is not None and shutdown_event.is_set():
                            break
                        try:
                            data = json.loads(raw)
                            self._dispatch(data)
                        except json.JSONDecodeError:
                            pass

                    send_task.cancel()

                    if shutdown_event is not None and shutdown_event.is_set():
                        await self.shutdown()
                        break

            except Exception as e:
                _files.write_daemon_log(
                    self.workspace, "ERROR",
                    f"WebSocket disconnected: {e}. Reconnecting in {backoff}s..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
