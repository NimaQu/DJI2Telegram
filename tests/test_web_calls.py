from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qdc507_gateway.api.app import create_app
from qdc507_gateway.events import EventBus
from qdc507_gateway.models import CallDirection, CallRecord, CallState
from qdc507_gateway.modem.service import ModuleServiceError
from qdc507_gateway.security import hash_token
from qdc507_gateway.storage.database import Database
from qdc507_gateway.telegram.calls import CallBridgeError, CallCoordinator
from qdc507_gateway.web.calls import (
    AUDIO_FRAME_BYTES,
    AUDIO_SUBPROTOCOL,
    AudioTicketStore,
    WebAudioSession,
    WebAudioDiagnosticService,
    WebCallController,
    extract_audio_ticket,
)


def test_audio_ticket_is_single_use_expiring_and_call_bound():
    store = AudioTicketStore(ttl_seconds=0.01)
    issued = store.issue("call-a")
    ticket = issued["ticket"]
    assert issued["subprotocol"] == AUDIO_SUBPROTOCOL
    assert not store.consume("call-b", ticket)
    assert not store.consume("call-a", ticket)

    ticket = store.issue("call-a")["ticket"]
    time.sleep(0.02)
    assert not store.consume("call-a", ticket)


def test_audio_ticket_subprotocol_parser_rejects_ambiguous_values():
    ticket = "A" * 43
    assert extract_audio_ticket(f"{AUDIO_SUBPROTOCOL}, ticket.{ticket}") == ticket
    assert extract_audio_ticket(f"ticket.{ticket}") is None
    assert extract_audio_ticket(
        f"{AUDIO_SUBPROTOCOL}, ticket.{ticket}, ticket.{'B' * 43}"
    ) is None


def test_web_outbound_waits_for_audio_before_dialing_and_cleans_up():
    async def run():
        actions = []

        async def mark(name, *args):
            actions.append((name, *args))

        controller = WebCallController(
            CallCoordinator(),
            lambda number: mark("dial", number),
            lambda: mark("answer"),
            lambda: mark("cellular_hangup"),
            lambda call_id: mark("audio_start", call_id),
            lambda: mark("audio_stop"),
        )
        record = await controller.start_outbound("+12045550100")
        assert record.frontend == "web"
        assert record.state == CallState.waiting_client
        assert actions == []

        await controller.attach_audio(record.id)
        assert actions == [
            ("audio_start", record.id),
            ("dial", "+12045550100"),
        ]
        assert (await controller.coordinator.current()).state == CallState.waiting_cellular

        await controller.cellular_connected()
        assert (await controller.coordinator.current()).state == CallState.active
        await controller.hangup(record.id)
        assert await controller.coordinator.current() is None
        assert actions[-2:] == [("audio_stop",), ("cellular_hangup",)]

    asyncio.run(run())


def test_websocket_requires_browser_pcm_before_attaching_audio_and_dialing():
    async def run():
        actions = []

        class Controller:
            async def attach_audio(self, call_id):
                actions.append(("attach", call_id))

            async def websocket_disconnected(self, call_id):
                actions.append(("cleanup", call_id))

        class Bridge:
            def __init__(self):
                self.received = []

            def push_telegram(self, frame):
                self.received.append(frame)
                return True

            @staticmethod
            def pull_for_telegram():
                return None

        class Adapter:
            pcm_bridge = Bridge()

        class WebSocket:
            def __init__(self):
                self.messages = iter((
                    {"type": "websocket.receive", "bytes": b"\1\0" * 160},
                    {"type": "websocket.disconnect"},
                ))

            async def receive(self):
                actions.append(("receive",))
                return next(self.messages)

            async def send_json(self, message):
                actions.append(("send", message["type"]))

        session = WebAudioSession(Controller(), Adapter())
        await session.run(WebSocket(), "call-with-mic")

        assert actions[0] == ("receive",)
        assert actions[1] == ("attach", "call-with-mic")
        assert ("send", "ready") in actions
        assert actions[-1] == ("cleanup", "call-with-mic")
        assert len(Adapter.pcm_bridge.received) == 1
        assert session.frames_from_browser == 1

    asyncio.run(run())


