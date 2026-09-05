import asyncio
import json

import pytest

from qdc507_gateway.cli import main
from qdc507_gateway.config import ConfigurationError, Settings
from qdc507_gateway.network_setup import NetworkSetupError, setup_network


class Modem:
    def __init__(self, apn="old", registered=False):
        self.contexts = {1: ["IP", apn], 2: ["IPV4V6", "ims"], 3: ["IPV4V6", "sos"]}
        self.registered = registered
        self.commands = []
        self.mode = 0
        self.radio = 1
        self.fail_write = False
        self.attach_success = True

    async def at(self, command, timeout_ms=5000):
        self.commands.append(command)
        lines = []
        if command == "AT+CGDCONT?":
            lines = [f'+CGDCONT: {cid},"{pdp}","{apn}","0.0.0.0",0,0' for cid, (pdp, apn) in self.contexts.items()]
        elif command == "AT+CEREG?":
            lines = [f'+CEREG: 0,{1 if self.registered else 3}']
        elif command == "AT+COPS?":
            lines = [f'+COPS: {self.mode}']
        elif command == "AT+CFUN?":
            lines = [f'+CFUN: {self.radio}']
        elif command == "AT+COPS=0":
            self.mode = 0
        elif command in ("AT+CFUN=0", "AT+CFUN=1"):
            self.radio = int(command[-1])
            if self.radio:
                self.registered = self.attach_success
            return {"operation": "cfun", "changed": True}
        elif command.startswith("AT+CGDCONT="):
            assert command.startswith('AT+CGDCONT=1,')
            assert self.radio == 0
            if self.fail_write:
                return {"ok": False}
            import csv
            _, pdp, apn = next(csv.reader([command.split("=", 1)[1]]))
            self.contexts[1] = [pdp, apn]
        else:
            raise AssertionError(command)
        return {"ok": True, "lines": lines, "terminal": "OK"}


def test_unspecified_apn_is_read_only(tmp_path):
    modem = Modem()
    result = asyncio.run(setup_network(modem, Settings(), backup_dir=tmp_path))
    assert result["mode"] == "keep" and not result["registered"]
    assert all(command.endswith("?") for command in modem.commands)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("apn", ["connect", ""])
def test_manual_and_subscription_apn_preserve_ims_sos(tmp_path, apn):
    modem = Modem()
    modem.mode = 1
    result = asyncio.run(setup_network(modem, Settings(network_apn=apn), backup_dir=tmp_path))
    assert result["registered"] and result["changed"] and result["reattached"]
    assert modem.contexts == {1: ["IP", apn], 2: ["IPV4V6", "ims"], 3: ["IPV4V6", "sos"]}
    assert modem.commands.count("AT+CFUN=0") == modem.commands.count("AT+CFUN=1") == 1
    saved = list(tmp_path.glob("*.json"))[0]
    assert json.loads(saved.read_text())["contexts"]["1"]["apn"] == "old"
    assert saved.stat().st_mode & 0o777 == 0o600
    modem.commands.clear()
    result = asyncio.run(setup_network(modem, Settings(network_apn=apn), backup_dir=tmp_path))
    assert not result["changed"] and not result["reattached"]
    assert all(command.endswith("?") for command in modem.commands)


def test_failed_apn_write_restores_radio_without_retry(tmp_path):
    modem = Modem()
    modem.fail_write = True
    with pytest.raises(NetworkSetupError, match="original network settings saved"):
        asyncio.run(setup_network(modem, Settings(network_apn="connect"), backup_dir=tmp_path))
    assert modem.radio == 1
    assert modem.commands.count('AT+CGDCONT=1,"IP","connect"') == 1
    assert modem.contexts[1][1] == "old"


def test_registration_denial_is_not_success_or_apn_fallback(tmp_path):
    modem = Modem()
    modem.attach_success = False
    result = asyncio.run(setup_network(modem, Settings(network_apn="connect"), backup_dir=tmp_path, wait_seconds=0))
    assert not result["registered"]
    assert result["registration"]["denied"]
    assert modem.commands.count('AT+CGDCONT=1,"IP","connect"') == 1
    assert 'AT+CGDCONT=1,"IP",""' not in modem.commands


def test_protected_context_one_is_not_overwritten(tmp_path):
    modem = Modem(apn="ims")
    with pytest.raises(NetworkSetupError, match="reserved for IMS/SOS"):
        asyncio.run(setup_network(modem, Settings(network_apn="connect"), backup_dir=tmp_path))
    assert all(command.endswith("?") for command in modem.commands)


@pytest.mark.parametrize("value", ['bad"apn', 'foo\rAT+CFUN=0', 'bad apn', 123, 'x' * 101])
def test_apn_validation_prevents_at_injection(value):
    with pytest.raises(ConfigurationError, match="network.apn"):
        Settings(network_apn=value)


def test_empty_apn_survives_toml_and_environment(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[network]\napn=""\npdp_type="IPV4V6"\n')
    settings = Settings.load(path, environ={})
    assert settings.network_apn == "" and settings.network_pdp_type == "IPV4V6"
    assert Settings.load(path, environ={"QDC507_NETWORK_APN": "connect"}).network_apn == "connect"


def test_network_setup_requires_confirmation_before_loading_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="pass --confirm"):
        main(["network-setup"])


def test_backup_failure_prevents_radio_changes(tmp_path):
    path = tmp_path / "blocked"
    path.write_text("x")
    modem = Modem()
    with pytest.raises(FileExistsError):
        asyncio.run(setup_network(modem, Settings(network_apn="connect"), backup_dir=path))
    assert all(command.endswith("?") for command in modem.commands)


def test_network_cli_preserves_empty_apn_and_reports_denial(tmp_path, monkeypatch, capsys):
    from qdc507_gateway import network_setup
    from qdc507_gateway.modem import service
    (tmp_path / "config.toml").write_text('[app]\ndata_dir="data"\n[network]\napn=""\n')
    monkeypatch.chdir(tmp_path)
    class FakeService:
        def __init__(self, *args, **kwargs):
            pass
        def close(self):
            pass
    async def run(modem, settings, **kwargs):
        assert settings.network_apn == ""
        return {"registered": False, "registration": {"status": 3}, "changed": True}
    monkeypatch.setattr(service, "LiveModuleService", FakeService)
    monkeypatch.setattr(network_setup, "setup_network", run)
    assert main(["network-setup", "--confirm"]) == 2
    assert json.loads(capsys.readouterr().out)["registration"]["status"] == 3
