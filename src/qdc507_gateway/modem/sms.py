from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


class SMSPDUError(ValueError):
    pass


@dataclass(frozen=True)
class SMSPart:
    sender: str
    body: str
    timestamp: Optional[dt.datetime]
    raw_pdu: str
    concat_reference: Optional[int] = None
    concat_total: Optional[int] = None
    concat_sequence: Optional[int] = None


@dataclass(frozen=True)
class SMSSubmitSegment:
    pdu: str
    tpdu_length: int
    sequence: int
    total: int


class SMSAssembler:
    """Bounded, out-of-order concatenated SMS assembler.

    PDU de-duplication is deliberately kept in SQLite. This class only owns
    the short-lived in-memory assembly state, so a restart cannot replay a
    previously persisted PDU into a new message.
    """

    def __init__(self, ttl_seconds: float = 300.0, max_groups: int = 128,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = max(1.0, ttl_seconds)
        self.max_groups = max(1, max_groups)
        self.clock = clock
        self._groups: Dict[tuple[str, int, int], tuple[float, Dict[int, SMSPart]]] = {}

    def add(self, part: SMSPart) -> Optional[SMSPart]:
        if part.concat_reference is None or part.concat_total is None or part.concat_sequence is None:
            return part
        if part.concat_total < 1 or not 1 <= part.concat_sequence <= part.concat_total:
            raise SMSPDUError("invalid concatenated SMS metadata")

        now = self.clock()
        self._purge(now)
        key = (part.sender, part.concat_reference, part.concat_total)
        if key not in self._groups and len(self._groups) >= self.max_groups:
            oldest = min(self._groups, key=lambda item: self._groups[item][0])
            del self._groups[oldest]
        _expires_at, parts = self._groups.setdefault(key, (now + self.ttl_seconds, {}))
        parts[part.concat_sequence] = part
        if len(parts) != part.concat_total:
            return None
        if any(sequence not in parts for sequence in range(1, part.concat_total + 1)):
            return None

        ordered = [parts[sequence] for sequence in range(1, part.concat_total + 1)]
        del self._groups[key]
        timestamp = next((item.timestamp for item in ordered if item.timestamp is not None), None)
        return SMSPart(
            sender=part.sender,
            body="".join(item.body for item in ordered),
            timestamp=timestamp,
            raw_pdu="\n".join(item.raw_pdu for item in ordered),
            concat_reference=part.concat_reference,
            concat_total=part.concat_total,
            concat_sequence=None,
        )

    def _purge(self, now: float) -> None:
        expired = [key for key, (expires_at, _) in self._groups.items() if expires_at <= now]
        for key in expired:
            del self._groups[key]


class SMSIngress:
    """Decode, de-duplicate, assemble and persist inbound SMS PDUs."""

    def __init__(self, database, assembler: Optional[SMSAssembler] = None,
                 clock: Callable[[], dt.datetime] | None = None):
        self.database = database
        self.assembler = assembler or SMSAssembler()
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def ingest(self, pdu: str) -> Optional[dict[str, object]]:
        part = decode_deliver(pdu)
        received_at = self.clock().isoformat()
        pdu_hash = hashlib.sha256(part.raw_pdu.encode("ascii")).hexdigest()
        if not self.database.record_sms_pdu(
            pdu_hash,
            received_at,
            sender=part.sender,
            concat_reference=part.concat_reference,
            concat_sequence=part.concat_sequence,
        ):
            return None

        message_part = self.assembler.add(part)
        if message_part is None:
            return None
        raw_pdus = message_part.raw_pdu.splitlines()
        message = {
            "id": "sms-" + hashlib.sha256(message_part.raw_pdu.encode("ascii")).hexdigest(),
            "sender": message_part.sender,
            "body": message_part.body,
            "timestamp": (message_part.timestamp or self.clock()).isoformat(),
            "is_read": False,
            "raw_pdus": json.dumps(raw_pdus),
        }
        self.database.save_sms(message)
        return message


_GSM7_ALPHABET = "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
_GSM7_EXTENSION = {"^": 0x14, "{": 0x28, "}": 0x29, "\\": 0x2F, "[": 0x3C, "~": 0x3D, "]": 0x3E, "|": 0x40, "€": 0x65}


def _semi_octets(digits: str) -> bytes:
    values = [ord(char) - 48 for char in digits]
    output = bytearray()
    for index in range(0, len(values), 2):
        low = values[index]
        high = values[index + 1] if index + 1 < len(values) else 0xF
        output.append(low | (high << 4))
    return bytes(output)


def _concat_reference(reference: Optional[int]) -> int:
    if reference is None:
        return secrets.randbelow(256)
    if isinstance(reference, bool) or not isinstance(reference, int) or not 0 <= reference <= 255:
        raise SMSPDUError("concatenation reference must be an 8-bit integer")
    return reference


def encode_ucs2(destination: str, body: str, reference: Optional[int] = None) -> List[SMSSubmitSegment]:
    normalized = destination.strip().replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\+?[0-9]{1,20}", normalized):
        raise SMSPDUError("invalid destination")
    if not body:
        raise SMSPDUError("empty body")
    reference = _concat_reference(reference)
    international = normalized.startswith("+")
    digits = normalized[1:] if international else normalized
    encoded = body.encode("utf-16-be")
    units = [encoded[index:index + 2] for index in range(0, len(encoded), 2)]
    unit_limit = 70 if len(units) <= 70 else 67
    unit_chunks = []
    index = 0
    while index < len(units):
        end = min(len(units), index + unit_limit)
        # Keep a UTF-16 surrogate pair in one segment. Splitting it would
        # produce replacement characters in otherwise valid UCS2 SMS text.
        if end < len(units) and end > index:
            previous = int.from_bytes(units[end - 1], "big")
            following = int.from_bytes(units[end], "big")
            if 0xD800 <= previous <= 0xDBFF and 0xDC00 <= following <= 0xDFFF:
                end -= 1
        if end == index:
            end = min(len(units), index + unit_limit)
        unit_chunks.append(units[index:end])
        index = end
    chunks = [b"".join(chunk) for chunk in unit_chunks]
    total = len(chunks)
    if total > 255:
        raise SMSPDUError("message is too long")
    result = []
    for index, chunk in enumerate(chunks, 1):
        udh = bytes([0x05, 0x00, 0x03, reference & 0xFF, total, index]) if total > 1 else b""
        first_octet = 0x41 if total > 1 else 0x01
        tpdu = bytearray([first_octet, 0x00, len(digits), 0x91 if international else 0x81])
        tpdu.extend(_semi_octets(digits))
        tpdu.extend([0x00, 0x08, len(udh) + len(chunk)])
        tpdu.extend(udh)
        tpdu.extend(chunk)
        pdu = bytes([0]) + bytes(tpdu)
        result.append(SMSSubmitSegment(pdu.hex().upper(), len(tpdu), index, total))
    return result


def _destination_tpdu(destination: str) -> bytearray:
    normalized = destination.strip().replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\+?[0-9]{1,20}", normalized):
        raise SMSPDUError("invalid destination")
    international = normalized.startswith("+")
    digits = normalized[1:] if international else normalized
    # SMS-SUBMIT already wrote the first octet and message reference before
    # appending the destination address.  Do not add a third leading octet:
    # the address starts with its digit length, followed by the type-of-number.
    return bytearray([len(digits), 0x91 if international else 0x81]) + bytearray(_semi_octets(digits))


def _gsm7_codes(body: str) -> List[int]:
    codes = []
    for char in body:
        if char in _GSM7_EXTENSION:
            codes.extend((0x1B, _GSM7_EXTENSION[char]))
        else:
            index = _GSM7_ALPHABET.find(char)
            if index < 0:
                raise SMSPDUError("body is not encodable as GSM7")
            codes.append(index)
    return codes


def _pack_gsm7(codes: List[int], header: bytes = b"") -> bytes:
    header_bits = len(header) * 8
    fill_bits = (-header_bits) % 7
    body_offset = header_bits + fill_bits
    total_bits = body_offset + len(codes) * 7
    output = bytearray((total_bits + 7) // 8)
    output[:len(header)] = header
    for index, code in enumerate(codes):
        for bit in range(7):
            if code & (1 << bit):
                bit_index = body_offset + index * 7 + bit
                output[bit_index // 8] |= 1 << (bit_index % 8)
    return bytes(output)


def _split_gsm7_codes(codes: List[int], limit: int) -> List[List[int]]:
    """Split septets without leaving an ESC extension code at a boundary."""
    chunks = []
    index = 0
    while index < len(codes):
        end = min(len(codes), index + limit)
        if end < len(codes) and end > index and codes[end - 1] == 0x1B:
            end -= 1
        if end == index:
            # ``limit`` is larger than an ESC pair in all supported SMS
            # layouts, but retain progress if a future caller changes it.
            end = min(len(codes), index + limit)
        chunks.append(codes[index:end])
        index = end
    return chunks


def encode_gsm7(destination: str, body: str, reference: Optional[int] = None) -> List[SMSSubmitSegment]:
    if not body:
        raise SMSPDUError("empty body")
    reference = _concat_reference(reference)
    codes = _gsm7_codes(body)
    unit_limit = 160 if len(codes) <= 160 else 153
    chunks = _split_gsm7_codes(codes, unit_limit)
    total = len(chunks)
    result = []
    for index, chunk in enumerate(chunks, 1):
        udh = bytes([0x05, 0x00, 0x03, reference & 0xFF, total, index]) if total > 1 else b""
        tpdu = bytearray([0x41 if total > 1 else 0x01, 0x00])
        tpdu.extend(_destination_tpdu(destination))
        tpdu.extend([0x00, 0x00])
        tpdu.append(((len(udh) * 8 + 6) // 7) + len(chunk))
        tpdu.extend(_pack_gsm7(chunk, udh))
        result.append(SMSSubmitSegment(
            (bytes([0]) + bytes(tpdu)).hex().upper(), len(tpdu), index, total,
        ))
    return result


def encode_sms(destination: str, body: str, reference: Optional[int] = None) -> List[SMSSubmitSegment]:
    """Choose GSM7 where possible, otherwise use UCS2."""
    try:
        _gsm7_codes(body)
    except SMSPDUError:
        return encode_ucs2(destination, body, reference)
    return encode_gsm7(destination, body, reference)


def _decode_number(data: bytes, digits: int, type_of_address: int) -> str:
    value = ""
    for byte in data:
        value += str(byte & 0x0F)
        value += str((byte >> 4) & 0x0F)
    value = value[:digits]
    if type_of_address & 0x70 == 0x50:
        return value
    return ("+" if type_of_address & 0x70 == 0x10 else "") + value


def _decode_gsm7(data: bytes, septets: int, bit_offset: int = 0) -> str:
    bits = int.from_bytes(data, "little") >> bit_offset
    extension = {value: key for key, value in _GSM7_EXTENSION.items()}
    codes = []
    for index in range(septets):
        code = (bits >> (index * 7)) & 0x7F
        codes.append(code)
    chars = []
    index = 0
    while index < len(codes):
        code = codes[index]
        if code == 0x1B and index + 1 < len(codes):
            chars.append(extension.get(codes[index + 1], "�"))
            index += 2
            continue
        chars.append(_GSM7_ALPHABET[code] if code < len(_GSM7_ALPHABET) else "�")
        index += 1
    return "".join(chars)


def _decode_timestamp(data: bytes) -> Optional[dt.datetime]:
    if len(data) != 7:
        return None
    values = []
    for byte in data[:6]:
        values.append(int("%x%x" % (byte & 0x0F, (byte >> 4) & 0x0F)))
    year, month, day, hour, minute, second = values
    year += 2000 if year < 70 else 1900
    try:
        return dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def decode_deliver(pdu: str) -> SMSPart:
    cleaned = re.sub(r"\s+", "", pdu)
    try:
        data = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise SMSPDUError("invalid PDU hex") from exc
    if not data:
        raise SMSPDUError("empty PDU")
    cursor = data[0] + 1
    if cursor + 3 > len(data):
        raise SMSPDUError("truncated SMSC/TPDU")
    first_octet = data[cursor]
    cursor += 1
    if first_octet & 0x03 != 0:
        raise SMSPDUError("PDU is not SMS-DELIVER")
    sender_length = data[cursor]
    sender_type = data[cursor + 1]
    cursor += 2
    sender_bytes = (sender_length + 1) // 2
    if cursor + sender_bytes + 2 + 7 + 1 > len(data):
        raise SMSPDUError("truncated SMS-DELIVER header")
    sender = _decode_number(data[cursor:cursor + sender_bytes], sender_length, sender_type)
    cursor += sender_bytes
    cursor += 1  # PID
    dcs = data[cursor]
    cursor += 1
    if cursor + 8 > len(data):
        raise SMSPDUError("truncated SMS timestamp")
    timestamp = _decode_timestamp(data[cursor:cursor + 7])
    cursor += 7
    user_data_length = data[cursor]
    cursor += 1
    has_udh = bool(first_octet & 0x40)
    udh_bytes = 0
    user_data_start = cursor
    reference = total = sequence = None
    if has_udh:
        if cursor >= len(data):
            raise SMSPDUError("truncated SMS user-data header")
        udh_length = data[cursor]
        udh_end = cursor + 1 + udh_length
        if udh_end > len(data):
            raise SMSPDUError("truncated SMS user-data header")
        cursor += 1
        while cursor < udh_end:
            if cursor + 2 > udh_end:
                raise SMSPDUError("truncated SMS information element")
            tag, length = data[cursor], data[cursor + 1]
            if cursor + 2 + length > udh_end:
                raise SMSPDUError("truncated SMS information element")
            value = data[cursor + 2:cursor + 2 + length]
            if tag == 0x00 and length == 3:
                reference, total, sequence = value
            elif tag == 0x08 and length == 4:
                reference = (value[0] << 8) | value[1]
                total, sequence = value[2], value[3]
            cursor += 2 + length
        udh_bytes = 1 + udh_length
    if dcs & 0x0C == 0x08:
        required_octets = user_data_length
    elif dcs & 0x0C == 0x04:
        required_octets = user_data_length
    else:
        required_octets = (user_data_length * 7 + 7) // 8
    if user_data_start + required_octets > len(data):
        raise SMSPDUError("truncated SMS user data")
    payload = data[user_data_start:user_data_start + required_octets]
    if dcs & 0x0C == 0x08:
        if udh_bytes > len(payload):
            raise SMSPDUError("invalid UCS2 user-data header")
        body = payload[udh_bytes:].decode("utf-16-be", "replace")
    elif dcs & 0x0C == 0x04:
        if udh_bytes > len(payload):
            raise SMSPDUError("invalid binary user-data header")
        body = "[binary] " + payload[udh_bytes:].hex().upper()
    else:
        header_septets = (udh_bytes * 8 + 6) // 7 if has_udh else 0
        header_bits = udh_bytes * 8
        fill_bits = (-header_bits) % 7 if has_udh else 0
        body = _decode_gsm7(
            payload,
            max(0, user_data_length - header_septets),
            header_bits + fill_bits,
        )
    return SMSPart(sender, body, timestamp, cleaned.upper(), reference, total, sequence)