def test_websocket_without_browser_pcm_never_attaches_audio():
    async def run():
        actions = []

        class Controller:
            async def attach_audio(self, call_id):
                actions.append(("attach", call_id))

            async def websocket_disconnected(self, call_id):
                actions.append(("cleanup", call_id))

        class Adapter:
            pcm_bridge = object()

        class SilentWebSocket:
            @staticmethod
            async def receive():
                await asyncio.Event().wait()

        session = WebAudioSession(
            Controller(), Adapter(), startup_timeout_seconds=0.01,
        )
        with pytest.raises(CallBridgeError, match="produced no PCM"):
            await session.run(SilentWebSocket(), "call-without-mic")

        assert actions == [("cleanup", "call-without-mic")]

    asyncio.run(run())


def test_websocket_stream_stops_when_cellular_call_ends_first():
    async def run():
        ready = asyncio.Event()
        closed = asyncio.Event()

        async def noop(*_args):
            return None

        controller = WebCallController(
            CallCoordinator(),
            noop,
            noop,
            noop,
            noop,
            noop,
        )

        class Bridge:
            @staticmethod
            def push_telegram(_frame):
                return True

            @staticmethod
            def pull_for_telegram():
                return None

        class Adapter:
            pcm_bridge = Bridge()

        class WebSocket:
            def __init__(self):
                self.messages = asyncio.Queue()
                self.messages.put_nowait({
                    "type": "websocket.receive",
                    "bytes": b"\1\0" * 160,
                })

            async def receive(self):
                return await self.messages.get()

            async def send_json(self, message):
                if message.get("type") == "ready":
                    ready.set()

            async def send_bytes(self, _data):
                return None

            async def close(self, **_kwargs):
                closed.set()
                self.messages.put_nowait({"type": "websocket.disconnect"})

        record = await controller.start_outbound("+12045550100")
        session = WebAudioSession(controller, Adapter())
        task = asyncio.create_task(session.run(WebSocket(), record.id))
        await asyncio.wait_for(ready.wait(), timeout=1)

        await controller.cellular_disconnected()
        await asyncio.wait_for(task, timeout=1)

        assert closed.is_set()
        assert await controller.coordinator.current() is None

    asyncio.run(run())


def test_web_incoming_requires_audio_before_answering():
    async def run():
        actions = []

        async def mark(name, *args):
            actions.append((name, *args))

        controller = WebCallController(
            CallCoordinator(),
            lambda number: mark("dial", number),
            lambda: mark("answer"),
            lambda: mark("cellular_hangup"),
            lambda call_id: mark("audio_start", call_id),
            lambda: mark("audio_stop"),
        )
        record = await controller.start_inbound("+12045550100")
        assert record.direction == CallDirection.inbound_cellular
        assert record.state == CallState.ringing_cellular
        with pytest.raises(CallBridgeError, match="audio must be connected"):
            await controller.answer(record.id)
        assert actions == []

        await controller.attach_audio(record.id)
        assert actions == [("audio_start", record.id)]
        answered = await controller.answer(record.id)
        assert answered.state == CallState.active
        assert actions[-1] == ("answer",)
        await controller.hangup(record.id)

    asyncio.run(run())


def test_repeated_incoming_ring_updates_late_caller_id():
    async def run():
        records = []

        async def noop(*_args):
            return None

        async def record(value):
            records.append(value.cellular_number)

        controller = WebCallController(
            CallCoordinator(),
            noop,
            noop,
            noop,
            noop,
            noop,
            record_sink=record,
        )
        first = await controller.start_inbound(None)
        second = await controller.start_inbound("+12045550100")
        assert second is first
        assert second.cellular_number == "+12045550100"
        assert records == [None, "+12045550100"]
        await controller.hangup(first.id)

    asyncio.run(run())


def test_web_call_timeout_completes_without_cancelling_its_own_cleanup():
    async def run():
        records = []

        async def noop(*_args):
            return None

        async def record(value):
            records.append(value.state)

        controller = WebCallController(
            CallCoordinator(), noop, noop, noop, noop, noop,
            record_sink=record,
            timeout_seconds=0.01,
        )
        await controller.start_outbound("+12045550100")
        await asyncio.sleep(0.04)
        assert await controller.coordinator.current() is None
        assert records[-1] == CallState.ended

    asyncio.run(run())


