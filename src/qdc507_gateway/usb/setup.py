from __future__ import annotations

import time

from qdc507_gateway.modem.qadbkey import authorize_qadbkey
from qdc507_gateway.modem.service import LiveModuleService, ModuleServiceError
from qdc507_gateway.modem.usbcfg import is_reenumeration_signal, parse_usbcfg_command
from qdc507_gateway.usb.descriptors import LibUSBDeviceLocator
from qdc507_gateway.usb.live import LibUSBDeviceSession
from qdc507_gateway.usb.owner import DeviceOwnerLock


class SetupModuleService(LiveModuleService):
    """Offline provisioning lease which follows a module across USB identities.

    Normal gateway operation uses 2c7c:0125. Only module-setup discovers
    both the factory DJI identity and the fixed Quectel target.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.identities = {(0x2CA3, 0x4006), (0x2C7C, 0x0125)}
        self.vendor_id, self.product_id = 0x2C7C, 0x0125
        self.original_device = None
        self.last_device = None

    def _discover(self):
        locator = self.locator or LibUSBDeviceLocator()
        try:
            candidates = [device for vid, pid in sorted(self.identities) for device in locator.find(vid, pid)]
        finally:
            if self.locator is None:
                locator.close()
        if len(candidates) > 1:
            raise ModuleServiceError("module-setup requires exactly one matching USB module")
        if not candidates:
            return None
        device = candidates[0]
        original = self.original_device
        if original is not None:
            if original.serial and device.serial:
                same = original.serial == device.serial
            else:
                same = bool(original.port_path and device.port_path) and (
                    original.bus, original.port_path
                ) == (device.bus, device.port_path)
            if not same:
                raise ModuleServiceError("USB module moved or changed during setup; reconnect it to the original port")
        self.original_device = original or device
        self.last_device = device
        self.vendor_id, self.product_id = device.vendor_id, device.product_id
        self.identity = device.identity
        return device

    def _session(self):
        device = self._discover()
        if device is None:
            raise ModuleServiceError(
                "Setup USB module is absent; for KVM/PVE check USB passthrough for "
                "2c7c:0125 or pass through the physical USB port, then rerun module-setup"
            )
        return LibUSBDeviceSession(
            owner=DeviceOwnerLock(self.lock_path),
            vendor_id=self.vendor_id, product_id=self.product_id,
        )

    def _wait_for_same_device(self, previous):
        deadline = time.monotonic() + 90
        disappeared = False
        while time.monotonic() < deadline:
            device = self._discover()
            if device is None:
                disappeared = True
            elif disappeared or (
                device.vendor_id, device.product_id, device.address
            ) != (previous.vendor_id, previous.product_id, previous.address):
                return device
            time.sleep(0.5)
        raise ModuleServiceError(
            "Module did not return after restart; check KVM/PVE USB passthrough "
            "for 2c7c:0125 or use physical-port passthrough, then rerun module-setup"
        )

    def _persistent_usbcfg(self, command, timeout_ms):
        target = parse_usbcfg_command(command)
        if target is None or not target.is_full_target:
            raise ModuleServiceError("setup requires the complete target USB configuration")
        with self._session() as session:
            at = session.open_at(handshake=True)
            before = self._read_usbcfg(at, timeout_ms)
            if before == target:
                return {"operation": "usbcfg", "changed": False, "reenumerated": False}
            try:
                response = at.command(target.command, timeout_ms / 1000)
            except Exception as exc:
                if not is_reenumeration_signal(exc):
                    raise
                previous = session.snapshot
                session.close()
                self._wait_for_same_device(previous)
            else:
                if not response.ok:
                    raise ModuleServiceError("USBCFG write was not accepted")
            # QDC507GLEFM21 acknowledges the write but may keep adb=0 until
            # QADBKEY authorization. Do not wait for a detach after plain OK.
            return {"operation": "usbcfg", "changed": True, "reenumerated": False}

    async def authorize_adb_for_setup(self):
        """Unlock the ADB configuration bit before writing USBCFG.

        QDC507GLEFM21 returns OK but retains adb=0 if this step is skipped.
        ADB does not appear until a subsequent USBCFG write and CFUN restart.
        """
        def work():
            with self._session() as session:
                return authorize_qadbkey(session.open_at(handshake=True)).confirmed
        return bool(await self.run_exclusive(work))

    async def usb_capabilities(self):
        device = self._discover()
        if device is None:
            raise ModuleServiceError("USB module is absent")
        return {
            "identity": device.identity,
            "adb": bool(device.adb_interfaces),
            "audio": {ep.direction for ep in device.uac_audio_endpoints} == {"in", "out"},
        }
