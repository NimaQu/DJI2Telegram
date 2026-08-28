import asyncio
from types import SimpleNamespace

from qdc507_gateway.audio.bridge import PCMBridge
from qdc507_gateway.audio.ring import PCMFrame
from qdc507_gateway.models import CallState
from qdc507_gateway.telegram.calls import CallBridgeOrchestrator, CallCoordinator
from qdc507_gateway.telegram.kurigram import (
    NTGCALLS_AUDIO_FRAME_BYTES,
    NTGCALLS_AUDIO_SAMPLE_RATE,
    KurigramPyTgCallsBridge,
)


def test_outbound_call_waits_for_both_legs_and_cleans_up_symmetrically():
    async def run():
        actions = []

        async def request(user_id):
            actions.append(("telegram_request", user_id))
            return "tg-handle"

        async def telegram_hangup(handle):
            actions.append(("telegram_hangup", handle))

        async def dial(number):
            actions.append(("dial", number))

        async def answer():
            actions.append("answer")

        async def cellular_hangup():
            actions.append("cellular_hangup")

        async def audio_start(handle):
            actions.append(("audio_start", handle))

        async def audio_stop():
            actions.append("audio_stop")

        async def dial_cue():
            actions.append("dial_cue")

        coordinator = CallCoordinator()
        bridge = CallBridgeOrchestrator(
            coordinator, 42, request, telegram_hangup, dial, answer,
            cellular_hangup, audio_start, audio_stop,
            audio_dial_cue=dial_cue,
        )
        record = await bridge.start_outbound("+12045550100")
        assert record.state == CallState.waiting_telegram
        await bridge.telegram_connected()
        assert ("dial", "+12045550100") in actions
        assert actions.index(("audio_start", "tg-handle")) < actions.index(
            ("dial", "+12045550100")
        )
        assert actions.index(("dial", "+12045550100")) < actions.index("dial_cue")
        assert (await bridge.current()).state == CallState.waiting_cellular
        await bridge.cellular_connected()
        assert (await bridge.current()).state == CallState.active
        assert actions.count(("audio_start", "tg-handle")) == 1
        await bridge.hangup(record.id)
        assert await bridge.current() is None
        assert actions[-3:] == ["audio_stop", "cellular_hangup", ("telegram_hangup", "tg-handle")]

    asyncio.run(run())


def test_inbound_call_answers_cellular_leg_after_telegram_connects():
    async def run():
        actions = []

        async def record(name, *args):
            actions.append((name, *args))
            return "handle" if name == "request" else None

        bridge = CallBridgeOrchestrator(
            CallCoordinator(), 42,
            lambda user_id: record("request", user_id),
            lambda handle: record("telegram_hangup", handle),
            lambda number: record("dial", number),
            lambda: record("answer"),
            lambda: record("cellular_hangup"),
            lambda handle: record("audio_start", handle),
            lambda: record("audio_stop"),
        )
        await bridge.start_inbound("+12045550100")
        await bridge.telegram_connected()
        assert (await bridge.current()).state == CallState.active
        assert ("answer",) in actions
        assert actions.index(("audio_start", "handle")) < actions.index(("answer",))
        await bridge.cellular_disconnected()
        assert await bridge.current() is None

    asyncio.run(run())


def test_outbound_audio_failure_does_not_dial_cellular_leg():
    async def run():
        actions = []

        async def request(_user_id):
            return "handle"

        async def mark(name, *args):
            actions.append((name, *args))

        async def fail_audio(_handle):
            actions.append(("audio_start",))
            raise RuntimeError("audio prewarm failed")

        bridge = CallBridgeOrchestrator(
            CallCoordinator(),
            42,
            request,
            lambda handle: mark("telegram_hangup", handle),
            lambda number: mark("dial", number),
            lambda: mark("answer"),
            lambda: mark("cellular_hangup"),
            fail_audio,
            lambda: mark("audio_stop"),
        )
        await bridge.start_outbound("+12045550100")
        try:
            await bridge.telegram_connected()
        except RuntimeError as exc:
            assert str(exc) == "audio prewarm failed"
        else:
            raise AssertionError("audio prewarm failure was accepted")
        assert not any(item[0] == "dial" for item in actions)

    asyncio.run(run())


def test_call_timeout_cleans_up():
    async def run():
        calls = []

        async def mark(name, *args):
            calls.append(name)
            return "handle" if name == "request" else None

        bridge = CallBridgeOrchestrator(
            CallCoordinator(), 42,
            lambda user_id: mark("request", user_id),
            lambda handle: mark("telegram_hangup", handle),
            lambda number: mark("dial", number),
            lambda: mark("answer"),
            lambda: mark("cellular_hangup"),
            lambda handle: mark("audio_start", handle),
            lambda: mark("audio_stop"),
            timeout_seconds=0.01,
        )
        await bridge.start_inbound(None)
        await asyncio.sleep(0.03)
        assert await bridge.current() is None
        assert "telegram_hangup" in calls

    asyncio.run(run())


def test_concurrent_hangups_cleanup_only_once():
    async def run():
        actions = []

        async def request(_user_id):
            return "handle"

        async def mark(name, *args):
            actions.append((name, *args))

        bridge = CallBridgeOrchestrator(
            CallCoordinator(), 42,
            request,
            lambda handle: mark("telegram_hangup", handle),
            lambda number: mark("dial", number),
            lambda: mark("answer"),
            lambda: mark("cellular_hangup"),
            lambda handle: mark("audio_start", handle),
            lambda: mark("audio_stop"),
        )
        record = await bridge.start_inbound("+12045550100")
        await asyncio.gather(bridge.hangup(record.id), bridge.hangup(record.id))
        assert [item[0] for item in actions].count("telegram_hangup") == 1
        assert [item[0] for item in actions].count("cellular_hangup") == 1
        assert await bridge.current() is None

    asyncio.run(run())


