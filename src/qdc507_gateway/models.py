from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ModuleState(str, Enum):
    disconnected = "disconnected"
    probing = "probing"
    connected = "connected"
    reenumerating = "reenumerating"
    error = "error"


class CallState(str, Enum):
    idle = "idle"
    waiting_client = "waiting_client"
    waiting_telegram = "waiting_telegram"
    waiting_cellular = "waiting_cellular"
    ringing_cellular = "ringing_cellular"
    active = "active"
    hanging_up = "hanging_up"
    failed = "failed"
    ended = "ended"


class CallDirection(str, Enum):
    inbound_cellular = "inbound_cellular"
    outbound_cellular = "outbound_cellular"


@dataclass(frozen=True)
class EndpointDescriptor:
    address: int
    direction: str
    transfer_type: str
    max_packet_size: int
    interval: int = 0


@dataclass(frozen=True)
class InterfaceDescriptor:
    number: int
    interface_class: int
    subclass: int
    protocol: int
    endpoints: Tuple[EndpointDescriptor, ...] = ()

    @property
    def is_adb(self) -> bool:
        return (
            self.interface_class == 0xFF
            and self.subclass == 0x42
            and self.protocol == 0x01
            and any(ep.direction == "in" and ep.transfer_type == "bulk" for ep in self.endpoints)
            and any(ep.direction == "out" and ep.transfer_type == "bulk" for ep in self.endpoints)
        )

    @property
    def is_uac(self) -> bool:
        # QDC507 exposes its UAC function as interfaces 7/8/9. Interface 6
        # is configuration-dependent and must not be inferred as audio just
        # because a legacy descriptor reports class 0x01 there.
        return self.number in (7, 8, 9) and self.interface_class == 0x01

    @property
    def is_vendor_bulk_candidate(self) -> bool:
        return (
            self.interface_class == 0xFF
            and any(ep.transfer_type == "bulk" and ep.direction == "in" for ep in self.endpoints)
            and any(ep.transfer_type == "bulk" and ep.direction == "out" for ep in self.endpoints)
        )


@dataclass(frozen=True)
class USBDeviceSnapshot:
    vendor_id: int
    product_id: int
    bus: Optional[int] = None
    address: Optional[int] = None
    port_path: Tuple[int, ...] = ()
    serial: Optional[str] = None
    interfaces: Tuple[InterfaceDescriptor, ...] = ()

    @property
    def identity(self) -> str:
        return "%04X:%04X" % (self.vendor_id, self.product_id)

    @property
    def adb_interfaces(self) -> Tuple[InterfaceDescriptor, ...]:
        return tuple(item for item in self.interfaces if item.is_adb)

    @property
    def uac_interfaces(self) -> Tuple[InterfaceDescriptor, ...]:
        return tuple(item for item in self.interfaces if item.is_uac)

    @property
    def uac_audio_endpoints(self) -> Tuple[EndpointDescriptor, ...]:
        return tuple(
            endpoint
            for interface in self.uac_interfaces
            for endpoint in interface.endpoints
            if endpoint.transfer_type == "isochronous"
            and endpoint.direction in ("in", "out")
        )

    @property
    def at_candidates(self) -> Tuple[InterfaceDescriptor, ...]:
        return tuple(
            item for item in self.interfaces if item.is_vendor_bulk_candidate and not item.is_adb
        )

    @property
    def supported_identity(self) -> bool:
        return (self.vendor_id, self.product_id) == (0x2C7C, 0x0125)

    def same_physical_device(self, other: "USBDeviceSnapshot") -> bool:
        if (self.vendor_id, self.product_id) != (other.vendor_id, other.product_id):
            return False
        if self.serial and other.serial:
            return self.serial == other.serial
        if self.port_path and other.port_path:
            return self.port_path == other.port_path
        return True


@dataclass(frozen=True)
class USBProbeReport:
    found: bool
    device: Optional[USBDeviceSnapshot]
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        device = self.device
        return {
            "found": self.found,
            "warnings": list(self.warnings),
            "device": None if device is None else {
                "identity": device.identity,
                "vendor_id": device.vendor_id,
                "product_id": device.product_id,
                "bus": device.bus,
                "address": device.address,
                "port_path": list(device.port_path),
                "serial": device.serial,
                "adb_interfaces": [item.number for item in device.adb_interfaces],
                "uac_interfaces": [item.number for item in device.uac_interfaces],
                "uac_audio_endpoints": [
                    {
                        "address": endpoint.address,
                        "direction": endpoint.direction,
                        "max_packet_size": endpoint.max_packet_size,
                        "interval": endpoint.interval,
                    }
                    for endpoint in device.uac_audio_endpoints
                ],
                "at_candidates": [item.number for item in device.at_candidates],
                "interfaces": [
                    {
                        "number": item.number,
                        "class": item.interface_class,
                        "subclass": item.subclass,
                        "protocol": item.protocol,
                        "endpoints": [
                            {
                                "address": ep.address,
                                "direction": ep.direction,
                                "transfer_type": ep.transfer_type,
                                "max_packet_size": ep.max_packet_size,
                                "interval": ep.interval,
                            }
                            for ep in item.endpoints
                        ],
                    }
                    for item in device.interfaces
                ],
            },
        }


@dataclass(frozen=True)
class SMSMessage:
    id: str
    sender: str
    body: str
    timestamp: datetime
    is_read: bool = False
    raw_pdus: Tuple[str, ...] = ()


@dataclass
class CallRecord:
    id: str
    direction: CallDirection
    state: CallState
    cellular_number: Optional[str] = None
    telegram_user_id: Optional[int] = None
    frontend: str = "telegram"
    started_at: datetime = field(default_factory=utc_now)
    connected_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    last_error: Optional[str] = None


@dataclass(frozen=True)
class GatewayEvent:
    type: str
    payload: Dict[str, Any]
    timestamp: datetime = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
        }
