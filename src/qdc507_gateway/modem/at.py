from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple


TERMINALS = {
    "OK", "ERROR", "NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE", "NO DIAL TONE",
    "NO_DEVICE", "NO DEVICE", "NOT_CONNECTED", "NOT CONNECTED", "DETACHED", ">",
}
URC_PREFIXES = ("+CMTI:", "+CMT:", "+CDS:", "+CLIP:", "+CRING", "+CIEV:", "+QMTSTAT:")
URC_LINES = {"RING"}


def _is_timeout_error(error: Exception) -> bool:
    return "timeout" in type(error).__name__.lower()


def classify_terminal_line(line: str) -> Optional[str]:
    value = line.strip().upper()
    if value.startswith(">"):
        return ">"
    if value in TERMINALS or value.startswith("+CME ERROR:") or value.startswith("+CMS ERROR:"):
        return value
    return None


@dataclass(frozen=True)
class ATResponse:
    lines: Tuple[str, ...]
    terminal: Optional[str]

    @property
    def ok(self) -> bool:
        return self.terminal == "OK"


class ATSession:
    """Framing-only AT session; transport I/O is supplied by the caller."""

    def __init__(self, write: Callable[[bytes], None], read: Callable[[float], bytes]):
        self.write = write
        self.read = read
        self._buffer = bytearray()
        self.urcs: List[str] = []
        self._pending_direct_sms_pdu = False

    def _route_urc(self, line: str) -> bool:
        """Keep unsolicited notifications out of in-flight command responses."""
        upper = line.upper()
        if self._pending_direct_sms_pdu:
            self._pending_direct_sms_pdu = False
            if re.fullmatch(r"[0-9A-F]{20,}", upper):
                self.urcs.append(line)
                return True
        if upper.startswith("+CMT:"):
            self.urcs.append(line)
            self._pending_direct_sms_pdu = True
            return True
        if upper in URC_LINES or upper.startswith(URC_PREFIXES):
            self.urcs.append(line)
            return True
        return False

    def drain_urcs(self) -> List[str]:
        lines = self.urcs[:]
        self.urcs.clear()
        return lines

    def feed(self, data: bytes) -> List[str]:
        self._buffer.extend(data)
        lines = []
        while b"\n" in self._buffer:
            raw, _, remaining = self._buffer.partition(b"\n")
            self._buffer = bytearray(remaining)
            line = raw.rstrip(b"\r").decode("utf-8", "replace").strip()
            if line:
                lines.append(line)
        prompt = bytes(self._buffer).lstrip(b" \t\r")
        if prompt.startswith(b">"):
            consumed = len(self._buffer) - len(prompt) + 1
            while consumed < len(self._buffer) and self._buffer[consumed] in b" \t":
                consumed += 1
            self._buffer = self._buffer[consumed:]
            lines.append(">")
        return lines

    def _read_response(self, timeout: float = 3.0) -> ATResponse:
        lines: List[str] = []
        remaining_time = timeout
        while remaining_time > 0:
            read_timeout = min(0.2, remaining_time)
            try:
                chunk = self.read(read_timeout)
            except Exception as exc:
                if not _is_timeout_error(exc):
                    raise
                chunk = b""
            remaining_time -= read_timeout
            for line in self.feed(chunk):
                terminal = classify_terminal_line(line)
                if terminal:
                    return ATResponse(tuple(lines), terminal)
                if not self._route_urc(line):
                    lines.append(line)
        return ATResponse(tuple(lines), None)

    def command(self, command: str, timeout: float = 3.0) -> ATResponse:
        if not isinstance(command, str):
            raise ValueError("AT command contains an invalid control character")
        command = command.rstrip("\r")
        if not command.strip() or any(ord(character) < 0x20 for character in command):
            raise ValueError("AT command contains an invalid control character")
        self.write((command + "\r").encode("ascii"))
        return self._read_response(timeout)

    def send_pdu(self, pdu: str, tpdu_length: int, timeout: float = 30.0) -> ATResponse:
        """Send one SMS PDU after the modem's ``>`` prompt."""
        prompt = self.command(f"AT+CMGS={int(tpdu_length)}", timeout=min(5.0, timeout))
        if prompt.terminal != ">":
            return prompt
        self.write(pdu.encode("ascii") + b"\x1a")
        return self._read_response(timeout)
