from __future__ import annotations

import struct
from dataclasses import dataclass


class ADBProtocolError(ValueError):
    pass


def command_code(value: str) -> int:
    if len(value) != 4:
        raise ValueError("ADB command must be four ASCII bytes")
    return struct.unpack("<I", value.encode("ascii"))[0]


CNXN = command_code("CNXN")
OPEN = command_code("OPEN")
OKAY = command_code("OKAY")
WRTE = command_code("WRTE")
CLSE = command_code("CLSE")


def sync_code(value: str) -> int:
    return command_code(value)


SYNC_SEND = sync_code("SEND")
SYNC_DATA = sync_code("DATA")
SYNC_DONE = sync_code("DONE")
SYNC_RECV = sync_code("RECV")
SYNC_OKAY = sync_code("OKAY")
SYNC_FAIL = sync_code("FAIL")


@dataclass(frozen=True)
class ADBFrame:
    command: int
    arg0: int
    arg1: int
    payload: bytes = b""


def encode_frame(frame: ADBFrame) -> bytes:
    checksum = sum(frame.payload) & 0xFFFFFFFF
    header = struct.pack("<6I", frame.command, frame.arg0, frame.arg1, len(frame.payload), checksum, frame.command ^ 0xFFFFFFFF)
    return header + frame.payload


def decode_frame(data: bytes) -> ADBFrame:
    if len(data) < 24:
        raise ADBProtocolError("short ADB header")
    command, arg0, arg1, length, checksum, magic = struct.unpack("<6I", data[:24])
    if magic != (command ^ 0xFFFFFFFF):
        raise ADBProtocolError("invalid ADB magic")
    if len(data) != 24 + length:
        raise ADBProtocolError("invalid ADB payload length")
    payload = data[24:]
    if sum(payload) & 0xFFFFFFFF != checksum:
        raise ADBProtocolError("invalid ADB payload checksum")
    return ADBFrame(command, arg0, arg1, payload)
