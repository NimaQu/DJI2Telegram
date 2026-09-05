from __future__ import annotations

import asyncio
from typing import Any

from qdc507_gateway.events import EventBus
from qdc507_gateway.models import GatewayEvent, USBProbeReport
from qdc507_gateway.storage.database import Database
from qdc507_gateway.usb.descriptors import DeviceLocator


class GatewayRuntime:
    """Headless supervisor for the safe probe/reconnect boundary.

    The runtime deliberately stops at descriptor discovery. Claiming an AT or
    ADB interface is a later, explicitly owned operation and never happens as a
    service startup side effect.
    """

    def __init__(self, locator: DeviceLocator, database: Database, events: EventBus, state: dict[str, Any]):
        self.locator = locator
        self.database = database
        self.events = events
        self.state = state
        self.running = False
        self._probe_lock = asyncio.Lock()

    async def start(self) -> None:
        self.running = True
        await self.probe_once(reason="startup")

    async def stop(self) -> None:
        self.running = False
        closer = getattr(self.locator, "close", None)
        if callable(closer):
            await asyncio.to_thread(closer)

    async def reconnect(self) -> dict[str, Any]:
        return await self.probe_once(reason="reconnect")

    async def probe_once(self, reason: str = "probe") -> dict[str, Any]:
        async with self._probe_lock:
            try:
                devices = await asyncio.to_thread(self.locator.find)
                if devices:
                    device = devices[0]
                    report = USBProbeReport(
                        True,
                        device,
                        tuple(
                            warning for warning, present in (
                                ("no descriptor-qualified ADB interface FF/42/01 found", bool(device.adb_interfaces)),
                                ("no UAC interface found", bool(device.uac_interfaces)),
                                ("no descriptor-qualified QDC507 UAC audio endpoint found", bool(device.uac_audio_endpoints)),
                            ) if not present
                        ),
                    )
                else:
                    report = USBProbeReport(False, None, ("configured QDC507 USB device was not found",))
            except Exception as exc:
                message = f"USB probe failed: {type(exc).__name__}"
                self.state["status"] = {**self.state.get("status", {}), "module_state": "error", "last_error": message}
                self.state["module"] = {"connected": False, "identity": None, "error": message}
                await self.events.publish(GatewayEvent("module.error", {"reason": reason, "error": message}))
                return {"found": False, "error": message}

            payload = report.to_dict()
            device = report.device
            previous_signal = self.state.get("module", {}).get("signal")
            self.state["module"] = {
                "connected": report.found,
                "identity": None if device is None else device.identity,
                "bus": None if device is None else device.bus,
                "address": None if device is None else device.address,
                "adb_interfaces": [] if device is None else [item.number for item in device.adb_interfaces],
                "uac_interfaces": [] if device is None else [item.number for item in device.uac_interfaces],
                "uac_audio_endpoints": [] if device is None else [
                    {
                        "address": endpoint.address,
                        "direction": endpoint.direction,
                        "max_packet_size": endpoint.max_packet_size,
                        "interval": endpoint.interval,
                    }
                    for endpoint in device.uac_audio_endpoints
                ],
                "at_candidates": [] if device is None else [item.number for item in device.at_candidates],
                "warnings": list(report.warnings),
                "signal": previous_signal if report.found else None,
            }
            self.state["status"] = {
                **self.state.get("status", {}),
                "module_state": "connected" if report.found else "disconnected",
            }
            event_type = "module.connected" if report.found else "module.disconnected"
            await self.events.publish(GatewayEvent(event_type, {"reason": reason, **self.state["module"]}))
            return payload
