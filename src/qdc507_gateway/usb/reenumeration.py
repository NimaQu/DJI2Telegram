from __future__ import annotations

import time
from typing import Callable, Optional

from qdc507_gateway.models import USBDeviceSnapshot


class ReenumerationCoordinator:
    def __init__(
        self,
        locator,
        sleep: Callable[[float], None] = time.sleep,
        event_waiter: Optional[Callable[[float], bool]] = None,
    ):
        self.locator = locator
        self.sleep = sleep
        self.event_waiter = event_waiter
        self.in_progress = False

    def wait_for_device(self, previous: USBDeviceSnapshot, timeout: float = 20.0) -> USBDeviceSnapshot:
        self.in_progress = True
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for candidate in self.locator.find(previous.vendor_id, previous.product_id):
                    if previous.same_physical_device(candidate):
                        return candidate
                if self.event_waiter is not None:
                    try:
                        self.event_waiter(min(0.2, max(0.0, deadline - time.monotonic())))
                    except Exception:
                        # libusb descriptor polling remains authoritative when
                        # udev is unavailable or loses its netlink socket.
                        self.sleep(0.2)
                else:
                    self.sleep(0.2)
            raise TimeoutError("QDC507 did not re-enumerate before the deadline")
        finally:
            self.in_progress = False
