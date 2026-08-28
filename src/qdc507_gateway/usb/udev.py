from __future__ import annotations

import time
from typing import Any


class UdevUnavailable(RuntimeError):
    """Raised when the optional Linux udev monitor cannot be created."""


class PyUdevUSBMonitor:
    """Read-only udev monitor for the physical QDC507 USB device.

    The monitor observes kernel/udev events only. It never opens a device,
    claims an interface, resets USB, or changes a udev property.
    """

    def __init__(self, vendor_id: int = 0x2C7C, product_id: int = 0x0125):
        try:
            import pyudev  # type: ignore
        except ImportError as exc:
            raise UdevUnavailable("pyudev is required for USB hotplug monitoring") from exc
        self.vendor_id = "%04x" % vendor_id
        self.product_id = "%04x" % product_id
        try:
            self.context = pyudev.Context()
            self.monitor = pyudev.Monitor.from_netlink(self.context)
            self.monitor.filter_by(subsystem="usb", device_type="usb_device")
        except Exception as exc:
            raise UdevUnavailable("could not create a pyudev USB monitor") from exc

    def wait(self, timeout: float = 0.2) -> bool:
        """Wait for one matching QDC507 add/remove/change event."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                device = self.monitor.poll(timeout=remaining)
            except Exception as exc:
                raise UdevUnavailable("pyudev USB monitor poll failed") from exc
            if device is None:
                return False
            if self._is_qdc507(device):
                return True
            if remaining <= 0:
                return False

    def close(self) -> None:
        self.monitor = None
        self.context = None

    def _is_qdc507(self, device: Any) -> bool:
        vendor = self._value(device, "ID_VENDOR_ID", "idVendor")
        product = self._value(device, "ID_MODEL_ID", "idProduct")
        return vendor.lower().removeprefix("0x").zfill(4) == self.vendor_id and (
            product.lower().removeprefix("0x").zfill(4) == self.product_id
        )

    @staticmethod
    def _value(device: Any, *names: str) -> str:
        for name in names:
            try:
                value = device.get(name)
            except (AttributeError, KeyError, TypeError):
                value = None
            if value is not None:
                return str(value)
        return ""
