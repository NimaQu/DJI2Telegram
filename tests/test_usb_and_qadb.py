from pathlib import Path

from qdc507_gateway.models import EndpointDescriptor, InterfaceDescriptor, USBDeviceSnapshot
from qdc507_gateway.modem.qadbkey import authorize_qadbkey, derive_password, parse_challenge
from qdc507_gateway.modem.usbcfg import (
    PersistentConfigError,
    apply_usbcfg_once,
    parse_usb_configuration,
    parse_usbcfg_command,
)
from qdc507_gateway.usb.descriptors import JSONDeviceLocator, snapshot_from_mapping
from qdc507_gateway.usb.live import LibUSBDeviceSession
from qdc507_gateway.usb.owner import DeviceOwnerLock
from qdc507_gateway.modem.transport import LibUSBBulkTransport


def bulk_pair(in_address=0x81, out_address=0x01):
    return (
        EndpointDescriptor(in_address, "in", "bulk", 512),
        EndpointDescriptor(out_address, "out", "bulk", 512),
    )


def test_adb_requires_descriptor_signature_not_interface_number():
    legacy_audio = InterfaceDescriptor(6, 1, 1, 0, bulk_pair())
    assert not legacy_audio.is_adb
    assert not legacy_audio.is_uac

    adb = InterfaceDescriptor(6, 0xFF, 0x42, 0x01, bulk_pair())
    device = USBDeviceSnapshot(0x2C7C, 0x0125, interfaces=(legacy_audio, adb))
    assert [item.number for item in device.adb_interfaces] == [6]


def test_qadbkey_vector_and_strict_challenge():
    assert derive_password("12345678") == "0jXKXQwSwMxYoeg"
    assert parse_challenge("AT+QADBKEY?\r\n+QADBKEY: 12345678\r\nOK\r\n") == "12345678"

    try:
        parse_challenge("+QADBKEY: 12345678\r\n+QADBKEY: 87654321\r\nOK")
    except ValueError:
        pass
    else:
        raise AssertionError("ambiguous QADBKEY challenge accepted")


def test_qadbkey_authorization_checks_final_ok_without_returning_secret():
    class FakeSession:
        def __init__(self):
            self.commands = []

        def command(self, command):
            self.commands.append(command)
            if command == "AT+QADBKEY?":
                return type("Response", (), {"lines": ("+QADBKEY: 12345678",), "terminal": "OK", "ok": True})()
            return type("Response", (), {"lines": (), "terminal": "OK", "ok": True})()

    session = FakeSession()
    result = authorize_qadbkey(session)
    assert result.confirmed
    assert session.commands[0] == "AT+QADBKEY?"
    assert session.commands[1] == 'AT+QADBKEY="0jXKXQwSwMxYoeg"'


def test_usbcfg_distinguishes_legacy_and_full_target():
    legacy = parse_usb_configuration('+QCFG: "usbcfg",0x2C7C,0x0125,1,1,1,1,1,0,1\r\nOK')
    full = parse_usb_configuration('+QCFG: "usbcfg",0x2C7C,0x0125,1,1,1,1,1,1,1\r\nOK')
    assert legacy is not None and legacy.is_legacy_uac_target and not legacy.is_full_target
    assert full is not None and full.is_full_target


def test_usbcfg_command_is_parsed_before_any_write():
    target = parse_usbcfg_command(
        'AT+QCFG="USBCFG",0x2C7C,0x0125,1,1,1,1,1,1,1'
    )
    assert target is not None and target.is_full_target
    assert parse_usbcfg_command('AT+QCFG="USBCFG",0x2C7C,0x0125,1,1') is None


def test_usbcfg_change_requires_confirmation_and_reconnects_once():
    legacy = parse_usb_configuration('+QCFG: "usbcfg",0x2C7C,0x0125,1,1,1,1,1,0,1')
    target = parse_usb_configuration('+QCFG: "usbcfg",0x2C7C,0x0125,1,1,1,1,1,1,1')
    assert legacy is not None and target is not None
    calls = []

    def readback():
        return legacy

    try:
        apply_usbcfg_once(
            readback=readback, write=lambda command: calls.append(command),
            close_handle=lambda: calls.append("close"), wait_for_same_device=lambda: "device",
            readback_after_reconnect=lambda _: target, target=target,
        )
    except PersistentConfigError:
        pass
    else:
        raise AssertionError("persistent USBCFG change without confirmation accepted")
    assert calls == []

    result = apply_usbcfg_once(
        readback=readback,
        write=lambda command: calls.append(("write", command)) or type("Response", (), {"terminal": "DETACHED"})(),
        close_handle=lambda: calls.append("close"),
        wait_for_same_device=lambda: calls.append("reenumerate") or "device",
        readback_after_reconnect=lambda _: target,
        target=target,
        confirm_persistent=True,
    )
    assert result.reenumerated
    assert [item[0] if isinstance(item, tuple) else item for item in calls] == ["write", "close", "reenumerate"]


