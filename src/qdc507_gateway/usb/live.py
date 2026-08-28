from __future__ import annotations

from typing import Any, Optional

from qdc507_gateway.adb.transport import LibUSBADBClient
from qdc507_gateway.modem.at import ATSession
from qdc507_gateway.modem.transport import LibUSBBulkTransport, choose_at_interface
from qdc507_gateway.models import USBDeviceSnapshot
from qdc507_gateway.usb.descriptors import snapshot_from_libusb_device
from qdc507_gateway.usb.owner import DeviceOwnerLock


class LiveUSBError(RuntimeError):
    pass


class LibUSBDeviceSession:
    """Explicit live handle session shared by AT and ADB.

    Constructing the object and calling ``open`` only enumerates and opens a
    handle. Interface claim starts only from ``open_at`` or ``open_adb``. Both
    paths share one non-blocking process owner and never reset the device.
    """

    def __init__(
        self,
        context: Any = None,
        owner: Optional[DeviceOwnerLock] = None,
        vendor_id: int = 0x2C7C,
        product_id: int = 0x0125,
    ):
        if context is None:
            try:
                import usb1  # type: ignore
            except ImportError as exc:
                raise LiveUSBError("libusb1 (import usb1) is required for live USB access") from exc
            context = usb1.USBContext()
            self._owns_context = True
        else:
            self._owns_context = False
        self.context = context
        self.owner = owner or DeviceOwnerLock()
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.device = None
        self.handle = None
        self.snapshot = None
        self._transports: list[Any] = []

    def open(self, preferred: Optional[USBDeviceSnapshot] = None) -> None:
        if self.handle is not None:
            return
        for device in self.context.getDeviceList(skip_on_error=True):
            if device.getVendorID() != self.vendor_id or device.getProductID() != self.product_id:
                continue
            self.owner.acquire()
            try:
                self.device = device
                self.snapshot = snapshot_from_libusb_device(device)
                if preferred is not None and not preferred.same_physical_device(self.snapshot):
                    self.device = None
                    self.snapshot = None
                    self.owner.release()
                    continue
                self.handle = device.open()
                return
            except Exception:
                self.device = None
                self.snapshot = None
                self.handle = None
                self.owner.release()
                if self._owns_context:
                    self.context.close()
                raise
        if self._owns_context:
            self.context.close()
        raise LiveUSBError("QDC507 2C7C:0125 was not found")

    def _require_open(self) -> None:
        if self.handle is None or self.snapshot is None:
            raise LiveUSBError("live USB session is not open")

    def open_at(self, preferred: Optional[int] = None, handshake: bool = False) -> ATSession:
        self._require_open()
        candidates = self.snapshot.at_candidates
        if preferred is not None:
            candidates = (choose_at_interface(self.snapshot, preferred),)
        elif candidates:
            first = choose_at_interface(self.snapshot)
            candidates = (first,) + tuple(item for item in candidates if item.number != first.number)
        last_error: Optional[Exception] = None
        for interface in candidates:
            transport = LibUSBBulkTransport(self.handle, interface, close_handle=False)
            try:
                transport.open()
                session = ATSession(
                    transport.write,
                    lambda timeout: transport.read(timeout_ms=max(1, int(timeout * 1000))),
                )
                self._transports.append(transport)
                if not handshake:
                    return session
                response = session.command("AT", timeout=1.5)
                if response.ok:
                    return session
                raise LiveUSBError("AT handshake did not return OK")
            except Exception as exc:
                last_error = exc
                try:
                    transport.close()
                finally:
                    if transport in self._transports:
                        self._transports.remove(transport)
        if last_error is not None:
            raise LiveUSBError("no descriptor-qualified AT interface passed handshake") from last_error
        raise LiveUSBError("no descriptor-qualified vendor bulk AT interface")

    def open_adb(self) -> LibUSBADBClient:
        self._require_open()
        if not self.snapshot.adb_interfaces:
            raise LiveUSBError("no descriptor-qualified ADB interface FF/42/01 found")
        client = LibUSBADBClient(self.handle, self.snapshot.adb_interfaces[0], close_handle=False)
        self._transports.append(client)
        return client

    def close(self) -> None:
        for transport in reversed(self._transports):
            try:
                transport.close()
            except Exception:
                pass
        self._transports.clear()
        if self.handle is not None:
            try:
                self.handle.close()
            except Exception:
                pass
        self.handle = None
        self.device = None
        self.snapshot = None
        self.owner.release()
        if self._owns_context:
            self.context.close()

    def __enter__(self) -> "LibUSBDeviceSession":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