def test_web_call_failure_keeps_safe_module_error_detail():
    async def run():
        records = []

        async def fail_dial(_number):
            raise ModuleServiceError("AT monitor did not become ready")

        async def noop(*_args):
            return None

        async def record(value):
            records.append(value)

        controller = WebCallController(
            CallCoordinator(),
            fail_dial,
            noop,
            noop,
            noop,
            noop,
            record_sink=record,
        )
        started = await controller.start_outbound("+12045550100")
        with pytest.raises(ModuleServiceError):
            await controller.attach_audio(started.id)
        assert records[-1].state == CallState.failed
        assert records[-1].last_error == (
            "ModuleServiceError: AT monitor did not become ready"
        )

    asyncio.run(run())


def test_audio_diagnostic_uses_alsa_media_path_without_cellular_commands():
    async def run():
        actions = []

        class Adapter:
            async def start_web(self, identity):
                actions.append(("audio_start", identity))

            async def stop(self):
                actions.append(("audio_stop",))

        class Session:
            async def stream(self, _websocket, session_id, *, session_type):
                actions.append(("stream", session_id, session_type))

        tickets = AudioTicketStore()
        service = WebAudioDiagnosticService(
            CallCoordinator(), Adapter(), Session(), tickets,
        )
        created = await service.create()
        issued = await service.issue_ticket(created["id"])
        assert await service.consume_ticket(created["id"], issued["ticket"])
        await service.run(object(), created["id"])
        assert actions == [
            ("audio_start", "diagnostic:" + created["id"]),
            ("stream", created["id"], "diagnostic"),
            ("audio_stop",),
        ]
        assert not service.active
        assert not any(item[0] in {"dial", "answer"} for item in actions)

    asyncio.run(run())


