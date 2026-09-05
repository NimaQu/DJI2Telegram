from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Protocol

from qdc507_gateway.modem.usbcfg import USBConfiguration, parse_usb_configuration


class ModuleSetupError(RuntimeError):
    pass


TARGET_USB_CONFIGURATION = USBConfiguration(
    vendor_id=0x2C7C,
    product_id=0x0125,
    diagnostic=True,
    nmea=True,
    at=True,
    modem=True,
    network=True,
    adb=True,
    audio=True,
)


class ModuleService(Protocol):
    async def at(self, command: str, timeout_ms: int = 3000) -> dict[str, Any]:
        ...

    async def authorize_adb_for_setup(self) -> bool:
        ...

    async def usb_capabilities(self) -> dict[str, Any]:
        ...

    async def authorize_adb(self) -> bool:
        ...

    async def run_exclusive(self, operation: Callable[[], Any]) -> Any:
        ...

    def open_adb_client(self):
        ...


class VoiceController(Protocol):
    manifest: Any

    async def start_async(self) -> None:
        ...

    async def stop_async(self) -> None:
        ...


def _configuration_dict(value: USBConfiguration) -> dict[str, Any]:
    return {
        "vendor_id": f"0x{value.vendor_id:04X}",
        "product_id": f"0x{value.product_id:04X}",
        "diagnostic": value.diagnostic,
        "nmea": value.nmea,
        "at": value.at,
        "modem": value.modem,
        "network": value.network,
        "adb": value.adb,
        "audio": value.audio,
    }


async def _read_usbcfg(service: ModuleService) -> USBConfiguration:
    response = await service.at('AT+QCFG="USBCFG"', timeout_ms=5000)
    if not response.get("ok"):
        raise ModuleSetupError("USBCFG readback was not accepted")
    lines = response.get("lines")
    if not isinstance(lines, (list, tuple)):
        raise ModuleSetupError("USBCFG readback was malformed")
    terminal = response.get("terminal")
    parsed = parse_usb_configuration(
        "\n".join(str(item) for item in (*lines, terminal or ""))
    )
    if parsed is None:
        raise ModuleSetupError("modem returned an invalid USBCFG readback")
    return parsed


async def _inspect_adb(service: ModuleService) -> dict[str, Any]:
    def inspect() -> dict[str, Any]:
        client, close = service.open_adb_client()
        try:
            uid = client.shell("id -u", timeout=8).strip()
            kernel_release = client.shell("uname -r", timeout=8).strip()
            if not kernel_release:
                raise ModuleSetupError("module ADB returned no kernel release")
            return {
                "connected": True,
                "root": uid == "0",
                "kernel_release": kernel_release,
            }
        finally:
            close()

    return await service.run_exclusive(inspect)


