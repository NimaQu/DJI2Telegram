import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from qdc507_gateway.api.app import create_app
from qdc507_gateway.calls.core import CallCoordinator, CallBridgeError
from qdc507_gateway.config import Settings, ConfigurationError
from qdc507_gateway.events import EventBus
from qdc507_gateway.maintenance import MaintenanceController
from qdc507_gateway.security import hash_token
from qdc507_gateway.storage.database import Database


@pytest.mark.asyncio
async def test_maintenance_auth_validation_busy_and_restart(monkeypatch):
    operations = []

    async def at(command, timeout_ms):
        operations.append((command, timeout_ms))
        return {"terminal": "OK", "lines": ["+CSQ: 20,99"]}

    calls = CallCoordinator()
    diagnostic = SimpleNamespace(active=False)
    maintenance = MaintenanceController(Settings(), SimpleNamespace(at=at), calls, diagnostic)
    db = Database(":memory:")
    db.replace_token(hash_token("test"), "2026-09-10")
    app = create_app(
        db,
        EventBus(),
        {
            "at": maintenance.at,
            "restart_module": maintenance.restart_module,
            "restart_service": maintenance.restart_service,
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for endpoint in ["module/at", "module/restart", "service/restart"]:
            assert (
                await client.post("/api/v1/" + endpoint, json={"command": "AT"})
            ).status_code == 401
        client.headers["Authorization"] = "Bearer test"
        for command in ["AT\n", "AT\rATD123;", "AT\x7f", "你好", "ls", "AT" + "X" * 1024]:
            assert (
                await client.post("/api/v1/module/at", json={"command": command})
            ).status_code == 422
        assert not operations
        result = await client.post(
            "/api/v1/module/at", json={"command": "AT+CSQ", "timeout_ms": 4000}
        )
        assert result.status_code == 200 and result.json()["terminal"] == "OK"
        assert operations == [("AT+CSQ", 4000)]
        assert (
            await client.post("/api/v1/module/at", json={"command": "AT+CFUN=1,1"})
        ).status_code == 409
        assert (await client.post("/api/v1/service/restart")).status_code == 403
        for busy in ["call", "diagnostic"]:
            if busy == "call":
                await calls.start_inbound("+123")
            else:
                diagnostic.active = True
            for endpoint in ["module/at", "module/restart"]:
                assert (
                    await client.post("/api/v1/" + endpoint, json={"command": "AT"})
                ).status_code == 409
            calls.active = None
            diagnostic.active = False
        assert (await client.post("/api/v1/module/restart")).status_code == 200
        assert operations[-1] == ("AT+CFUN=1,1", 10000)
        maintenance.settings = replace(maintenance.settings, allow_service_restart=True)
        gate = asyncio.Event()

        async def delayed():
            await gate.wait()

        monkeypatch.setattr(maintenance, "_restart_later", delayed)
        for _ in range(2):
            result = await client.post("/api/v1/service/restart")
            assert result.status_code == 202 and result.json()["unit"] == "djisimhub.service"
        assert len(maintenance.tasks) == 1
        assert (await client.post("/api/v1/module/at", json={"command": "AT"})).status_code == 409
        with pytest.raises(CallBridgeError):
            maintenance.require_available()
    await maintenance.stop()
    db.close()


@pytest.mark.asyncio
async def test_restart_exec_uses_fixed_argv_and_recovers_from_failure(monkeypatch):
    import qdc507_gateway.maintenance as module

    calls = CallCoordinator()
    control = MaintenanceController(
        Settings(allow_service_restart=True), None, calls, SimpleNamespace(active=False)
    )
    seen = []

    async def no_sleep(_):
        pass

    async def process(*args, **kwargs):
        seen.append(args)

        async def wait():
            return 1

        return SimpleNamespace(wait=wait)

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", process)
    control.restart_pending = True
    await control._restart_later()
    assert seen == [("systemctl", "--no-block", "restart", "djisimhub.service")]
    assert not control.restart_pending


def test_restart_config_and_no_messaging_dependencies(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[server]\nallow_service_restart=true\nsystemd_unit="custom.service"\n')
    settings = Settings.load(config)
    assert settings.allow_service_restart and settings.systemd_unit == "custom.service"
    with pytest.raises(ConfigurationError):
        Settings(systemd_unit="anything; reboot")


def test_old_call_schema_preserves_calls_and_hides_retired_metadata(tmp_path):
    import sqlite3
    from qdc507_gateway.models import CallRecord, CallDirection, CallState

    path = tmp_path / "gateway.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE call_records (id TEXT PRIMARY KEY, direction TEXT, state TEXT, cellular_number TEXT, telegram_user_id INTEGER, frontend TEXT, started_at TEXT, connected_at TEXT, ended_at TEXT, last_error TEXT)"
        )
        conn.execute(
            "INSERT INTO call_records VALUES ('old','inbound_cellular','ended','123',42,'app','2026-09-10',NULL,NULL,NULL)"
        )
    db = Database(path)
    assert db.get_call("old")["cellular_number"] == "123"
    assert "telegram_user_id" not in db.get_call("old").keys()
    db.save_call(
        CallRecord(id="new", direction=CallDirection.inbound_cellular, state=CallState.ended)
    )
    db.close()
    db = Database(path)
    assert len(db.list_calls()) == 2
    db.close()


@pytest.mark.asyncio
async def test_at_serializes_with_new_calls_and_busy_restart_is_rejected(tmp_path):
    from qdc507_gateway.server import build_app

    app = build_app(Settings(data_dir=tmp_path, allow_service_restart=True))
    module = app.state.gateway_module_service
    entered, release = asyncio.Event(), asyncio.Event()

    async def at(command, timeout_ms):
        entered.set()
        await release.wait()
        return {"ok": True}

    module.at = at
    # The public handlers share the client's operation lock.
    db = app.state.gateway_database
    db.replace_token(hash_token("test"), "2026-09-10")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as client:
        command = asyncio.create_task(client.post("/api/v1/module/at", json={"command": "AT"}))
        await entered.wait()
        call_task = asyncio.create_task(app.state.gateway_web_calls.start_outbound("+123"))
        await asyncio.sleep(0.01)
        assert not call_task.done()
        release.set()
        assert (await command).status_code == 200
        await call_task
        assert (await client.post("/api/v1/service/restart")).status_code == 409

        async def noop(*args):
            pass

        app.state.gateway_web_calls.cellular_hangup = noop
        await app.state.gateway_web_calls.hangup()
    module.close()
    await app.state.gateway_runtime.stop()
    db.close()
