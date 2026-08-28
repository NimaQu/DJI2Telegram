import pytest

from qdc507_gateway.adb.transport import shell_service
from qdc507_gateway.modem.at import ATSession
from qdc507_gateway.modem.transport import choose_at_interface
from qdc507_gateway.models import EndpointDescriptor, InterfaceDescriptor, USBDeviceSnapshot
from qdc507_gateway.telegram.compatibility import (
    probe_kurigram_client,
    probe_kurigram_raw_phone_types,
    probe_pytgcalls_bridge,
)
from qdc507_gateway.usb.owner import DeviceOwnerError, DeviceOwnerLock


def test_at_response_keeps_qcfg_and_routes_known_urc():
    writes = []
    chunks = iter([b'+QCFG: "usbnet",1\r\n', b'+CMTI: "SM",1\r\nOK\r\n'])
    session = ATSession(writes.append, lambda _: next(chunks, b""))
    response = session.command("AT+QCFG=\"usbnet\"")
    assert response.lines == ('+QCFG: "usbnet",1',)
    assert session.urcs == ['+CMTI: "SM",1']
    assert writes == [b'AT+QCFG="usbnet"\r']


def test_at_sms_pdu_waits_for_prompt_then_final_ok():
    writes = []
    chunks = iter([b"\r\n> ", b"\r\n+CMGS: 1\r\nOK\r\n"])
    session = ATSession(writes.append, lambda _: next(chunks, b""))
    response = session.send_pdu("001122", 3)
    assert response.ok
    assert writes == [b"AT+CMGS=3\r", b"001122\x1a"]


def test_at_response_tolerates_usb_read_timeouts_and_routes_standard_ring():
    writes = []

    class USBErrorTimeout(Exception):
        pass

    chunks = iter((USBErrorTimeout(), b"RING\r\n", b"OK\r\n"))

    def read(_timeout):
        item = next(chunks)
        if isinstance(item, Exception):
            raise item
        return item

    session = ATSession(writes.append, read)
    response = session.command("AT")
    assert response.ok
    assert session.urcs == ["RING"]
    assert writes == [b"AT\r"]


def test_at_interface_selection_uses_descriptor_then_known_hint():
    pair = (EndpointDescriptor(0x81, "in", "bulk", 512), EndpointDescriptor(1, "out", "bulk", 512))
    device = USBDeviceSnapshot(0x2C7C, 0x0125, interfaces=(
        InterfaceDescriptor(2, 255, 255, 255, pair),
        InterfaceDescriptor(6, 255, 66, 1, pair),
    ))
    assert choose_at_interface(device).number == 2


def test_telegram_compatibility_boundaries():
    class Client:
        async def send_message(self, *_): pass
        async def resolve_peer(self, *_): pass

    assert probe_kurigram_client(Client()).passed
    assert not probe_kurigram_client(object()).passed
    assert probe_pytgcalls_bridge(type("Bridge", (), {
        name: (lambda self, *args, **kwargs: None)
        for name in ("request_call", "accept_call", "confirm_call", "send_signaling", "discard_call")
    })()).passed


def test_raw_phone_probe_is_callable_without_login():
    report = probe_kurigram_raw_phone_types()
    assert report.passed


def test_adb_shell_service_rejects_nul():
    assert shell_service("id").startswith(b"shell:id")
    try:
        shell_service("id\0whoami")
    except ValueError:
        pass
    else:
        raise AssertionError("NUL-containing shell command accepted")


def test_device_owner_lock_is_exclusive(tmp_path):
    path = tmp_path / "owner.lock"
    first = DeviceOwnerLock(path)
    second = DeviceOwnerLock(path)
    first.acquire()
    try:
        with pytest.raises(DeviceOwnerError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
