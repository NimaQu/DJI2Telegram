from __future__ import annotations

from dataclasses import replace
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
    vendor_id: int = 0x2C7C,
    product_id: int = 0x0125,
) -> dict[str, Any]:
    """Provision one QDC507 without sending SMS, dialing, or using USB networking.

    A non-target USBCFG is written exactly once and followed by exactly one
    CFUN reset. QADBKEY is attempted only when a direct ADB connection check
    fails. No persistent command is retried automatically.
    """

    target = replace(TARGET_USB_CONFIGURATION, vendor_id=vendor_id, product_id=product_id)
    notify = progress or (lambda _message: None)
    notify("Reading current USBCFG")
    before = await _read_usbcfg(service)
    changed = before != target
    restarted = False

    if changed:
        notify("Writing complete ADB+Audio USBCFG")
        await service.at(target.command, timeout_ms=5000)
        notify("Restarting the module once to activate USBCFG")
        await service.at("AT+CFUN=1,1", timeout_ms=10000)
        restarted = True

    after = await _read_usbcfg(service)
    if after != target:
        raise ModuleSetupError(
            "USBCFG did not match the complete ADB+Audio target after setup"
        )

    authorized_now = False
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
        "identity": f"{vendor_id:04X}:{product_id:04X}",
        "usbcfg": {
            "changed": changed,
            "restarted": restarted,
            "before": _configuration_dict(before),
            "after": _configuration_dict(after),
        },
        "adb": {**adb, "authorized_now": authorized_now},
        "voice_runtime": voice_result,
    }
