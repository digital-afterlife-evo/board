from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
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
    host_protocol: int = 2
    safe_recovery: bool = True
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
    active_source: str | None = None
    last_device_seen_at: int | None = None
    last_delivery: dict | None = None
    last_tx_byte: str | None = None

    def as_dict(self) -> dict:
        value = self.__dict__.copy()
        value["events"] = list(self.events or [])
        return value


class SerialBridge:
    def __init__(self, on_frame: Callable[[Frame], None], on_change: Callable[[], None], debug: bool = False):
        self.on_frame = on_frame
        self.on_change = on_change
        self.snapshot = DeviceSnapshot()
        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._sequence = 0
        self.debug = debug
        self._log_tail = b""

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
        logging.info("[board.serial.connected] port=%s baud=%d", port, baudrate)
        self.on_change()

    def close(self) -> None:
        if self.snapshot.connected:
            logging.info("[board.serial.closing] port=%s", self.snapshot.port)
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
                # Firmware diagnostics share this UART with protocol frames. Retain only hardware errors.
                self._log_tail = (self._log_tail + data)[-4096:]
                started = list(re.finditer(rb"starting byte 0x([0-9A-Fa-f]{2})", self._log_tail))
                if started:
                    self.snapshot.last_tx_byte = started[-1].group(1).decode("ascii")
                failure = re.search(rb"ACK timeout:[^\r\n]*[\r\n]", self._log_tail)
                if failure:
                    self.snapshot.last_error = failure.group(0).decode("ascii", errors="replace").strip()
                    self._log_tail = self._log_tail[failure.end():]
                    logging.error("[board.uart.ack_timeout] last_byte=%s detail=%s", self.snapshot.last_tx_byte, self.snapshot.last_error)
                    self.on_change()
                for frame in decoder.feed(data):
                    if self.debug:
                        logging.debug("[board.uart.rx] type=%d seq=%d request_id=%s bytes=%d",
                                      frame.type, frame.sequence, frame.request_id.hex(),
                                      len(frame.payload))
                    self.on_frame(frame)
        except Exception as exc:  # serial disconnects are surfaced in state
            logging.error("[board.serial.failed] port=%s error=%s", self.snapshot.port, type(exc).__name__)
            self.snapshot.last_error = str(exc)
        finally:
            self.snapshot.connected = False
            self.snapshot.state = "disconnected"
            self.on_change()


