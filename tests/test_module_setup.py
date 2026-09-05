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
        self.configuration = configuration
        self.adb_available = adb_available
        self.adb_root = adb_root
        self.commands = []
        self.authorize_calls = 0
        self.closed_adb = 0

    async def at(self, command, timeout_ms=3000):
        self.commands.append(command)
        if command == 'AT+QCFG="USBCFG"':
            return _readback(self.configuration)
        if command == TARGET_USB_CONFIGURATION.command:
            # The persistent value is intentionally not made active until the
            # one explicit CFUN reset below.
            return {"ok": True, "terminal": "OK", "lines": []}
        if command == "AT+CFUN=1,1":
            self.configuration = TARGET_USB_CONFIGURATION
            return {"operation": "cfun", "changed": True, "reenumerated": True}
        raise AssertionError(command)

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
    assert service.commands == [
        'AT+QCFG="USBCFG"',
        TARGET_USB_CONFIGURATION.command,
        "AT+CFUN=1,1",
        'AT+QCFG="USBCFG"',
    ]
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
    assert service.commands == ['AT+QCFG="USBCFG"', 'AT+QCFG="USBCFG"']
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
    assert service.authorize_calls == 0


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


def test_setup_preserves_configured_dji_identity():
    from dataclasses import replace
    from qdc507_gateway.modem.usbcfg import parse_usbcfg_command

    target = replace(TARGET_USB_CONFIGURATION, vendor_id=0x2CA3, product_id=0x4006)

    class DJIService(FakeModuleService):
        async def at(self, command, timeout_ms=3000):
            self.commands.append(command)
            if command == 'AT+QCFG="USBCFG"':
                return _readback(self.configuration)
            if command == target.command:
                self.pending = parse_usbcfg_command(command)
                return {"ok": True, "terminal": "OK"}
            if command == "AT+CFUN=1,1":
                self.configuration = self.pending
                return {"operation": "cfun", "reenumerated": True}
            raise AssertionError(command)

    service = DJIService(replace(target, adb=False, audio=False), adb_available=True)
    result = asyncio.run(setup_qdc507_module(
        service, None, vendor_id=0x2CA3, product_id=0x4006,
    ))
    assert result["ready"] and result["identity"] == "2CA3:4006"
    assert service.configuration == target
    assert service.commands.count(target.command) == 1
    assert service.commands.count("AT+CFUN=1,1") == 1
    service.commands.clear()
    asyncio.run(setup_qdc507_module(service, None, vendor_id=0x2CA3, product_id=0x4006))
    assert target.command not in service.commands
    assert "AT+CFUN=1,1" not in service.commands