def test_web_api_call_control_static_ui_and_one_time_websocket_ticket():
    database = Database(":memory:")
    token = "web-test-token"
    database.replace_token(
        hash_token(token),
        "2026-01-01T00:00:00Z",
    )
    tickets = AudioTicketStore()
    active = CallRecord(
        id="web-call",
        direction=CallDirection.outbound_cellular,
        state=CallState.waiting_client,
        cellular_number="+12045550100",
        frontend="web",
    )

    async def start(number):
        active.cellular_number = number
        return active

    async def current():
        return active

    async def answer(_call_id):
        active.state = CallState.active
        return active

    async def issue(call_id):
        assert call_id == active.id
        return tickets.issue(call_id)

    async def run_socket(websocket, call_id):
        await websocket.send_json({"type": "ready", "call_id": call_id})
        await websocket.close(code=1000)

    async def start_diagnostic():
        return {"id": "diagnostic-session", "state": "waiting_websocket"}

    async def issue_diagnostic(session_id):
        return tickets.issue("diagnostic:" + session_id)

    async def consume_diagnostic(session_id, ticket):
        return tickets.consume("diagnostic:" + session_id, ticket)

    app = create_app(database, EventBus(), {
        "start_web_call": start,
        "current_call": current,
        "answer_web_call": answer,
        "issue_audio_ticket": issue,
        "consume_audio_ticket": tickets.consume,
        "run_audio_websocket": run_socket,
        "start_audio_diagnostic": start_diagnostic,
        "issue_audio_diagnostic_ticket": issue_diagnostic,
        "consume_audio_diagnostic_ticket": consume_diagnostic,
        "run_audio_diagnostic_websocket": run_socket,
    })
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + token}

    assert client.get("/web/").status_code == 200
    web_response = client.get("/web/")
    assert web_response.headers["cache-control"] == "no-store, no-cache, must-revalidate"
    assert web_response.headers["pragma"] == "no-cache"
    web_page = web_response.text
    assert "QDC507 控制台" in web_page
    assert "localStorage" in web_page
    assert 'href="/web/styles.css?v=0.4.0"' in web_page
    assert 'src="/web/app.js?v=0.4.1"' in web_page
    assert 'id="signalDbm"' in web_page
    assert 'id="signalBars"' in web_page
    assert 'id="answerButton"' not in web_page
    web_script = client.get("/web/app.js").text
    assert 'TOKEN_STORAGE_KEY = "qdc507.gateway.bearer-token.v1"' in web_script
    assert "window.localStorage.setItem(TOKEN_STORAGE_KEY, value)" in web_script
    assert "connectGateway({ automatic: true })" in web_script
    assert "MICROPHONE_REQUEST_TIMEOUT_MS = 12000" in web_script
    assert "正在请求浏览器麦克风权限" in web_script
    assert "复用已授权的浏览器麦克风" in web_script
    assert "浏览器麦克风复用失活，正在重建" in web_script
    assert "BROWSER_CAPTURE_START_TIMEOUT_MS = 2000" in web_script
    assert "waitForBrowserCaptureFrame" in web_script
    assert 'track.readyState === "live"' in web_script
    assert "audioContext.suspend" not in web_script
    assert "intentionallyClosedSockets" in web_script
    assert "audioSocket === socket" in web_script
    assert "事件流断开，正在自动重连" in web_script
    assert "eventLoopGeneration" in web_script
    assert "renderSignal(module.connected ? module.signal : null)" in web_script
    assert 'Bot ${status.telegram_bot_state || "disabled"}' in web_script
    assert 'labels = ["未采样", "极弱", "较弱", "一般", "良好", "很强"]' in web_script
    assert "answerIncomingCall" not in web_script
    assert "/answer" not in web_script
    started = client.post(
        "/api/v1/calls/start",
        json={"number": "+1 204-555-0100"},
        headers=headers,
    )
    assert started.status_code == 200
    assert started.json()["cellular_number"] == "+12045550100"

    # The built-in page has no incoming-call controls, but the authenticated
    # control plane remains available to a future standalone application.
    answered = client.post(
        "/api/v1/calls/web-call/answer",
        headers=headers,
    )
    assert answered.status_code == 200
    assert answered.json()["state"] == "active"

    ticket_response = client.post(
        "/api/v1/calls/web-call/audio-ticket",
        headers=headers,
    )
    assert ticket_response.status_code == 200
    issued = ticket_response.json()
    protocols = [AUDIO_SUBPROTOCOL, "ticket." + issued["ticket"]]
    with client.websocket_connect(
        "/api/v1/calls/web-call/audio", subprotocols=protocols
    ) as websocket:
        assert websocket.accepted_subprotocol == AUDIO_SUBPROTOCOL
        assert websocket.receive_json()["type"] == "ready"

    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            "/api/v1/calls/web-call/audio", subprotocols=protocols
        ):
            pass
    assert error.value.code == 4401

    diagnostic = client.post("/api/v1/audio/diagnostic/start", headers=headers)
    assert diagnostic.status_code == 200
    diagnostic_id = diagnostic.json()["id"]
    diagnostic_ticket = client.post(
        f"/api/v1/audio/diagnostic/{diagnostic_id}/ticket",
        headers=headers,
    ).json()
    with client.websocket_connect(
        f"/api/v1/audio/diagnostic/{diagnostic_id}",
        subprotocols=[
            AUDIO_SUBPROTOCOL,
            "ticket." + diagnostic_ticket["ticket"],
        ],
    ) as websocket:
        assert websocket.receive_json()["call_id"] == diagnostic_id


def test_websocket_pcm_frame_size_constant_is_twenty_milliseconds():
    assert AUDIO_FRAME_BYTES == 8000 * 20 // 1000 * 2


def test_audio_websocket_returns_safe_module_error_detail():
    database = Database(":memory:")
    events = EventBus()
    tickets = AudioTicketStore()

    async def fail_socket(_websocket, _call_id):
        raise ModuleServiceError("AT monitor connection is unavailable")

    app = create_app(database, events, {
        "consume_audio_ticket": tickets.consume,
        "run_audio_websocket": fail_socket,
    })
    issued = tickets.issue("failed-call")
    protocols = [AUDIO_SUBPROTOCOL, "ticket." + issued["ticket"]]
    with TestClient(app).websocket_connect(
        "/api/v1/calls/failed-call/audio",
        subprotocols=protocols,
    ) as websocket:
        assert websocket.receive_json() == {
            "type": "error",
            "detail": (
                "web audio session failed: ModuleServiceError: "
                "AT monitor connection is unavailable"
            ),
        }
