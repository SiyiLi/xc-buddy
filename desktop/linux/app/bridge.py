from __future__ import annotations

import asyncio
import inspect
import json
import logging
import ssl
from collections.abc import Callable
from typing import Any, cast


LOG = logging.getLogger(__name__)


def classify_event(payload: dict) -> tuple[str, str]:
    raw_value = payload.get("type", payload.get("event", ""))
    raw = raw_value if isinstance(raw_value, str) else ""
    lowered = raw.lower()
    if lowered == "tool-call-start":
        return "tool_call_started", ""
    if "approval" in lowered:
        return "approval_needed", ""
    if "error" in lowered or "fail" in lowered:
        message = payload.get("message")
        return "error", (
            message if isinstance(message, str) and message else raw or "Codex error"
        )
    if "complete" in lowered or "done" in lowered:
        return "done", ""
    return "working", ""


def decode_relay_message(raw: str | bytes) -> tuple[str, str] | None:
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(message, dict):
        return None
    if (
        set(message) == {"kind", "state", "snapshot"}
        and message["kind"] == "state"
        and isinstance(message["snapshot"], bool)
        and message["state"] in ("idle", "working", "approval_needed")
    ):
        return message["state"], ""
    if (
        set(message) == {"kind", "notice"}
        and message["kind"] == "notice"
        and message["notice"] in ("done", "error")
    ):
        return (
            message["notice"],
            "Codex error" if message["notice"] == "error" else "",
        )
    return None


class CodexBridge:
    def __init__(
        self,
        port: int,
        token: str,
        callback: Callable[[str, str], None],
        status: Callable[[str], None] | None = None,
    ) -> None:
        self.port, self.token, self.callback = port, token, callback
        self.status = status or (lambda _value: None)
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.status("Starting")
        self.server = await asyncio.start_server(self._request, "127.0.0.1", self.port)
        port = self.server.sockets[0].getsockname()[1]
        self.status(f"Listening on 127.0.0.1:{port}")

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            self.status("Stopped")

    async def _request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status, reason, body = 400, "Bad Request", b"invalid request"
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if len(headers) > 65536:
                raise ValueError("headers too large")
            lines = headers.decode().split("\r\n")
            fields = {
                k.strip().lower(): v.strip()
                for line in lines[1:]
                if ":" in line
                for k, v in [line.split(":", 1)]
            }
            length = int(fields.get("content-length", "0"))
            if length > 1_048_576:
                status, reason, body = 413, "Payload Too Large", b"request too large"
            elif not lines[0].startswith("POST "):
                status, reason, body = 405, "Method Not Allowed", b"POST required"
            elif (
                self.token
                and fields.get("authorization", "").lower()
                != f"bearer {self.token}".lower()
            ):
                status, reason, body = 401, "Unauthorized", b"unauthorized"
            else:
                payload = json.loads((await reader.readexactly(length)).decode())
                if not isinstance(payload, dict):
                    raise ValueError("JSON request must be an object")
                event, message = classify_event(payload)
                self.callback(event, message)
                status, reason, body = 204, "No Content", b""
        except (
            ValueError,
            json.JSONDecodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
            UnicodeDecodeError,
        ):
            pass
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()


class RelayClient:
    def __init__(
        self,
        callback: Callable[[str, str], None],
        status: Callable[[str], None],
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.callback, self.status = callback, status
        self.ssl_context = ssl_context
        self.mode = "disabled"
        self.task: asyncio.Task | None = None
        self.socket: Any | None = None
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.sender_state = "idle"

    async def start(self, mode: str, endpoint: str, token: str) -> None:
        await self.stop()
        endpoint, token = endpoint.strip(), token.strip()
        self.mode = mode
        if mode == "disabled":
            self.status("Disabled")
            return
        if not endpoint.lower().startswith("wss://"):
            self.status("Relay URL must use wss://")
            return
        if not token:
            self.status(f"{mode.capitalize()} token is not set")
            return
        self.task = asyncio.create_task(self._run(endpoint, token))

    async def stop(self) -> None:
        self.mode = "disabled"
        socket = self.socket
        if socket is not None:
            await socket.close()
            await socket.wait_closed()
            if self.socket is socket:
                self.socket = None
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        self._clear_queue()

    def _clear_queue(self) -> None:
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def publish(self, event: str) -> None:
        if self.mode != "sender":
            return
        if event in ("working", "tool_call_started", "approval_needed", "idle"):
            state = "working" if event == "tool_call_started" else event
            self.sender_state = state
            self.queue.put_nowait({"kind": "state", "state": state})
        elif event in ("done", "error"):
            self.sender_state = "idle"
            self.queue.put_nowait({"kind": "notice", "notice": event})

    def retain_sender_state(self, state: str) -> None:
        self.sender_state = state

    async def _send_messages(self, socket: Any) -> None:
        while True:
            queued = asyncio.create_task(self.queue.get())
            closed = asyncio.create_task(socket.wait_closed())
            try:
                done, _ = await asyncio.wait(
                    (queued, closed), return_when=asyncio.FIRST_COMPLETED
                )
                if closed in done:
                    raise ConnectionError("relay connection closed")
                await socket.send(json.dumps(queued.result(), separators=(",", ":")))
            finally:
                queued.cancel()
                closed.cancel()
                await asyncio.gather(queued, closed, return_exceptions=True)

    async def _run(self, endpoint: str, token: str) -> None:
        import websockets

        delay = 1
        was_connected = False
        while True:
            try:
                self.status("Connecting")
                header_name = (
                    "additional_headers"
                    if "additional_headers"
                    in inspect.signature(websockets.connect).parameters
                    else "extra_headers"
                )
                connect = cast(Any, websockets.connect)
                options: dict[str, Any] = {
                    header_name: {"Authorization": f"Bearer {token}"}
                }
                if self.ssl_context is not None:
                    options["ssl"] = self.ssl_context
                async with connect(
                    endpoint,
                    **options,
                    ping_interval=30,
                    ping_timeout=10,
                ) as socket:
                    self.socket = socket
                    try:
                        self.status("Connected")
                        was_connected = True
                        delay = 1
                        if self.mode == "sender":
                            await socket.send(
                                json.dumps(
                                    {"kind": "state", "state": self.sender_state}
                                )
                            )
                            await self._send_messages(socket)
                        else:
                            while True:
                                raw = await socket.recv()
                                if event := decode_relay_message(raw):
                                    self.callback(*event)
                    finally:
                        if self.socket is socket:
                            self.socket = None
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.debug("relay connection failed", exc_info=True)
                if self.mode == "disabled":
                    return
                if was_connected and self.mode == "receiver":
                    self.callback("idle", "")
                was_connected = False
                self.status("Reconnecting")
                await asyncio.sleep(delay)
                self._clear_queue()
                delay = min(delay * 2, 30)
