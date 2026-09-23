from __future__ import annotations

from dataclasses import dataclass
import struct
import zlib

VERSION = 1
MAX_PAYLOAD = 480

HELLO = 1
HELLO_ACK = 2
STATE = 3
INPUT_DELTA = 4
INPUT_SUBMITTED = 5
PRINT_DATA = 6
PRINT_END = 7
PRINT_CREDIT = 8
PRINT_PROGRESS = 9
RECOVER = 10
CANCEL = 11
ERROR = 12
PING = 13
PONG = 14


@dataclass(slots=True)
class Frame:
    type: int
    flags: int
    request_id: bytes
    sequence: int
    payload: bytes


def _cobs_encode(data: bytes) -> bytes:
    output = bytearray([0])
    code_index = 0
    code = 1
    for value in data:
        if value == 0:
            output[code_index] = code
            code_index = len(output)
            output.append(0)
            code = 1
        else:
            output.append(value)
            code += 1
            if code == 0xFF:
                output[code_index] = code
                code_index = len(output)
                output.append(0)
                code = 1
    output[code_index] = code
    return bytes(output)


def _cobs_decode(data: bytes) -> bytes:
    output = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        index += 1
        if code == 0 or index + code - 1 > len(data):
            raise ValueError("invalid COBS frame")
        output.extend(data[index:index + code - 1])
        index += code - 1
        if code != 0xFF and index < len(data):
            output.append(0)
    return bytes(output)


def encode_frame(msg_type: int, request_id: bytes | None = None,
                 payload: bytes = b"", sequence: int = 0, flags: int = 0) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload exceeds protocol limit")
    rid = request_id or bytes(16)
    if len(rid) != 16:
        raise ValueError("request_id must be 16 bytes")
    raw = struct.pack("<BBB16sIH", VERSION, msg_type, flags, rid,
                      sequence, len(payload)) + payload
    raw += struct.pack("<I", zlib.crc32(raw) & 0xFFFFFFFF)
    return _cobs_encode(raw) + b"\x00"


def decode_frame(data: bytes) -> Frame:
    raw = _cobs_decode(data)
    if len(raw) < 29:
        raise ValueError("short protocol frame")
    version, msg_type, flags, request_id, sequence, payload_len = struct.unpack(
        "<BBB16sIH", raw[:25])
    if version != VERSION or payload_len > MAX_PAYLOAD or len(raw) != 29 + payload_len:
        raise ValueError("invalid protocol frame length/version")
    expected = struct.unpack("<I", raw[-4:])[0]
    if zlib.crc32(raw[:-4]) & 0xFFFFFFFF != expected:
        raise ValueError("protocol CRC mismatch")
    return Frame(msg_type, flags, request_id, sequence, raw[25:-4])


class Decoder:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        frames: list[Frame] = []
        for value in data:
            if value:
                if len(self._buffer) < 560:
                    self._buffer.append(value)
                else:
                    self._buffer.clear()
            elif self._buffer:
                try:
                    frames.append(decode_frame(bytes(self._buffer)))
                except ValueError:
                    pass
                self._buffer.clear()
        return frames
