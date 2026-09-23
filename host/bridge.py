from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import time
from typing import Callable
import uuid

import serial
from serial.tools import list_ports

from .protocol import (CANCEL, ERROR, HELLO, HELLO_ACK, INPUT_DELTA,
                       INPUT_SUBMITTED, PRINT_CREDIT, PRINT_DATA, PRINT_END,
                       PRINT_PROGRESS, RECOVER, STATE, Decoder, Frame,
                       encode_frame)


@dataclass
class DeviceSnapshot:
    connected: bool = False
    port: str | None = None
    device: str | None = None
    state: str = "disconnected"
    candidate: str = ""
    queued_bytes: int = 0
    credit: int = 0
    capacity: int = 0
    printed_bytes: int = 0
    active_request: str | None = None
    last_error: str | None = None
    agent_output: str = ""
    events: list[str] = None

    def as_dict(self) -> dict:
        value = self.__dict__.copy()
        value["events"] = list(self.events or [])
        return value


class SerialBridge:
    def __init__(self, on_frame: Callable[[Frame], None], on_change: Callable[[], None]):
        self.on_frame = on_frame
        self.on_change = on_change
        self.snapshot = DeviceSnapshot()
        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._sequence = 0

    @staticmethod
    def ports() -> list[dict]:
        return [{"device": p.device, "description": p.description,
                 "vid": p.vid, "pid": p.pid, "serial_number": p.serial_number}
                for p in list_ports.comports()]

    def open(self, port: str, baudrate: int = 115200) -> None:
        self.close()
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=0.1,
                                     write_timeout=1.0)
        self.snapshot.connected = True
        self.snapshot.port = port
        self.snapshot.state = "connecting"
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="serial-reader", daemon=True)
        self._thread.start()
        self.on_change()

    def close(self) -> None:
        self._stop.set()
        if self._serial:
            self._serial.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        self._serial = None
        self._thread = None
        self.snapshot.connected = False
        self.snapshot.state = "disconnected"
        self.on_change()

    def send(self, msg_type: int, payload: bytes = b"", request_id: bytes | None = None) -> None:
        if not self._serial or not self._serial.is_open:
            raise ConnectionError("device is not connected")
        frame = encode_frame(msg_type, request_id, payload, self._sequence)
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        with self._write_lock:
            self._serial.write(frame)

    def _reader(self) -> None:
        decoder = Decoder()
        try:
            while not self._stop.is_set() and self._serial and self._serial.is_open:
                data = self._serial.read(256)
                if not data:
                    continue
                for frame in decoder.feed(data):
                    self.on_frame(frame)
        except Exception as exc:  # serial disconnects are surfaced in state
            self.snapshot.last_error = str(exc)
        finally:
            self.snapshot.connected = False
            self.snapshot.state = "disconnected"
            self.on_change()


