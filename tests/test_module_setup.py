import asyncio

import pytest

from qdc507_gateway.cli import main
from qdc507_gateway.module_setup import (
    ModuleSetupError,
    TARGET_USB_CONFIGURATION,
    setup_module as setup_qdc507_module,
)
from qdc507_gateway.modem.usbcfg import USBConfiguration


LEGACY_USB_CONFIGURATION = USBConfiguration(
    vendor_id=0x2C7C,
    product_id=0x0125,
    diagnostic=True,
    nmea=True,
    at=True,
    modem=True,
    network=True,
    adb=False,
    audio=True,
)


def _readback(value: USBConfiguration) -> dict[str, object]:
    return {
        "ok": True,
        "terminal": "OK",
        "lines": [
            '+QCFG: "USBCFG",0x%04X,0x%04X,%s'
            % (
                value.vendor_id,
                value.product_id,
                ",".join(
                    "1" if flag else "0"
                    for flag in (
                        value.diagnostic,
                        value.nmea,
                        value.at,
                        value.modem,
                        value.network,
                        value.adb,
                        value.audio,
                    )
                ),
            )
        ],
    }


class FakeADB:
    def __init__(self, root=True):
        self.root = root

    def shell(self, command, timeout=10):
        if command == "id -u":
            return "0\n" if self.root else "2000\n"
        if command == "uname -r":
            return "3.18.44\n"
        raise AssertionError(command)


class FakeModuleService:
    def __init__(self, configuration, adb_available, adb_root=True):
        self.ims = [1, 0]
        self.configuration = configuration
        self.adb_available = adb_available
        self.adb_root = adb_root
        self.commands = []
        self.authorize_calls = 0
        self.closed_adb = 0

    async def at(self, command, timeout_ms=3000):
        self.commands.append(command)
        if command == 'AT+QCFG="ims"':
            return {"ok": True, "lines": [f'+QCFG: "ims",{self.ims[0]},{self.ims[1]}']}
        if command == 'AT+QCFG="ims",1':
            self.ims[0] = 1
            return {"ok": True}
        if command == 'AT+QCFG="USBCFG"':
            return _readback(self.configuration)
        if command == TARGET_USB_CONFIGURATION.command:
            self.configuration = TARGET_USB_CONFIGURATION
            # Saved USBCFG changes immediately; descriptors activate at reset.
            return {"ok": True, "terminal": "OK", "lines": []}
        if command == "AT+CFUN=1,1":
            self.configuration = TARGET_USB_CONFIGURATION
            return {"operation": "cfun", "changed": True, "reenumerated": True}
        raise AssertionError(command)

    async def usb_capabilities(self):
        return {"adb": self.configuration.adb, "audio": self.configuration.audio}

    async def authorize_adb_for_setup(self):
        return await self.authorize_adb()

    async def authorize_adb(self):
        self.authorize_calls += 1
        self.adb_available = True
        return True

    async def run_exclusive(self, operation):
        return operation()

    def open_adb_client(self):
        if not self.adb_available:
            raise RuntimeError("ADB is unauthorized")
        return FakeADB(root=self.adb_root), self._close_adb

    def _close_adb(self):
        self.closed_adb += 1


class FakeManifest:
    runtime_version = "test-runtime"


class FakeVoiceController:
    manifest = FakeManifest()

    def __init__(self):
        self.started = 0
        self.stopped = 0

    async def start_async(self):
        self.started += 1

    async def stop_async(self):
        self.stopped += 1


def test_module_setup_applies_usbcfg_restarts_authorizes_and_tests_voice_once():
    service = FakeModuleService(LEGACY_USB_CONFIGURATION, adb_available=False)
    voice = FakeVoiceController()

    result = asyncio.run(setup_qdc507_module(service, voice))

    assert result["ready"] is True
    assert result["usbcfg"]["changed"] is True
    assert result["usbcfg"]["restarted"] is True
    assert result["adb"]["authorized_now"] is True
    assert result["voice_runtime"] == {
        "configured": True,
        "tested": True,
        "runtime_version": "test-runtime",
    }
    assert service.commands.count(TARGET_USB_CONFIGURATION.command) == 1
    assert service.commands.count("AT+CFUN=1,1") == 1
    assert service.authorize_calls == 1
    assert voice.started == 1
    assert voice.stopped == 1


