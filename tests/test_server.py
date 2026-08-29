import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

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