class DeviceService:
    def __init__(self):
        self.lock = threading.RLock()
        self.bridge = SerialBridge(self._on_frame, self._changed)
        self.snapshot = self.bridge.snapshot
        self._listeners: list[Callable[[dict], None]] = []
        self._pending: list[tuple[bytes, bytes]] = []
        self._end_pending: bytes | None = None
        self._agent_broadcast: Callable[[dict], None] | None = None

    def add_listener(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def set_agent_broadcast(self, callback: Callable[[dict], None]) -> None:
        self._agent_broadcast = callback

    def _changed(self) -> None:
        with self.lock:
            self.snapshot = self.bridge.snapshot
            value = self.snapshot.as_dict()
        for callback in self._listeners:
            callback(value)

    def _emit_agent(self, message: dict) -> None:
        if self._agent_broadcast:
            self._agent_broadcast(message)

    def _record(self, message: str) -> None:
        if self.snapshot.events is None:
            self.snapshot.events = []
        self.snapshot.events.append(message)
        del self.snapshot.events[:-100]

    def _on_frame(self, frame: Frame) -> None:
        with self.lock:
            if frame.type == HELLO:
                hello = json.loads(frame.payload.decode("ascii"))
                self.snapshot.device = hello.get("device")
                self._record(f"HELLO: {self.snapshot.device}")
                self.bridge.send(HELLO_ACK)
            elif frame.type == STATE:
                value = json.loads(frame.payload.decode("ascii"))
                for key in ("state", "queued_bytes", "capacity", "printed_bytes"):
                    if key in value:
                        setattr(self.snapshot, key, value[key])
                if value.get("state") == "editing" and self.snapshot.active_request:
                    self.snapshot.active_request = None
                    self.snapshot.candidate = ""
            elif frame.type == INPUT_DELTA:
                for char in frame.payload.decode("ascii"):
                    if char == "\b":
                        self.snapshot.candidate = self.snapshot.candidate[:-1]
                    else:
                        self.snapshot.candidate += char
            elif frame.type == INPUT_SUBMITTED:
                request_id = str(uuid.UUID(bytes=frame.request_id))
                if frame.payload:
                    self.snapshot.candidate = frame.payload.decode("ascii")
                self.snapshot.active_request = request_id
                self.snapshot.state = "thinking"
                self.snapshot.agent_output = ""
                self._record(f"提交请求: {request_id}")
                self._pending.clear()
                self._end_pending = None
                self._emit_agent({"v": 1, "type": "input.submitted",
                                  "request_id": request_id,
                                  "text": self.snapshot.candidate,
                                  "capabilities": {"output_charset": "ascii"}})
            elif frame.type == PRINT_CREDIT:
                value = json.loads(frame.payload.decode("ascii"))
                self.snapshot.credit = int(value.get("credit", 0))
                self.snapshot.queued_bytes = int(value.get("queued", 0))
                self._flush_pending()
            elif frame.type == PRINT_PROGRESS:
                self._record("设备打印进度更新")
                self._emit_agent({"v": 1, "type": "print.progress",
                                  "request_id": str(uuid.UUID(bytes=frame.request_id))})
            elif frame.type == ERROR:
                value = json.loads(frame.payload.decode("ascii"))
                self.snapshot.last_error = value.get("error", "device_error")
                self._record(f"设备错误: {self.snapshot.last_error}")
                self._emit_agent({"v": 1, "type": "device.error", "error": self.snapshot.last_error})
        self._changed()

    def _flush_pending(self) -> None:
        while self._pending and self.snapshot.credit >= len(self._pending[0][1]):
            request_id, payload = self._pending.pop(0)
            self.bridge.send(PRINT_DATA, payload, request_id)
            self.snapshot.credit -= len(payload)
        if self._end_pending is not None and not self._pending:
            request_id = self._end_pending
            self.bridge.send(PRINT_END, b"", request_id)
            self._end_pending = None

    def append_input(self, text: str) -> None:
        payload = text.encode("ascii")
        self.bridge.send(INPUT_DELTA, payload)

    def submit(self, text: str = "") -> str:
        with self.lock:
            if self.snapshot.active_request or self.snapshot.state in {"thinking", "responding", "draining"}:
                raise RuntimeError("busy")
        if text:
            self.append_input(text)
        request_id = uuid.uuid4()
        self.bridge.send(INPUT_SUBMITTED, request_id.bytes)
        return str(request_id)

    def response_delta(self, request_id: str, seq: int, text: str) -> None:
        rid = uuid.UUID(request_id).bytes
        with self.lock:
            if self.snapshot.active_request != request_id:
                raise RuntimeError("unknown request")
            payload = text.encode("ascii")
            self.snapshot.agent_output += text
            self._record(f"Agent 输出 {len(payload)} 字节")
            for offset in range(0, len(payload), 128):
                chunk = payload[offset:offset + 128]
                if self.snapshot.credit >= len(chunk):
                    self.bridge.send(PRINT_DATA, chunk, rid)
                    self.snapshot.credit -= len(chunk)
                else:
                    if sum(len(item[1]) for item in self._pending) + len(chunk) > 262144:
                        raise RuntimeError("response buffer full")
                    self._pending.append((rid, chunk))

    def response_end(self, request_id: str) -> None:
        rid = uuid.UUID(request_id).bytes
        with self.lock:
            if self.snapshot.active_request != request_id:
                raise RuntimeError("unknown request")
            self._end_pending = rid
            self._flush_pending()

    def print_text(self, text: str) -> str:
        payload = text.encode("ascii")
        if not payload:
            raise ValueError("text must not be empty")
        with self.lock:
            if self.snapshot.active_request or self.snapshot.state in {"thinking", "responding", "draining"}:
                raise RuntimeError("busy")
            request_id = str(uuid.uuid4())
            self.snapshot.active_request = request_id
            self.snapshot.state = "responding"
            self.snapshot.agent_output = ""
            self._record(f"电脑直接打印请求: {request_id}")
            self._pending.clear()
            self._end_pending = None
        self.response_delta(request_id, 0, text)
        self.response_end(request_id)
        return request_id

    def recover(self) -> None:
        self.bridge.send(RECOVER)

    def cancel(self, request_id: str) -> None:
        self.bridge.send(CANCEL, b"", uuid.UUID(request_id).bytes)
