from __future__ import annotations

import asyncio
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Any
import urllib.parse

from websockets.asyncio.server import ServerConnection, serve

from .bridge import DeviceService


class AgentGateway:
    def __init__(self, service: DeviceService, host: str = "127.0.0.1", port: int = 8765):
        self.service = service
        self.host = host
        self.port = port
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[ServerConnection] = set()
        self._thread: threading.Thread | None = None
        self._http: ThreadingHTTPServer | None = None
        self._stop_event: asyncio.Event | None = None
        self._output_stop = threading.Event()
        service.set_agent_broadcast(self.broadcast)
        service.add_listener(lambda state: self.broadcast({"v": 1, "type": "state", "state": state}))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_ws, name="agent-ws", daemon=True)
        self._thread.start()
        handler = self._handler_factory()
        self._http = ThreadingHTTPServer((self.host, self.port), handler)
        logging.info("[board.http.listening] host=%s port=%d", self.host, self.port)
        threading.Thread(target=self._http.serve_forever, name="agent-http", daemon=True).start()
        threading.Thread(target=self._output_loop, name="printer-output", daemon=True).start()

    def _output_loop(self) -> None:
        while not self._output_stop.wait(.01):
            try:
                self.service.tick_output()
            except (RuntimeError, ConnectionError, OSError) as exc:
                logging.error("[board.output.failed] request_id=%s error=%s", self.service.snapshot.active_request, type(exc).__name__)
                with self.service.lock:
                    self.service.snapshot.last_error = str(exc)
                    self.service.snapshot.state = "fault"
                    self.service._pending.clear()
                    self.service._end_pending = None
                self.broadcast({"v": 1, "type": "device.error", "error": "host_output_failed"})

    def stop(self) -> None:
        logging.info("[board.stopping]")
        self._output_stop.set()
        if self._http:
            self._http.shutdown()
        if self.loop and self._stop_event:
            self.loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def broadcast(self, message: dict) -> None:
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast_async(message), self.loop)

    async def _broadcast_async(self, message: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(message, ensure_ascii=True)
        await asyncio.gather(*(client.send(data) for client in tuple(self.clients)),
                             return_exceptions=True)

    async def _client(self, connection: ServerConnection) -> None:
        if self.clients:
            logging.warning("[board.ws.rejected] reason=agent_already_connected")
            await connection.close(code=1013, reason="Only one Agent connection is allowed")
            return
        self.clients.add(connection)
        logging.info("[board.ws.connected] peer=%s", connection.remote_address)
        try:
            await connection.send(json.dumps({"v": 1, "type": "state",
                                              "state": self.service.snapshot.as_dict()}))
            snapshot = self.service.snapshot
            if snapshot.active_request and snapshot.active_source == "keyboard":
                await connection.send(json.dumps({
                    "v": 1,
                    "type": "input.submitted",
                    "request_id": snapshot.active_request,
                    "text": snapshot.candidate,
                    "locally_printed": True,
                    "capabilities": {"output_charset": "ascii"},
                }, ensure_ascii=True))
            async for raw in connection:
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict) or message.get("v") != 1:
                        raise ValueError("invalid message")
                    await self._handle_agent_message(message)
                except (ValueError, TypeError):
                    await connection.send(json.dumps({"v": 1, "type": "error", "error": "invalid message"}))
        finally:
            self.clients.discard(connection)
            logging.info("[board.ws.disconnected] peer=%s", connection.remote_address)

    async def _handle_agent_message(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        try:
            if kind == "response.start":
                return
            if kind == "stop.ack":
                self.service.acknowledge_stop(message["stop_id"])
            elif kind == "response.delta":
                self.service.response_delta(message["request_id"], int(message.get("seq", 0)),
                                            message.get("text", ""))
            elif kind == "response.end":
                self.service.response_end(message["request_id"])
            elif kind == "response.error":
                self.service.response_end(message["request_id"])
            else:
                await self._broadcast_async({"v": 1, "type": "error", "error": "unknown_message"})
        except (KeyError, ValueError, UnicodeEncodeError, RuntimeError, ConnectionError) as exc:
            logging.warning("[board.ws.failed] type=%s request_id=%s error=%s", kind, message.get("request_id"), type(exc).__name__)
            await self._broadcast_async({"v": 1, "type": "error", "request_id": message.get("request_id"), "error": str(exc)})

    def _run_ws(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        async def runner() -> None:
            self._stop_event = asyncio.Event()
            async with serve(self._client, self.host, self.port + 1):
                logging.info("[board.ws.listening] host=%s port=%d", self.host, self.port + 1)
                await self._stop_event.wait()
        try:
            self.loop.run_until_complete(runner())
        except (RuntimeError, OSError) as exc:
            logging.error("[board.ws.start_failed] host=%s port=%d error=%s detail=%s", self.host, self.port + 1, type(exc).__name__, str(exc))
        finally:
            self.loop.close()

    def _handler_factory(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _json(self, code: int, value: dict) -> None:
                if self.command == "POST" or code >= 400:
                    logging.log(logging.WARNING if code >= 400 else logging.INFO,
                                "[board.http] method=%s path=%s status=%d request_id=%s ms=%.0f",
                                self.command, self.path.split("?")[0], code, value.get("request_id"),
                                (time.monotonic() - getattr(self, "_started", time.monotonic())) * 1000)
                data = json.dumps(value, ensure_ascii=True).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path == "/api/v1/health":
                    self._json(200, {"ok": True, "ws": f"ws://{gateway.host}:{gateway.port + 1}/api/v1/agent"})
                elif path == "/api/v1/state":
                    self._json(200, gateway.service.snapshot.as_dict())
                else:
                    self._json(404, {"error": "not_found"})

            def do_POST(self):  # noqa: N802
                self._started = time.monotonic()
                path = urllib.parse.urlparse(self.path).path
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > 1048576:
                        raise ValueError("request too large")
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("JSON object required")
                    if path == "/api/v1/device/recover":
                        gateway.service.recover()
                        self._json(202, {"accepted": True})
                    elif path == "/api/v1/input/append":
                        gateway.service.append_input(body.get("text", ""))
                        self._json(202, {"accepted": True})
                    elif path == "/api/v1/input/submit":
                        request_id = gateway.service.submit(body.get("text", ""))
                        self._json(202, {"request_id": request_id})
                    elif path == "/api/v1/print":
                        request_id = gateway.service.print_text(body.get("text", ""), body.get("request_id"))
                        self._json(202, {"request_id": request_id})
                    elif path == "/api/v1/thinking":
                        request_id = gateway.service.begin_thinking(body["request_id"])
                        self._json(202, {"request_id": request_id})
                    elif path.startswith("/api/v1/requests/") and path.endswith("/cancel"):
                        request_id = path.split("/")[-2]
                        gateway.service.cancel(request_id)
                        self._json(202, {"accepted": True})
                    else:
                        self._json(404, {"error": "not_found"})
                except (KeyError, TypeError, ValueError, UnicodeEncodeError, RuntimeError, ConnectionError) as exc:
                    logging.warning("[board.http.rejected] path=%s error=%s", path, type(exc).__name__)
                    self._json(409, {"error": str(exc)})

        return Handler
