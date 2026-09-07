import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from qdc507_gateway.config import Settings
from qdc507_gateway import server


def test_headless_runner_enters_gateway_lifespan_without_http_server():
    async def scenario():
        transitions = []

        @asynccontextmanager
        async def lifespan(_app):
            transitions.append("started")
            try:
                yield
            finally:
                transitions.append("stopped")

        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan))
        stop = asyncio.Event()
        stop.set()
        await server.run_headless(app, stop)
        assert transitions == ["started", "stopped"]

    asyncio.run(scenario())


def test_disabled_server_selects_headless_runner(monkeypatch, tmp_path):
    app = object()
    started = []

    async def headless(value):
        started.append(value)

    monkeypatch.setattr(server, "build_app", lambda _settings: app)
    monkeypatch.setattr(server, "run_headless", headless)

    settings = Settings(
        data_dir=tmp_path,
        lock_path=tmp_path / "device.lock",
        web_enabled=False,
    )
    assert server.run(settings) == 0
    assert started == [app]


# Exercise the real lifespan: cleanup order and failure propagation matter for
# releasing the USB device and database after partial startup or failed shutdown.
@pytest.mark.parametrize("failure", [
    None, "startup", "diagnostic", "hangup", "monitor", "module",
    "telegram", "runtime", "apns", "database",
])
def test_gateway_cleanup_continues_after_failure(monkeypatch, tmp_path, failure):
    transitions = []

    def sync_step(name):
        def run(*args, **kwargs):
            transitions.append(name)
            if failure == name:
                raise RuntimeError(name)
        return run

    def async_step(name):
        async def run(*args, **kwargs):
            sync_step(name)()
        return run

    async def idle(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(server.APNsService, "start", async_step("startup"))
    monkeypatch.setattr(server.GatewayRuntime, "probe_once", async_step("probe"))
    monkeypatch.setattr(server.KurigramTelegramService, "start", async_step("telegram_start"))
    monkeypatch.setattr(server.LiveModuleService, "start_monitor", async_step("monitor_start"))
    monkeypatch.setattr(server, "monitor_cellular_call_status", idle)
    monkeypatch.setattr(server.LiveModuleService, "signal", idle)
    monkeypatch.setattr(server.LiveModuleService, "network_status", idle)
    for cls, method, name in (
        (server.WebAudioDiagnosticService, "stop", "diagnostic"),
        (server.CallBridgeOrchestrator, "hangup", "hangup"),
        (server.LiveModuleService, "stop_monitor", "monitor"),
        (server.KurigramTelegramService, "stop", "telegram"),
        (server.GatewayRuntime, "stop", "runtime"),
        (server.APNsService, "stop", "apns"),
    ):
        monkeypatch.setattr(cls, method, async_step(name))
    monkeypatch.setattr(server.LiveModuleService, "close", sync_step("module"))
    original_close = server.Database.close

    def close_database(database):
        original_close(database)
        sync_step("database")()

    monkeypatch.setattr(server.Database, "close", close_database)
    monkeypatch.setattr(server, "LibUSBDeviceLocator", lambda: SimpleNamespace())
    app = server.build_app(Settings(data_dir=tmp_path, lock_path=tmp_path / "device.lock"))

    async def scenario():
        async with app.router.lifespan_context(app):
            pass

    if failure:
        with pytest.raises(RuntimeError, match=f"^{failure}$"):
            asyncio.run(scenario())
    else:
        asyncio.run(scenario())
    assert transitions[-8:] == [
        "diagnostic", "hangup", "monitor", "module",
        "telegram", "runtime", "apns", "database",
    ]
