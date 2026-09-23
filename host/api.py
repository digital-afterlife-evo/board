from __future__ import annotations

import asyncio
import json
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
        service.set_agent_broadcast(self.broadcast)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_ws, name="agent-ws", daemon=True)
        self._thread.start()
        handler = self._handler_factory()
        self._http = ThreadingHTTPServer((self.host, self.port), handler)
        threading.Thread(target=self._http.serve_forever, name="agent-http", daemon=True).start()

    def stop(self) -> None:
        if self._http:
            self._http.shutdown()
        if self.loop and self._stop_event:
            self.loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def broadcast(self, message: dict) -> None:
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._broadcast_async(message), self.loop)

    async def _broadcast_async(self, message: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(message, ensure_ascii=True)
        await asyncio.gather(*(client.send(data) for client in tuple(self.clients)),
                             return_exceptions=True)

    async def _client(self, connection: ServerConnection) -> None:
        self.clients.add(connection)
        try:
            await connection.send(json.dumps({"v": 1, "type": "state",
                                              "state": self.service.snapshot.as_dict()}))
            snapshot = self.service.snapshot
            if snapshot.active_request:
                await connection.send(json.dumps({
                    "v": 1,
                    "type": "input.submitted",
                    "request_id": snapshot.active_request,
                    "text": snapshot.candidate,
                    "capabilities": {"output_charset": "ascii"},
                }, ensure_ascii=True))
            async for raw in connection:
                await self._handle_agent_message(json.loads(raw))
        finally:
            self.clients.discard(connection)

    async def _handle_agent_message(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        try:
            if kind == "response.start":
                return
            if kind == "response.delta":
                self.service.response_delta(message["request_id"], int(message.get("seq", 0)),
                                            message.get("text", ""))
            elif kind == "response.end":
                self.service.response_end(message["request_id"])
            elif kind == "response.error":
                self.service.response_end(message["request_id"])
            else:
                await self._broadcast_async({"v": 1, "type": "error", "error": "unknown_message"})
        except (KeyError, ValueError, UnicodeEncodeError, RuntimeError) as exc:
            await self._broadcast_async({"v": 1, "type": "error", "error": str(exc)})

    def _run_ws(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        async def runner() -> None:
            self._stop_event = asyncio.Event()
            async with serve(self._client, self.host, self.port + 1):
                await self._stop_event.wait()
        try:
            self.loop.run_until_complete(runner())
        except RuntimeError:
            pass
        finally:
            self.loop.close()

    def _handler_factory(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _json(self, code: int, value: dict) -> None:
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
                path = urllib.parse.urlparse(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
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
                        request_id = gateway.service.print_text(body.get("text", ""))
                        self._json(202, {"request_id": request_id})
                    elif path.startswith("/api/v1/requests/") and path.endswith("/cancel"):
                        request_id = path.split("/")[-2]
                        gateway.service.cancel(request_id)
                        self._json(202, {"accepted": True})
                    else:
                        self._json(404, {"error": "not_found"})
                except (ValueError, UnicodeEncodeError, RuntimeError, ConnectionError) as exc:
                    self._json(409, {"error": str(exc)})

        return Handler