def test_module_setup_is_idempotent_when_configuration_and_adb_are_ready():
    service = FakeModuleService(TARGET_USB_CONFIGURATION, adb_available=True)

    result = asyncio.run(setup_qdc507_module(service, None))

    assert result["usbcfg"]["changed"] is False
    assert result["usbcfg"]["restarted"] is False
    assert result["adb"]["authorized_now"] is False
    assert result["voice_runtime"] == {"configured": False, "tested": False}
    assert TARGET_USB_CONFIGURATION.command not in service.commands
    assert "AT+CFUN=1,1" not in service.commands
    assert service.authorize_calls == 0


def test_module_setup_fails_closed_if_target_does_not_survive_restart():
    class BrokenService(FakeModuleService):
        async def at(self, command, timeout_ms=3000):
            result = await super().at(command, timeout_ms)
            if command == "AT+CFUN=1,1":
                self.configuration = LEGACY_USB_CONFIGURATION
            return result

    service = BrokenService(LEGACY_USB_CONFIGURATION, adb_available=False)
    with pytest.raises(ModuleSetupError, match="did not match"):
        asyncio.run(setup_qdc507_module(service, None))
    assert service.authorize_calls == 1


def test_module_setup_rejects_non_root_adb_without_reauthorizing():
    service = FakeModuleService(
        TARGET_USB_CONFIGURATION,
        adb_available=True,
        adb_root=False,
    )
    with pytest.raises(ModuleSetupError, match="does not provide root"):
        asyncio.run(setup_qdc507_module(service, None))
    assert service.authorize_calls == 0


def test_module_setup_cli_requires_explicit_confirmation():
    with pytest.raises(SystemExit, match="pass --confirm"):
        main(["module-setup"])


def test_locked_firmware_requires_authorization_before_usb_write(tmp_path):
    import json
    from dataclasses import replace

    class LockedFirmware(FakeModuleService):
        unlocked = False
        async def authorize_adb_for_setup(self):
            self.unlocked = True
            return await super().authorize_adb_for_setup()
        async def at(self, command, timeout_ms=3000):
            if command == TARGET_USB_CONFIGURATION.command:
                assert self.unlocked
                saved = list(tmp_path.glob("*.json"))
                assert len(saved) == 1
                assert json.loads(saved[0].read_text())["usb"]["vendor_id"] == "0x2CA3"
            return await super().at(command, timeout_ms)

    service = LockedFirmware(replace(LEGACY_USB_CONFIGURATION, vendor_id=0x2CA3, product_id=0x4006), False)
    service.ims = [0, 0]
    result = asyncio.run(setup_qdc507_module(service, None, backup_dir=tmp_path))
    assert result["ready"] and result["identity"] == "2C7C:0125"
    assert result["ims"] == {"changed": True, "configuration": 1, "volte_capability": 0}
    assert service.commands.count(TARGET_USB_CONFIGURATION.command) == 1
    assert service.commands.count("AT+CFUN=1,1") == 1


def test_setup_restarts_when_saved_settings_do_not_match_live_descriptors():
    class Firmware(FakeModuleService):
        rebooted = False
        async def usb_capabilities(self):
            return {"adb": self.rebooted, "audio": self.rebooted}
        async def at(self, command, timeout_ms=3000):
            result = await super().at(command, timeout_ms)
            if command == "AT+CFUN=1,1":
                self.rebooted = True
            return result
    service = Firmware(TARGET_USB_CONFIGURATION, True)
    result = asyncio.run(setup_qdc507_module(service, None))
    assert result["usbcfg"]["restarted"]
    assert TARGET_USB_CONFIGURATION.command not in service.commands


def test_setup_does_not_restart_after_unaccepted_staged_usb_readback():
    class Firmware(FakeModuleService):
        async def at(self, command, timeout_ms=3000):
            if command == TARGET_USB_CONFIGURATION.command:
                self.commands.append(command)
                return {"ok": True}
            return await super().at(command, timeout_ms)
    service = Firmware(LEGACY_USB_CONFIGURATION, False)
    with pytest.raises(ModuleSetupError, match="stopped before restart"):
        asyncio.run(setup_qdc507_module(service, None))
    assert "AT+CFUN=1,1" not in service.commands
