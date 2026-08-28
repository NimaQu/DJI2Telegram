import datetime as dt

from qdc507_gateway.modem.sms import (
    SMSAssembler,
    SMSIngress,
    SMSPart,
    _decode_gsm7,
    decode_deliver,
    encode_gsm7,
    encode_sms,
    encode_ucs2,
)
from qdc507_gateway.storage.database import Database


def test_ucs2_short_sms_submit():
    segments = encode_ucs2("+8613800138000", "测试")
    assert len(segments) == 1
    assert segments[0].pdu.startswith("0001")
    assert segments[0].tpdu_length == 18


def test_decode_ucs2_deliver():
    # SMS-DELIVER, sender +8613800138000, body "测试".
    pdu = "00040D91683108108300F0000862805221436500046D4B8BD5"
    decoded = decode_deliver(pdu)
    assert decoded.sender.startswith("+8613800138000")
    assert decoded.body == "测试"


def test_sms_encoder_selects_gsm7_and_segments_long_messages():
    short = encode_sms("+12045550100", "hello")
    assert len(short) == 1
    assert short[0].pdu.startswith("0001")

    long = encode_sms("+12045550100", "a" * 161, reference=7)
    assert len(long) == 2
    assert all(segment.total == 2 for segment in long)
    assert all(segment.pdu.startswith("0041") for segment in long)
    # After the SMS-SUBMIT first octet and message reference, the address
    # length is the next octet; there is no extra placeholder byte.
    assert long[0].pdu[4:8] == "000B"

    unicode = encode_sms("+12045550100", "测试")
    assert unicode[0].pdu.startswith("0001")
    assert "08" in unicode[0].pdu


def test_sms_encoder_rejects_invalid_concat_reference():
    for reference in (-1, 256, True):
        try:
            encode_sms("+12045550100", "hello", reference=reference)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid concatenation reference accepted")


def test_gsm7_submit_keeps_extension_pairs_and_destination_alignment():
    body = "^{}\\[]~|€" * 30
    segments = encode_gsm7("+12045550100", body, reference=7)
    decoded = []
    for segment in segments:
        tpdu = bytes.fromhex(segment.pdu)[1:]
        destination_length = tpdu[2]
        destination_octets = (destination_length + 1) // 2
        udl_offset = 2 + 1 + 1 + destination_octets + 2
        udl = tpdu[udl_offset]
        user_data = tpdu[udl_offset + 1:]
        udh_octets = user_data[0] + 1
        header_septets = (udh_octets * 8 + 6) // 7
        fill_bits = (-udh_octets * 8) % 7
        decoded.append(_decode_gsm7(
            user_data,
            udl - header_septets,
            udh_octets * 8 + fill_bits,
        ))
    assert "".join(decoded) == body


def test_ucs2_submit_does_not_split_surrogate_pair():
    body = "a" * 66 + "😀" + "b" * 65
    segments = encode_ucs2("+12045550100", body)
    assert len(segments) == 2
    payloads = []
    for segment in segments:
        tpdu = bytes.fromhex(segment.pdu)[1:]
        destination_length = tpdu[2]
        destination_octets = (destination_length + 1) // 2
        udl_offset = 2 + 1 + 1 + destination_octets + 2
        user_data = tpdu[udl_offset + 1:]
        payloads.append(user_data[user_data[0] + 1:])
    assert b"".join(payloads).decode("utf-16-be") == body


def test_sms_assembler_accepts_out_of_order_parts_and_expires_groups():
    now = [100.0]
    assembler = SMSAssembler(ttl_seconds=10, clock=lambda: now[0])
    first = SMSPart("+12045550100", "hello ", None, "PDU1", 7, 2, 1)
    second = SMSPart("+12045550100", "world", None, "PDU2", 7, 2, 2)
    assert assembler.add(second) is None
    assembled = assembler.add(first)
    assert assembled is not None
    assert assembled.body == "hello world"
    assert assembled.raw_pdu.splitlines() == ["PDU1", "PDU2"]

    assert assembler.add(first) is None
    now[0] = 111.0
    assert assembler.add(second) is None


def test_sms_ingress_deduplicates_pdu_and_persists_message():
    database = Database(":memory:")
    def fixed():
        return dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

    ingress = SMSIngress(database, clock=fixed)
    pdu = "00040D91683108108300F0000862805221436500046D4B8BD5"
    message = ingress.ingest(pdu)
    assert message is not None
    assert message["body"] == "测试"
    assert ingress.ingest(pdu) is None
    assert len(database.list_sms()) == 1
    assert database.connection.execute("SELECT COUNT(*) FROM sms_pdu_dedup").fetchone()[0] == 1
