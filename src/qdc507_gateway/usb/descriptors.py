from __future__ import annotations

import json
from typing import Any, Mapping, Protocol, Sequence

from qdc507_gateway.models import EndpointDescriptor, InterfaceDescriptor, USBDeviceSnapshot


class DeviceLocator(Protocol):
    def find(self, vendor_id: int = 0x2C7C, product_id: int = 0x0125) -> Sequence[USBDeviceSnapshot]:
        ...


def _transfer_type(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.lower()
        if normalized in {"control", "isochronous", "bulk", "interrupt"}:
            return normalized
        try:
            value = int(value, 0)
        except ValueError:
            return "unknown"
    # USB bmAttributes stores the transfer type in its low two bits.  The
    # mapping path may receive the complete bmAttributes byte, while libusb1
    # exposes the same byte through getAttributes().
    return {0: "control", 1: "isochronous", 2: "bulk", 3: "interrupt"}.get(int(value) & 0x03, "unknown")


def _endpoint_transfer_type(endpoint: Any) -> str:
    """Read a transfer type from either PyUSB-style or libusb1 endpoints."""
    get_transfer_type = getattr(endpoint, "getTransferType", None)
    if callable(get_transfer_type):
        return _transfer_type(get_transfer_type())
    get_attributes = getattr(endpoint, "getAttributes", None)
    if callable(get_attributes):
        return _transfer_type(get_attributes())
    return "unknown"


def _number(value: Any) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


def _endpoint_mapping(raw: Mapping[str, Any]) -> EndpointDescriptor:
    address = _number(raw.get("address", raw.get("bEndpointAddress", 0)))
    direction = str(raw.get("direction", "in" if address & 0x80 else "out")).lower()
    if direction in ("0", "outgoing"):
        direction = "out"
    elif direction in ("1", "incoming"):
        direction = "in"
    return EndpointDescriptor(
        address=address,
        direction=direction,
        transfer_type=_transfer_type(raw.get("transfer_type", raw.get("type", raw.get("bmAttributes", 0)))),
        max_packet_size=_number(raw.get("max_packet_size", raw.get("wMaxPacketSize", 0))),
        interval=_number(raw.get("interval", raw.get("bInterval", 0))),
    )


def snapshot_from_mapping(raw: Mapping[str, Any]) -> USBDeviceSnapshot:
    interfaces = []
    for item in raw.get("interfaces", []):
        interfaces.append(
            InterfaceDescriptor(
                number=int(item.get("number", item.get("interface", item.get("bInterfaceNumber", 0)))),
                interface_class=int(item.get("class", item.get("interface_class", item.get("bInterfaceClass", 0)))),
                subclass=int(item.get("subclass", item.get("interface_subclass", item.get("bInterfaceSubClass", 0)))),
                protocol=int(item.get("protocol", item.get("interface_protocol", item.get("bInterfaceProtocol", 0)))),
                endpoints=tuple(_endpoint_mapping(ep) for ep in item.get("endpoints", [])),
            )
        )
    return USBDeviceSnapshot(
        vendor_id=int(raw.get("vendor_id", raw.get("idVendor", 0)), 0) if isinstance(raw.get("vendor_id", raw.get("idVendor", 0)), str) else int(raw.get("vendor_id", raw.get("idVendor", 0))),
        product_id=int(raw.get("product_id", raw.get("idProduct", 0)), 0) if isinstance(raw.get("product_id", raw.get("idProduct", 0)), str) else int(raw.get("product_id", raw.get("idProduct", 0))),
        bus=raw.get("bus"),
        address=raw.get("address"),
        port_path=tuple(int(x) for x in raw.get("port_path", raw.get("ports", []))),
        serial=raw.get("serial"),
        interfaces=tuple(interfaces),
    )


class JSONDeviceLocator:
    """Offline descriptor source used by tests and CI."""

    def __init__(self, path: str):
        self.path = path

    def find(self, vendor_id: int = 0x2C7C, product_id: int = 0x0125) -> Sequence[USBDeviceSnapshot]:
        with open(self.path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        values = raw if isinstance(raw, list) else raw.get("devices", [raw])
        return tuple(
            snapshot
            for snapshot in (snapshot_from_mapping(item) for item in values)
            if snapshot.vendor_id == vendor_id and snapshot.product_id == product_id
        )


def snapshot_from_libusb_device(device: Any) -> USBDeviceSnapshot:
    """Build a descriptor snapshot from one already-enumerated libusb device."""
    interfaces = []
    for setting in device.iterSettings():
        endpoints = []
        for endpoint in setting.iterEndpoints():
            address = endpoint.getAddress()
            endpoints.append(
                EndpointDescriptor(
                    address=address,
                    direction="in" if address & 0x80 else "out",
                    transfer_type=_endpoint_transfer_type(endpoint),
                    max_packet_size=endpoint.getMaxPacketSize(),
                    interval=endpoint.getInterval(),
                )
            )
        interfaces.append(
            InterfaceDescriptor(
                number=setting.getNumber(),
                interface_class=setting.getClass(),
                subclass=setting.getSubClass(),
                protocol=setting.getProtocol(),
                endpoints=tuple(endpoints),
            )
        )
    serial = None
    try:
        serial = device.getSerialNumber()
    except Exception:
        pass
    port_path = ()
    try:
        port_path = tuple(device.getPortNumberList())
    except Exception:
        pass
    return USBDeviceSnapshot(
        vendor_id=device.getVendorID(),
        product_id=device.getProductID(),
        bus=device.getBusNumber(),
        address=device.getDeviceAddress(),
        port_path=port_path,
        serial=serial,
        interfaces=tuple(interfaces),
    )


class LibUSBDeviceLocator:
    """Descriptor-only libusb locator. It never claims or resets a device."""

    def __init__(self, context: Any = None):
        try:
            import usb1  # type: ignore
        except ImportError as exc:
            raise RuntimeError("libusb1 (import usb1) is required for live USB probing") from exc
        self._usb1 = usb1
        self._owns_context = context is None
        self._context = context if context is not None else usb1.USBContext()

    def find(self, vendor_id: int = 0x2C7C, product_id: int = 0x0125) -> Sequence[USBDeviceSnapshot]:
        result = []
        for device in self._context.getDeviceList(skip_on_error=True):
            if device.getVendorID() != vendor_id or device.getProductID() != product_id:
                continue
            result.append(snapshot_from_libusb_device(device))
        return tuple(result)

    def close(self) -> None:
        if self._owns_context and self._context is not None:
            self._context.close()
            self._context = None
