from dataclasses import replace

import pytest

from qdc507_gateway.events import EventBus
from qdc507_gateway.models import USBDeviceSnapshot
from qdc507_gateway.modem.service import ModuleServiceError
from qdc507_gateway.storage.database import Database
from qdc507_gateway.usb.setup import SetupModuleService


@pytest.fixture
def service():
    database = Database(":memory:")
    value = SetupModuleService(database, EventBus())
    yield value
    value.close()
    database.close()


def test_setup_follows_same_port_across_identity_change(service):
    original = USBDeviceSnapshot(0x2CA3, 0x4006, bus=1, address=2, port_path=(1,))
    target = replace(original, vendor_id=0x2C7C, product_id=0x0125, address=3)
    class Locator:
        devices = [original]
        def find(self, vid, pid):
            return [d for d in self.devices if (d.vendor_id, d.product_id) == (vid, pid)]
    locator = Locator()
    service.locator = locator
    assert service._discover() == original
    locator.devices = [target]
    assert service._wait_for_same_device(original) == target
    assert service.identity == "2C7C:0125"
    locator.devices = [replace(target, port_path=(2,))]
    with pytest.raises(ModuleServiceError, match="moved or changed"):
        service._discover()


def test_setup_rejects_ambiguous_modules(service):
    class Locator:
        def find(self, vid, pid):
            return [USBDeviceSnapshot(vid, pid, bus=1, address=2, port_path=(1,))]
    service.locator = Locator()
    with pytest.raises(ModuleServiceError, match="exactly one"):
        service._discover()


def test_setup_timeout_explains_vm_passthrough(service, monkeypatch):
    from qdc507_gateway.usb import setup
    clock = iter([0, 0, 91])
    monkeypatch.setattr(setup.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(setup.time, "sleep", lambda _: None)
    service._discover = lambda: None
    with pytest.raises(ModuleServiceError, match="KVM/PVE USB passthrough"):
        service._wait_for_same_device(USBDeviceSnapshot(0x2CA3, 0x4006))


def test_setup_usb_ok_is_staged_without_waiting_for_disconnect(service):
    from qdc507_gateway.modem.at import ATResponse
    from qdc507_gateway.module_setup import TARGET_USB_CONFIGURATION
    class AT:
        commands = []
        def command(self, command, timeout=5):
            self.commands.append(command)
            if command == 'AT+QCFG="USBCFG"':
                return ATResponse(('+QCFG: "usbcfg",0x2CA3,0x4006,1,1,1,1,1,0,1',), "OK")
            return ATResponse((), "OK")
    at = AT()
    class Session:
        closed = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.closed = True
        def open_at(self, handshake=False):
            return at
    session = Session()
    service._session = lambda: session
    def forbidden_wait(previous):
        raise AssertionError("no detach was observed")
    service._wait_for_same_device = forbidden_wait
    result = service._persistent_usbcfg(TARGET_USB_CONFIGURATION.command, 5000)
    assert result["changed"] and not result["reenumerated"]
    assert at.commands == ['AT+QCFG="USBCFG"', TARGET_USB_CONFIGURATION.command]
    assert session.closed
