from __future__ import annotations

from typing import Any, Optional

from qdc507_gateway.models import InterfaceDescriptor, USBDeviceSnapshot
from qdc507_gateway.usb.owner import DeviceOwnerLock


class USBTransportError(IOError):
    pass


def choose_at_interface(device: USBDeviceSnapshot, preferred: Optional[int] = None) -> InterfaceDescriptor:
    candidates = device.at_candidates
    if preferred is not None:
        candidates = tuple(item for item in candidates if item.number == preferred)
    if not candidates:
        raise USBTransportError("no descriptor-qualified vendor bulk AT interface")
    if len(candidates) > 1 and preferred is None:
        # Interface 2 is a known QDC507 hint, not an identity rule. It wins only
        # after descriptor filtering; callers may override it explicitly.
        hinted = tuple(item for item in candidates if item.number == 2)
        if hinted:
            return hinted[0]
    return candidates[0]


class LibUSBBulkTransport:
    """Small synchronous bulk transport used behind a single owner thread."""

    def __init__(
        self,
        handle: Any,
        interface: InterfaceDescriptor,
        owner: Optional[DeviceOwnerLock] = None,
        close_handle: bool = True,
    ):
        self.handle = handle
        self.interface = interface
        self.owner = owner
        self.close_handle = close_handle
        try:
            self.input_endpoint = next(
                ep.address
                for ep in interface.endpoints
                if ep.direction == "in" and ep.transfer_type == "bulk"
            )
            self.output_endpoint = next(
                ep.address
                for ep in interface.endpoints
                if ep.direction == "out" and ep.transfer_type == "bulk"
            )
        except StopIteration as exc:
            raise USBTransportError(
                "descriptor-qualified bulk interface is missing an IN/OUT endpoint"
            ) from exc
        self._claimed = False
        self._kernel_driver_detached = False

    def _detach_kernel_driver_if_needed(self) -> None:
        """Temporarily release a Linux class driver from this interface only."""
        is_active = getattr(self.handle, "kernelDriverActive", None)
        detach = getattr(self.handle, "detachKernelDriver", None)
        if not callable(is_active) or not callable(detach):
            return
        try:
            active = bool(is_active(self.interface.number))
        except Exception:
            # libusb reports NOT_FOUND when no kernel driver is bound.  Leave
            # claimInterface to provide the authoritative error otherwise.
            active = False
        if active:
            detach(self.interface.number)
            self._kernel_driver_detached = True

    def _reattach_kernel_driver(self) -> None:
        if not self._kernel_driver_detached:
            return
        attach = getattr(self.handle, "attachKernelDriver", None)
        try:
            if callable(attach):
                attach(self.interface.number)
        finally:
            self._kernel_driver_detached = False

    def open(self) -> None:
        if self.owner is not None:
            self.owner.acquire()
        try:
            self._detach_kernel_driver_if_needed()
            self.handle.claimInterface(self.interface.number)
            self._claimed = True
        except Exception:
            self._reattach_kernel_driver()
            if self.owner is not None:
                self.owner.release()
            raise

    def close(self) -> None:
        first_error: Optional[Exception] = None
        if self._claimed:
            try:
                self.handle.releaseInterface(self.interface.number)
            except Exception as exc:
                first_error = exc
            finally:
                self._claimed = False
                try:
                    self._reattach_kernel_driver()
                except Exception as exc:
                    first_error = first_error or exc
        if self.close_handle:
            try:
                self.handle.close()
            except Exception as exc:
                first_error = first_error or exc
        if self.owner is not None:
            try:
                self.owner.release()
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def write(self, data: bytes, timeout_ms: int = 1000) -> None:
        if not self._claimed:
            raise USBTransportError("bulk transport is not open")
        self.handle.bulkWrite(self.output_endpoint, data, timeout=timeout_ms)

    def read(self, length: int = 4096, timeout_ms: int = 200) -> bytes:
        if not self._claimed:
            raise USBTransportError("bulk transport is not open")
        return bytes(self.handle.bulkRead(self.input_endpoint, length, timeout=timeout_ms))