class DeviceService:
    def __init__(self, debug: bool = False, char_interval_ms: int = 0, return_delay_ms: int = 0):
        self.lock = threading.RLock()
        self.bridge = SerialBridge(self._on_frame, self._changed, debug=debug)
        self.debug = debug
        self.snapshot = self.bridge.snapshot
        self._listeners: list[Callable[[dict], None]] = []
        self._pending: list[tuple[bytes, bytes]] = []
        self._end_pending: bytes | None = None
        self._input_request_id: str | None = None
        self._agent_broadcast: Callable[[dict], None] | None = None
        self._sequences: dict[int, str] = {}
        self._end_sent: str | None = None
        self._draining_seen = False
        self._print_baseline = 0
        self._expected_bytes = 0
        self._cancel_pending: str | None = None
        self._char_interval = char_interval_ms / 1000
        self._return_delay = return_delay_ms / 1000
        self._next_send_at = 0.0
        self._thinking_last = 0.0
        self._thinking_dots = 0
        self._thinking_visible = False
        self._reply_started = False
        self._recover_pending = False
        self._last_state_log = None
        self._last_output_log = 0.0
        self._sent_bytes = 0

    def _begin_output(self, request_id: str, source: str) -> None:
        logging.info("[board.output.begin] request_id=%s source=%s printed_baseline=%d", request_id, source, self.snapshot.printed_bytes)
        self.snapshot.active_request = request_id
        self.snapshot.active_source = source
        self.snapshot.agent_output = ""
        self.snapshot.last_error = None
        self._pending.clear()
        self._end_pending = None
        self._end_sent = None
        self._draining_seen = False
        self._cancel_pending = None
        self._sequences.clear()
        self._print_baseline = self.snapshot.printed_bytes
        self._expected_bytes = 0
        self._thinking_last = time.monotonic()
        self._thinking_dots = 0
        self._thinking_visible = False
        self._reply_started = False
        self._sent_bytes = 0
        self._last_output_log = 0.0

    def begin_thinking(self, request_id: str) -> str:
        request_id = str(uuid.UUID(request_id))
        with self.lock:
            if self.snapshot.active_request == request_id:
                return request_id
            if self.snapshot.active_request or self.snapshot.state != "editing":
                raise RuntimeError("busy")
            if not self.snapshot.connected:
                raise RuntimeError("device unavailable")
            self._begin_output(request_id, "web-thinking")
            self.snapshot.state = "responding"
            self._thinking_last -= 1.5
        return request_id

    def tick_output(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self.lock:
            if (self.snapshot.active_request and self.snapshot.active_source in {"keyboard", "web-thinking"}
                    and not self._reply_started and self.snapshot.state in {"thinking", "responding"}
                    and now - self._thinking_last >= 1.5):
                if not self._thinking_visible:
                    payload = b"thinking." if self.snapshot.active_source == "keyboard" else b"thinking"
                else:
                    payload = b"\n\rthinking." if self._thinking_dots >= 32 else b"."
                self._thinking_dots = 0 if self._thinking_dots >= 32 else self._thinking_dots + 1
                self._thinking_visible = True
                self._thinking_last = now
                self._expected_bytes += len(payload)
                self._pending.append((uuid.UUID(self.snapshot.active_request).bytes, payload))
                logging.info("[board.thinking] request_id=%s emission=%s next_in_s=1.5", self.snapshot.active_request, "dot" if payload == b"." else "label")
            self._flush_pending()

    def add_listener(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def set_agent_broadcast(self, callback: Callable[[dict], None]) -> None:
        self._agent_broadcast = callback

    def _changed(self) -> None:
        with self.lock:
            self.snapshot = self.bridge.snapshot
            value = self.snapshot.as_dict()
            key = (value["connected"], value["state"], value["active_request"], value["last_error"])
            if key != self._last_state_log:
                self._last_state_log = key
                logging.info("[board.state] connected=%s state=%s request_id=%s queued_bytes=%d printed_bytes=%d last_byte=%s error=%s",
                             value["connected"], value["state"], value["active_request"], value["queued_bytes"], value["printed_bytes"], value["last_tx_byte"], value["last_error"])
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
            self.snapshot.last_device_seen_at = int(time.time() * 1000)
            if self.debug:
                logging.debug("SERVICE frame type=%d candidate_len=%d active=%s state=%s",
                              frame.type, len(self.snapshot.candidate),
                              self.snapshot.active_request, self.snapshot.state)
            if frame.type == HELLO:
                hello = json.loads(frame.payload.decode("ascii"))
                self.snapshot.device = hello.get("device")
                self._record(f"HELLO: {self.snapshot.device}")
                self.bridge.send(HELLO_ACK)
            elif frame.type == STATE:
                value = json.loads(frame.payload.decode("ascii"))
                if self._recover_pending and value.get("state") == "editing":
                    self._recover_pending = False
                    self.snapshot.last_error = None
                if value.get("state") == "draining" and self._end_sent == self.snapshot.active_request:
                    self._draining_seen = True
                for key in ("state", "queued_bytes", "capacity", "printed_bytes"):
                    if key in value:
                        setattr(self.snapshot, key, value[key])
                if value.get("state") == "editing" and self.snapshot.active_request:
                    bytes_acked = self._expected_bytes > 0 and value.get("printed_bytes", 0) - self._print_baseline >= self._expected_bytes
                    if self._end_sent == self.snapshot.active_request and (self._draining_seen or bytes_acked) and not self._pending and not value.get("queued_bytes"):
                        logging.info("[board.software_drained] request_id=%s expected_bytes=%d ack_delta=%d draining_seen=%s",
                                     self.snapshot.active_request, self._expected_bytes, value.get("printed_bytes", 0) - self._print_baseline, self._draining_seen)
                        # This proves software draining only, not mechanical/paper completion.
                        self.snapshot.last_delivery = {"request_id": self.snapshot.active_request, "status": "drained", "at": int(time.time() * 1000)}
                        self._emit_agent({"v": 1, "type": "response.drained", **self.snapshot.last_delivery})
                        self.snapshot.active_request = None
                        self.snapshot.active_source = None
                    elif self._cancel_pending == self.snapshot.active_request:
                        self.snapshot.active_request = None
                        self.snapshot.active_source = None
            elif frame.type == INPUT_DELTA:
                if frame.request_id != bytes(16):
                    request_id = str(uuid.UUID(bytes=frame.request_id))
                    if request_id != self._input_request_id:
                        self.snapshot.candidate = ""
                        self._input_request_id = request_id
                for char in frame.payload.decode("ascii"):
                    if char == "\b":
                        self.snapshot.candidate = self.snapshot.candidate[:-1]
                    else:
                        self.snapshot.candidate += char
                if self.debug:
                    logging.debug("[board.candidate] chars=%d", len(self.snapshot.candidate))
            elif frame.type == INPUT_SUBMITTED:
                request_id = str(uuid.UUID(bytes=frame.request_id))
                if self.snapshot.active_request == request_id:
                    return
                if frame.payload:
                    self.snapshot.candidate = frame.payload.decode("ascii")
                logging.info("[board.input.submitted] request_id=%s chars=%d embedded_payload=%s", request_id, len(self.snapshot.candidate), bool(frame.payload))
                self._begin_output(request_id, "keyboard")
                self.snapshot.state = "thinking"
                self._record(f"提交请求: {request_id}")
                if self.debug:
                    logging.debug("[board.input.buffer] request_id=%s candidate_chars=%d", request_id, len(self.snapshot.candidate))
                self._emit_agent({"v": 1, "type": "input.submitted",
                                  "request_id": request_id,
                                  "text": self.snapshot.candidate,
                                  "locally_printed": True,
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
                logging.error("[board.device.error] request_id=%s code=%s", str(uuid.UUID(bytes=frame.request_id)), self.snapshot.last_error)
                self._pending.clear()
                self._end_pending = None
                self.snapshot.state = "fault"
                self._end_sent = None
                self._record(f"设备错误: {self.snapshot.last_error}")
                self._emit_agent({"v": 1, "type": "device.error", "request_id": str(uuid.UUID(bytes=frame.request_id)), "error": self.snapshot.last_error})
        self._changed()

    def _flush_pending(self) -> None:
        if self.snapshot.state in {"fault", "disconnected"}:
            return
        while self._pending and self.snapshot.credit >= len(self._pending[0][1]):
            request_id, payload = self._pending[0]
            if self._char_interval:
                if time.monotonic() < self._next_send_at:
                    return
                payload = payload[:1]
            self.bridge.send(PRINT_DATA, payload, request_id)
            self._sent_bytes += len(payload)
            if time.monotonic() - self._last_output_log >= 1:
                self._last_output_log = time.monotonic()
                logging.info("[board.output.progress] request_id=%s host_sent_bytes=%d expected_bytes=%d credit=%d", str(uuid.UUID(bytes=request_id)), self._sent_bytes, self._expected_bytes, self.snapshot.credit)
            remaining = self._pending[0][1][len(payload):]
            if remaining:
                self._pending[0] = (request_id, remaining)
            else:
                self._pending.pop(0)
            self.snapshot.credit -= len(payload)
            if self._char_interval:
                self._next_send_at = time.monotonic() + (max(self._return_delay, self._char_interval) if payload == b"\r" else self._char_interval)
                if self._pending:
                    return
        if self._end_pending is not None and not self._pending:
            if self._char_interval and time.monotonic() < self._next_send_at:
                return
            request_id = self._end_pending
            self.bridge.send(PRINT_END, b"", request_id)
            self._end_pending = None
            self._end_sent = str(uuid.UUID(bytes=request_id))
            self._expected_bytes += 2  # Firmware appends CRLF when PRINT_END is accepted.
            logging.info("[board.output.end_sent] request_id=%s expected_bytes=%d awaiting=software_drain", self._end_sent, self._expected_bytes)

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
        self.bridge.send(INPUT_SUBMITTED, request_id=request_id.bytes)
        return str(request_id)

    def response_delta(self, request_id: str, seq: int, text: str) -> None:
        rid = uuid.UUID(request_id).bytes
        with self.lock:
            if self.snapshot.active_request != request_id:
                raise RuntimeError("unknown request")
            payload = text.encode("ascii")
            if any(byte < 32 and byte not in (10, 13) or byte > 126 for byte in payload):
                raise ValueError("unsupported control character")
            if seq in self._sequences:
                if self._sequences[seq] != text:
                    raise ValueError("conflicting sequence")
                return
            if seq != len(self._sequences) or self._end_pending is not None or self._end_sent == request_id:
                raise RuntimeError("out of order or ended response")
            if self.snapshot.state in {"fault", "disconnected"}:
                raise RuntimeError("device unavailable")
            if self._thinking_visible and not self._reply_started:
                payload = b"\n\r" + payload
            if self._expected_bytes + len(payload) > 262144:
                raise RuntimeError("response buffer full")
            self._reply_started = True
            if not self._sequences:
                logging.info("[board.reply.started] request_id=%s thinking_stopped=%s", request_id, self._thinking_visible)
            logging.debug("[board.reply.delta] request_id=%s seq=%d bytes=%d", request_id, seq, len(payload))
            self._sequences[seq] = text
            self._expected_bytes += len(payload)
            self.snapshot.agent_output += text
            self._record(f"Agent 输出 {len(payload)} 字节")
            for offset in range(0, len(payload), 128):
                chunk = payload[offset:offset + 128]
                self._pending.append((rid, chunk))
            self._flush_pending()

    def response_end(self, request_id: str) -> None:
        rid = uuid.UUID(request_id).bytes
        with self.lock:
            if self.snapshot.active_request != request_id:
                raise RuntimeError("unknown request")
            if self._end_sent == request_id or self._end_pending == rid:
                return
            self._reply_started = True
            self._end_pending = rid
            self._flush_pending()

    def print_text(self, text: str, request_id: str | None = None) -> str:
        payload = text.encode("ascii")
        if not payload:
            raise ValueError("text must not be empty")
        if len(payload) > 262144 or any(byte < 32 and byte not in (10, 13) or byte > 126 for byte in payload):
            raise ValueError("unsupported or oversized print text")
        request_id = str(uuid.UUID(request_id)) if request_id else str(uuid.uuid4())
        with self.lock:
            if self.snapshot.active_request == request_id:
                if self.snapshot.agent_output != text:
                    raise ValueError("request text conflict")
                return request_id
            if (self.snapshot.last_delivery or {}).get("request_id") == request_id:
                return request_id
            if self.snapshot.active_request or self.snapshot.state in {"thinking", "responding", "draining"}:
                raise RuntimeError("busy")
            if not self.snapshot.connected or self.snapshot.state != "editing":
                raise RuntimeError("device unavailable")
            self._begin_output(request_id, "direct")
            self.snapshot.state = "responding"
            self._record(f"电脑直接打印请求: {request_id}")
            self.response_delta(request_id, 0, text)
            self.response_end(request_id)
        self._changed()
        return request_id

    def recover(self) -> None:
        with self.lock:
            if not self.snapshot.connected or self.snapshot.state != "fault":
                raise RuntimeError("recovery requires a connected faulted device")
            if self.snapshot.queued_bytes:
                raise RuntimeError("device queue is not empty")
            logging.warning("[board.device.recover] port=%s state=%s request_id=%s", self.snapshot.port, self.snapshot.state, self.snapshot.active_request)
            # Drop unsent bytes from the failed request; they must never resume after recovery.
            self._pending.clear()
            self._end_pending = None
            self._end_sent = None
            self._draining_seen = False
            self._reply_started = True
            self._cancel_pending = None
            self.snapshot.active_request = None
            self.snapshot.active_source = None
            self._recover_pending = True
            self.bridge.send(RECOVER)

    def cancel(self, request_id: str) -> None:
        with self.lock:
            if request_id != self.snapshot.active_request:
                raise RuntimeError("unknown request")
            logging.warning("[board.request.cancel] request_id=%s pending_chunks=%d", request_id, len(self._pending))
            self._pending.clear()
            self._end_pending = None
            self._end_sent = None
            self._draining_seen = False
            self._cancel_pending = request_id
            self._reply_started = True
            self.bridge.send(CANCEL, b"", uuid.UUID(request_id).bytes)