def test_kurigram_private_call_cleanup_stops_ntgcalls_binding():
    async def run():
        class Engine:
            async def leave_call(self, chat_id, close=False):
                actions.append(("leave_call", chat_id, close))

            async def discard_call(self, chat_id, is_missed=False):
                actions.append(("discard_call", chat_id, is_missed))

        actions = []
        bridge = object.__new__(KurigramPyTgCallsBridge)
        bridge.engine = Engine()
        bridge._pcm_bindings = []
        bridge._private_calls = []

        async def play(_user_id, _stream=None):
            await asyncio.sleep(60)

        bridge.play = play
        handle = await bridge.start_private_call(42)
        await bridge.stop_private_call(handle)
        assert actions == [("leave_call", 42, False)]
        assert bridge._private_calls == []

    asyncio.run(run())


def test_kurigram_pcm_binding_enables_record_stream_and_moves_both_directions():
    async def run():
        from pytgcalls.types import Device

        class Engine:
            def __init__(self):
                self.handlers = []
                self.records = []
                self.sent = []
                self.removed = []

            def on_update(self, *_args, **_kwargs):
                def decorator(callback):
                    self.handlers.append(callback)
                    return callback

                return decorator

            async def record(self, chat_id, media):
                self.records.append((chat_id, media))

            async def send_frame(self, chat_id, device, data):
                self.sent.append((chat_id, device, data))

            def remove_handler(self, handler):
                self.removed.append(handler)

        engine = Engine()
        bridge = object.__new__(KurigramPyTgCallsBridge)
        bridge.engine = engine
        bridge._pcm_bindings = []
        bridge._private_calls = []
        pcm = PCMBridge(capacity=10)
        await pcm.start()

        binding = await bridge.attach_pcm(42, pcm)
        assert engine.records[0][0] == 42
        assert engine.records[0][1] is not None

        await engine.handlers[0](
            engine,
            SimpleNamespace(
                chat_id=42,
                device=Device.MICROPHONE,
                frames=[SimpleNamespace(frame=b"\x01\x00" * 80)],
            ),
        )
        incoming = pcm.pull_for_cellular()
        assert incoming is not None
        assert incoming.data == b"\x01\x00" * 80
        assert incoming.sample_rate == NTGCALLS_AUDIO_SAMPLE_RATE

        pcm.push_cellular(PCMFrame(b"\x02\x00" * 160))
        await asyncio.sleep(0.03)
        assert len(engine.sent) == 2
        assert all(item[0] == 42 for item in engine.sent)
        assert all(len(item[2]) == NTGCALLS_AUDIO_FRAME_BYTES for item in engine.sent)
        assert b"".join(item[2] for item in engine.sent) == b"\x02\x00" * 160

        engine.sent.clear()
        assert pcm.queue_telegram_cue(duration_ms=20)
        pcm.push_cellular(PCMFrame(b"\0" * 320))
        await asyncio.sleep(0.03)
        assert len(engine.sent) == 2
        assert any(any(payload) for _chat_id, _device, payload in engine.sent)

        await bridge.detach_pcm(binding)
        assert len(engine.records) == 1
        assert engine.removed == [engine.handlers[0]]

    asyncio.run(run())


def test_kurigram_pcm_cleanup_does_not_restart_call_via_record():
    async def run():
        class Engine:
            def __init__(self):
                self.handler = None
                self.records = []

            def on_update(self, *_args, **_kwargs):
                def decorator(callback):
                    self.handler = callback
                    return callback

                return decorator

            async def record(self, chat_id, media):
                self.records.append((chat_id, media))

            async def send_frame(self, *_args):
                return None

            def remove_handler(self, _handler):
                return None

        bridge = object.__new__(KurigramPyTgCallsBridge)
        bridge.engine = Engine()
        bridge._pcm_bindings = []
        bridge._private_calls = []
        pcm = PCMBridge(capacity=1)
        await pcm.start()
        binding = await bridge.attach_pcm(42, pcm)
        await bridge.detach_pcm(binding)
        assert len(bridge.engine.records) == 1
        assert bridge.engine.records[0][1] is not None

    asyncio.run(run())


def test_kurigram_private_call_propagates_remote_discard():
    async def run():
        from pytgcalls.types import ChatUpdate

        class Engine:
            def __init__(self):
                self.handlers = []
                self.removed = []

            async def leave_call(self, *_args, **_kwargs):
                return None

            def on_update(self, *_args, **_kwargs):
                def decorator(callback):
                    self.handlers.append(callback)
                    return callback
                return decorator

            def remove_handler(self, callback):
                self.removed.append(callback)

        engine = Engine()
        bridge = object.__new__(KurigramPyTgCallsBridge)
        bridge.engine = engine
        bridge._pcm_bindings = []
        bridge._private_calls = []

        async def play(_user_id, _stream=None):
            await asyncio.sleep(60)

        bridge.play = play
        disconnected = []
        handle = await bridge.start_private_call(
            42,
            on_disconnected=lambda call: _record(call, disconnected),
        )
        await engine.handlers[0](engine, ChatUpdate(42, ChatUpdate.Status.DISCARDED_CALL))
        assert disconnected == [handle]
        await bridge.stop_private_call(handle)
        assert engine.removed == [engine.handlers[0]]

    async def _record(value, target):
        target.append(value)

    asyncio.run(run())