async def setup_module(
    service: ModuleService,
    voice_controller: VoiceController | None,
    *,
    progress: Callable[[str], None] | None = None,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """Provision one QDC507 without sending SMS, dialing, or using USB networking.

    Unlock QADBKEY before writing an inactive ADB bit, then verify staged
    settings and reset once if settings or live descriptors need activation.
    No persistent command is retried automatically.
    """

    target = TARGET_USB_CONFIGURATION
    notify = progress or (lambda _message: None)
    notify("Reading current USBCFG")
    before = await _read_usbcfg(service)
    ims_before = await _read_ims(service)
    capabilities_before = await service.usb_capabilities()
    backup_path = None
    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, backup_path = tempfile.mkstemp(prefix="module-before-", suffix=".json", dir=backup_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"usb": _configuration_dict(before), "ims": ims_before,
                       "restore_usb_command": before.command}, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        notify(f"Saved settings backup: {backup_path}")
    changed = before != target
    ims_changed = ims_before[0] != 1
    restarted = False
    authorized_before_write = False
    if not before.adb:
        notify("Authorizing QADBKEY before enabling the ADB configuration bit")
        if not await service.authorize_adb_for_setup():
            raise ModuleSetupError("QADBKEY authorization was not accepted")
        authorized_before_write = True
    if ims_changed:
        notify("Enabling IMS")
        _accepted(await service.at('AT+QCFG="ims",1', timeout_ms=5000), "IMS write")
        if (await _read_ims(service))[0] != 1:
            raise ModuleSetupError("IMS configuration did not change to 1")
    if changed:
        notify("Writing complete ADB+Audio USBCFG")
        _accepted(await service.at(target.command, timeout_ms=5000), "USBCFG write")
        staged = await _retry_read(lambda: _read_usbcfg(service))
        if staged != target:
            raise ModuleSetupError("USBCFG readback does not match target; stopped before restart")
    if changed or ims_changed or not (capabilities_before["adb"] and capabilities_before["audio"]):
        notify("Restarting the module once to activate USB/IMS settings")
        _accepted(await service.at("AT+CFUN=1,1", timeout_ms=10000), "module restart")
        restarted = True

    after = await _retry_read(lambda: _read_usbcfg(service))
    if after != target:
        raise ModuleSetupError("USBCFG did not match the complete ADB+Audio target after setup")
    actual = await service.usb_capabilities()
    if not actual["adb"] or not actual["audio"]:
        raise ModuleSetupError("Saved USBCFG is correct but actual ADB/full-duplex UAC interfaces are missing")
    ims_after = await _retry_read(lambda: _read_ims(service))
    if ims_after[0] != 1:
        raise ModuleSetupError("IMS configuration did not survive restart")

    authorized_now = authorized_before_write
    notify("Checking direct ADB root access")
    try:
        adb = await _inspect_adb(service)
    except ModuleSetupError:
        raise
    except Exception:
        notify("ADB is unavailable; performing QADBKEY authorization")
        authorized = await service.authorize_adb()
        if not authorized:
            raise ModuleSetupError("QADBKEY authorization was not accepted")
        authorized_now = True
        adb = await _inspect_adb(service)
    if not adb["root"]:
        raise ModuleSetupError("module ADB is connected but does not provide root")

    voice_result: dict[str, Any]
    if voice_controller is None:
        voice_result = {"configured": False, "tested": False}
    else:
        notify("Testing the module voice runtime and UAC route")
        started = False
        try:
            await voice_controller.start_async()
            started = True
        finally:
            if started:
                await voice_controller.stop_async()
        voice_result = {
            "configured": True,
            "tested": True,
            "runtime_version": voice_controller.manifest.runtime_version,
        }

    notify("Module setup completed")
    return {
        "ready": True,
        "identity": "2C7C:0125",
        "backup_path": backup_path,
        "ims": {"changed": ims_changed, "configuration": ims_after[0], "volte_capability": ims_after[1]},
        "usbcfg": {
            "changed": changed,
            "restarted": restarted,
            "before": _configuration_dict(before),
            "after": _configuration_dict(after),
        },
        "adb": {**adb, "authorized_now": authorized_now},
        "voice_runtime": voice_result,
    }


def _accepted(response: dict[str, Any], operation: str) -> None:
    if response.get("ok") or (response.get("operation") in {"usbcfg", "cfun"} and "changed" in response):
        return
    raise ModuleSetupError(f"{operation} was not accepted")


async def _read_ims(service: ModuleService) -> list[int]:
    response = await service.at('AT+QCFG="ims"', timeout_ms=5000)
    if response.get("ok"):
        for line in response.get("lines", []):
            match = re.fullmatch(r'\s*\+QCFG:\s*"ims"\s*,\s*([012])\s*,\s*([01])\s*', str(line), re.I)
            if match:
                return [int(match[1]), int(match[2])]
    raise ModuleSetupError("IMS readback was not accepted or malformed")


async def _retry_read(operation):
    for attempt in range(15):
        try:
            return await operation()
        except Exception:
            if attempt == 14:
                raise
            await asyncio.sleep(2)