def test_json_probe_fixture_classifies_complete_device():
    fixture = Path(__file__).parent / "fixtures" / "complete.json"
    devices = JSONDeviceLocator(str(fixture)).find()
    assert len(devices) == 1
    assert devices[0].adb_interfaces[0].number == 6
    assert [item.number for item in devices[0].uac_interfaces] == [7, 8, 9]
    assert {item.address for item in devices[0].uac_audio_endpoints} == {0x87, 0x08}
    assert devices[0].at_candidates[0].number == 2


def test_live_mapping_masks_endpoint_bm_attributes_to_transfer_type():
    device = snapshot_from_mapping({
        "vendor_id": "0x2C7C",
        "product_id": "0x0125",
        "interfaces": [{
            "number": 2,
            "class": 255,
            "endpoints": [
                {"address": "0x84", "bmAttributes": 0x82, "wMaxPacketSize": 512},
                {"address": "0x03", "bmAttributes": 0x02, "wMaxPacketSize": 512},
            ],
        }],
    })
    assert device.at_candidates[0].endpoints[0].transfer_type == "bulk"


def test_legacy_audio_fixture_keeps_interface_six_out_of_qdc507_uac():
    fixture = Path(__file__).parent / "fixtures" / "legacy-uac.json"
    device = JSONDeviceLocator(str(fixture)).find()[0]
    assert [item.number for item in device.uac_interfaces] == [7, 8, 9]
    assert device.uac_audio_endpoints == ()


def test_live_session_opens_handle_without_claim_until_at_is_requested(tmp_path):
    pair = (bulk_pair(0x84, 0x03),)

    class Setting:
        def getNumber(self):
            return 2
        def getClass(self):
            return 255
        def getSubClass(self):
            return 255
        def getProtocol(self):
            return 255
        def iterEndpoints(self):
            return iter(pair)

    class Endpoint:
        def __init__(self, address):
            self.address = address
        def getAddress(self):
            return self.address
        def getAttributes(self):
            return 2
        def getMaxPacketSize(self):
            return 512
        def getInterval(self):
            return 0

    class Handle:
        def __init__(self):
            self.claimed = []
            self.released = []
            self.closed = False
        def claimInterface(self, number):
            self.claimed.append(number)
        def releaseInterface(self, number):
            self.released.append(number)
        def close(self):
            self.closed = True

    class Device:
        def __init__(self):
            self.handle = Handle()
        def getVendorID(self):
            return 0x2C7C
        def getProductID(self):
            return 0x0125
        def getBusNumber(self):
            return 1
        def getDeviceAddress(self):
            return 2
        def getPortNumberList(self):
            return (1, 2)
        def getSerialNumber(self):
            return None
        def iterSettings(self):
            return iter((Setting(),))
        def open(self):
            return self.handle

    class Context:
        def __init__(self, device):
            self.device = device
            self.closed = False
        def getDeviceList(self, skip_on_error=True):
            return (self.device,)
        def close(self):
            self.closed = True

    # Replace the one endpoint tuple with endpoint objects after the descriptor fixture is defined.
    pair = (Endpoint(0x84), Endpoint(0x03))
    device = Device()
    context = Context(device)
    with LibUSBDeviceSession(context=context, owner=DeviceOwnerLock(tmp_path / "owner.lock")) as session:
        assert session.snapshot is not None
        assert device.handle.claimed == []
        session.open_at()
        assert device.handle.claimed == [2]
    assert device.handle.released == [2]
    assert device.handle.closed
    assert context.closed is False


def test_bulk_transport_restores_kernel_driver_after_claim(tmp_path):
    events = []

    class Handle:
        def kernelDriverActive(self, number):
            return True

        def detachKernelDriver(self, number):
            events.append(("detach", number))

        def attachKernelDriver(self, number):
            events.append(("attach", number))

        def claimInterface(self, number):
            events.append(("claim", number))

        def releaseInterface(self, number):
            events.append(("release", number))

    interface = InterfaceDescriptor(2, 255, 0, 0, bulk_pair(0x84, 0x03))
    transport = LibUSBBulkTransport(
        Handle(), interface, owner=DeviceOwnerLock(tmp_path / "owner.lock"), close_handle=False,
    )
    transport.open()
    transport.close()
    assert events == [("detach", 2), ("claim", 2), ("release", 2), ("attach", 2)]
