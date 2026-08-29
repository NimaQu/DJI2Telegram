from __future__ import annotations

import asyncio

from qdc507_gateway.events import EventBus
from qdc507_gateway.models import EndpointDescriptor, InterfaceDescriptor, USBDeviceSnapshot
from qdc507_gateway.runtime import GatewayRuntime
from qdc507_gateway.storage.database import Database


def test_runtime_probe_updates_state_and_publishes_without_claiming_usb():
    pair = (EndpointDescriptor(0x81, "in", "bulk", 512), EndpointDescriptor(0x01, "out", "bulk", 512))
    device = USBDeviceSnapshot(
        0x2C7C,
        0x0125,
        interfaces=(InterfaceDescriptor(6, 0xFF, 0x42, 1, pair),),
    )

    class Locator:
        def find(self):
            return (device,)

    async def run():
        events = EventBus()
        runtime = GatewayRuntime(Locator(), Database(":memory:"), events, {"status": {}})
        result = await runtime.probe_once()
        assert result["found"]
        assert runtime.state["module"]["identity"] == "2C7C:0125"

    asyncio.run(run())


def test_runtime_stop_closes_locator_context():
    class Locator:
        def __init__(self):
            self.closed = False

        def find(self):
            return ()

        def close(self):
            self.closed = True

    async def run():
        locator = Locator()
        runtime = GatewayRuntime(locator, Database(":memory:"), EventBus(), {"status": {}})
        await runtime.stop()
        assert locator.closed

    asyncio.run(run())


def test_reenumeration_can_use_udev_event_waiter():
    from qdc507_gateway.usb.reenumeration import ReenumerationCoordinator

    class Locator:
        def find(self, *_args):
            return ()

    waits = []
    coordinator = ReenumerationCoordinator(
        Locator(),
        sleep=lambda _seconds: None,
        event_waiter=lambda timeout: waits.append(timeout) or False,
    )
    try:
        coordinator.wait_for_device(
            USBDeviceSnapshot(0x2C7C, 0x0125, port_path=(1,)),
            timeout=0.001,
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("missing USB device did not time out")
    assert waits


def test_reenumeration_waits_for_disconnect_before_accepting_same_device():
    from qdc507_gateway.usb.reenumeration import ReenumerationCoordinator

    previous = USBDeviceSnapshot(0x2C7C, 0x0125, port_path=(1, 1))
    rebound = USBDeviceSnapshot(0x2C7C, 0x0125, port_path=(1, 1), address=7)

    class Locator:
        def __init__(self):
            self.states = iter(((previous,), (), (rebound,)))

        def find(self, *_args):
            return next(self.states)

    coordinator = ReenumerationCoordinator(
        Locator(),
        sleep=lambda _seconds: None,
    )
    result = coordinator.wait_for_device(previous, timeout=1.0)
    assert result is rebound


def test_udev_monitor_filters_to_qdc507_without_opening_usb():
    from qdc507_gateway.usb.udev import PyUdevUSBMonitor

    monitor = object.__new__(PyUdevUSBMonitor)
    monitor.vendor_id = "2c7c"
    monitor.product_id = "0125"

    class Device:
        def __init__(self, values):
            self.values = values

        def get(self, name):
            return self.values[name]

    assert monitor._is_qdc507(Device({"ID_VENDOR_ID": "2c7c", "ID_MODEL_ID": "0125"}))
    assert not monitor._is_qdc507(Device({"ID_VENDOR_ID": "2c7c", "ID_MODEL_ID": "0126"}))
