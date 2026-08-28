from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


class QADBKeyError(ValueError):
    pass


@dataclass(frozen=True)
class QADBKeyAuthorization:
    """Non-sensitive result; challenge and derived password are never retained."""

    confirmed: bool


_SECRET = b"SH_adb_quectel"
_ALPHABET = b"./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def parse_challenge(response: str) -> str:
    lines = [line.strip() for line in response.replace("\r", "\n").split("\n") if line.strip()]
    if not lines or lines[-1].upper() != "OK":
        raise QADBKeyError("QADBKEY query did not terminate with OK")
    if any("ERROR" in line.upper() for line in lines):
        raise QADBKeyError("QADBKEY is unsupported")
    values = []
    for line in lines:
        if line.upper().startswith("+QADBKEY:"):
            value = line.split(":", 1)[1].strip()
            if re.fullmatch(r"[0-9]{8}", value):
                values.append(value)
    if len(values) != 1:
        raise QADBKeyError("QADBKEY response must contain one 8-digit challenge")
    return values[0]


def _crypt(password: bytes, salt: bytes) -> bytes:
    alternate = hashlib.md5(password + salt + password).digest()
    initial = password + b"$1$" + salt
    remaining = len(password)
    while remaining > 0:
        count = min(remaining, len(alternate))
        initial += alternate[:count]
        remaining -= len(alternate)
    count = len(password)
    while count > 0:
        initial += b"\0" if count & 1 else password[:1]
        count >>= 1
    digest = hashlib.md5(initial).digest()
    for round_number in range(1000):
        current = password if round_number & 1 else digest
        if round_number % 3:
            current += salt
        if round_number % 7:
            current += password
        current += digest if round_number & 1 else password
        digest = hashlib.md5(current).digest()

    output = bytearray()

    def append(high: int, middle: int, low: int, count: int) -> None:
        value = (high << 16) | (middle << 8) | low
        for _ in range(count):
            output.append(_ALPHABET[value & 0x3F])
            value >>= 6

    append(digest[0], digest[6], digest[12], 4)
    append(digest[1], digest[7], digest[13], 4)
    append(digest[2], digest[8], digest[14], 4)
    append(digest[3], digest[9], digest[15], 4)
    append(digest[4], digest[10], digest[5], 4)
    append(0, 0, digest[11], 2)
    return bytes(output)


def derive_password(challenge: str) -> str:
    if not re.fullmatch(r"[0-9]{8}", challenge):
        raise QADBKeyError("challenge must be exactly eight digits")
    return _crypt(_SECRET, challenge.encode("ascii"))[:15].decode("ascii")


def authorize_qadbkey(session: Any) -> QADBKeyAuthorization:
    """Perform the challenge/response exchange without exposing its secret.

    ``session`` only needs the existing ATSession ``command`` method. The
    derived password is kept in a local variable and is never returned,
    persisted, or included in an exception message.
    """
    query = session.command("AT+QADBKEY?")
    query_text = "\n".join(query.lines + ((query.terminal,) if query.terminal else ()))
    challenge = parse_challenge(query_text)
    # Quectel's string parameter must be quoted.  Without quotes some
    # firmware accepts the query but rejects the otherwise correct digest.
    response = session.command('AT+QADBKEY="' + derive_password(challenge) + '"')
    if not response.ok:
        raise QADBKeyError("QADBKEY authorization was not accepted")
    return QADBKeyAuthorization(confirmed=True)
