import asyncio

import pytest

from qdc507_gateway.events import EventBus
from qdc507_gateway.models import EndpointDescriptor, InterfaceDescriptor, USBDeviceSnapshot
from qdc507_gateway.modem.at import ATResponse
from qdc507_gateway.modem.service import (
    LiveModuleService,
    ModuleServiceError,
    parse_clcc_voice_status,
    parse_cnum_subscriber,
    parse_cops_operator,
    parse_cpas_call_status,
    parse_csq_signal,
)
from qdc507_gateway.storage.database import Database


def _snapshot():
    return USBDeviceSnapshot(
        0x2C7C,
        0x0125,
        serial="qdc507-test",
        interfaces=(InterfaceDescriptor(
            2,
            0xFF,
            0xFF,
            0xFF,
            (EndpointDescriptor(0x81, "in", "bulk", 512), EndpointDescriptor(1, "out", "bulk", 512)),
        ),),
    )


class FakeAT:
    def __init__(self, before_usbcfg=None, after_usbcfg=None, before_cfun=0, after_cfun=1):
        self.before_usbcfg = before_usbcfg
        self.after_usbcfg = after_usbcfg
        self.before_cfun = before_cfun
        self.after_cfun = after_cfun
        self.commands = []
        self.readback_count = 0
        self.urcs = []

    def command(self, command, timeout=3.0):
        self.commands.append(command)
        if command == 'AT+QCFG="USBCFG"':
            value = self.before_usbcfg if self.readback_count == 0 else self.after_usbcfg
            self.readback_count += 1
            return ATResponse((value,), "OK")
        if command.startswith('AT+QCFG="USBCFG",'):
            return ATResponse((), "DETACHED")
        if command == "AT+CFUN?":
            value = self.before_cfun if self.readback_count == 0 else self.after_cfun
            self.readback_count += 1
            return ATResponse((f"+CFUN: {value}",), "OK")
        if command.startswith("AT+CFUN="):
            return ATResponse((), "OK")
        return ATResponse((), "OK")


class FakeSession:
    def __init__(self, at):
        self.at = at
        self.snapshot = _snapshot()
        self.closed = False

    def open(self, preferred=None):
        assert preferred is None or preferred.same_physical_device(self.snapshot)

    def open_at(self, handshake=False):
        assert handshake
        return self.at

    def close(self):
        self.closed = True


@pytest.mark.parametrize(("rssi", "dbm", "bars"), (
    (0, -113, 1),
    (7, -99, 3),
    (12, -89, 4),
    (17, -79, 5),
    (31, -51, 5),
    (99, None, 0),
))
def test_csq_signal_is_converted_to_dbm_and_bars(rssi, dbm, bars):
    signal = parse_csq_signal(("AT+CSQ", f"+CSQ: {rssi},99"))
    assert signal["rssi"] == rssi
    assert signal["dbm"] == dbm
    assert signal["bars"] == bars
    assert signal["available"] is (dbm is not None)
    assert signal["ber"] is None


def test_csq_signal_rejects_missing_or_out_of_range_values():
    with pytest.raises(ModuleServiceError, match="no CSQ"):
        parse_csq_signal(("OK",))
    with pytest.raises(ModuleServiceError, match="invalid CSQ"):
        parse_csq_signal(("+CSQ: 32,0",))


def test_cnum_and_cops_match_qdc507_network_status_rows():
    subscriber = parse_cnum_subscriber((
        "AT+CNUM",
        '+CNUM: ,"14312764514",129',
    ))
    assert subscriber == {
        "available": True,
        "phone_number": "14312764514",
        "numbers": [{"label": None, "number": "14312764514", "type": 129}],
    }
    operator = parse_cops_operator((
        "AT+COPS?",
        '+COPS: 0,0,"Lucky",7',
    ))
    assert operator == {
        "available": True,
        "name": "Lucky",
        "mode": 0,
        "format": 0,
        "access_technology": 7,
        "radio": "LTE",
    }


def test_cnum_allows_a_sim_without_a_provisioned_phone_number():
    assert parse_cnum_subscriber(("AT+CNUM", "OK")) == {
        "available": False,
        "phone_number": None,
        "numbers": [],
    }


def test_network_status_returns_partial_results_for_unsupported_queries():
    async def run():
        service = LiveModuleService(Database(":memory:"), EventBus())

        async def at(command, timeout_ms=3000):
            assert timeout_ms == 3000
            if command == "AT+CNUM":
                return {"ok": True, "lines": ['+CNUM: ,"14312764514",129']}
            if command == "AT+COPS?":
                return {"ok": False, "lines": []}
            return {"ok": True, "lines": ["+CSQ: 17,99"]}

        service.at = at
        status = await service.network_status()
        assert status["phone_number"] == "14312764514"
        assert status["signal"]["dbm"] == -79
        assert status["operator"]["available"] is False
        assert status["errors"] == {"operator": "ModuleServiceError"}

    asyncio.run(run())


def test_clcc_voice_status_ignores_persistent_data_contexts():
    status = parse_clcc_voice_status((
        "AT+CLCC",
        '+CLCC: 2,1,0,1,0,"",128',
        '+CLCC: 1,1,0,1,0,"",128',
        "OK",
    ))
    assert status == {
        "state": "idle",
        "source": "clcc",
        "voice_calls": [],
        "total_calls": 2,
    }


@pytest.mark.parametrize(("code", "expected"), (
    (0, "active"),
    (1, "active"),
    (2, "dialing"),
    (3, "dialing"),
    (4, "ringing"),
    (5, "ringing"),
))
def test_clcc_voice_status_maps_voice_call_states(code, expected):
    status = parse_clcc_voice_status((
        f'+CLCC: 3,0,{code},0,0,"+16479178964",145',
    ))
    assert status["state"] == expected
    assert status["voice_calls"] == [{
        "index": 3,
        "direction": "outbound",
        "status": {
            0: "active", 1: "held", 2: "dialing", 3: "alerting",
            4: "incoming", 5: "waiting",
        }[code],
        "status_code": code,
        "multiparty": False,
        "number": "+16479178964",
    }]


def test_cpas_call_status_maps_supported_module_values():
    assert parse_cpas_call_status(("+CPAS: 0",))["state"] == "idle"
    assert parse_cpas_call_status(("+CPAS: 3",))["state"] == "ringing"
    assert parse_cpas_call_status(("+CPAS: 4",))["state"] == "active"
    with pytest.raises(ModuleServiceError, match="no CPAS"):
        parse_cpas_call_status(("OK",))


def test_voice_call_status_uses_cpas_only_when_clcc_has_no_rows():
    async def run():
        service = LiveModuleService(Database(":memory:"), EventBus())
        commands = []

        async def at(command, timeout_ms=3000):
            commands.append((command, timeout_ms))
            if command == "AT+CLCC":
                return {"ok": True, "lines": ["AT+CLCC"]}
            return {"ok": True, "lines": ["AT+CPAS", "+CPAS: 4"]}

        service.at = at
        status = await service.voice_call_status()
        assert status["state"] == "active"
        assert status["source"] == "cpas"
        assert commands == [("AT+CLCC", 2000), ("AT+CPAS", 2000)]

    asyncio.run(run())


def test_usbcfg_service_path_readbacks_and_reenumerates_once():
    async def run():
        legacy = '+QCFG: "USBCFG",0x2C7C,0x0125,1,1,1,1,1,0,1'
        full = '+QCFG: "USBCFG",0x2C7C,0x0125,1,1,1,1,1,1,1'
        first_at = FakeAT(legacy, legacy)
        second_at = FakeAT(full, full)
        sessions = iter((FakeSession(first_at), FakeSession(second_at)))
        service = LiveModuleService(Database(":memory:"), EventBus())
        service._session = lambda: next(sessions)
        service._wait_for_same_device = lambda previous: _snapshot()

        result = await service.at(
            'AT+QCFG = "USBCFG",0x2C7C,0x0125,1,1,1,1,1,1,1'
        )
        assert result["changed"] is True
        assert result["reenumerated"] is True
        assert first_at.commands[:2] == ['AT+QCFG="USBCFG"', 'AT+QCFG="USBCFG",0x2C7C,0x0125,1,1,1,1,1,1,1']
        assert second_at.commands == ['AT+QCFG="USBCFG"']

    asyncio.run(run())


def test_cfun_reset_path_readbacks_after_reenumeration():
    async def run():
        first_at = FakeAT(before_cfun=0, after_cfun=0)
        second_at = FakeAT(before_cfun=1, after_cfun=1)
        sessions = iter((FakeSession(first_at), FakeSession(second_at)))
        service = LiveModuleService(Database(":memory:"), EventBus())
        service._session = lambda: next(sessions)
        service._wait_for_same_device = lambda previous: _snapshot()

        result = await service.at("AT+CFUN=1,1")
        assert result["reenumerated"] is True
        assert result["before"] == 0
        assert result["after"] == 1
        assert first_at.commands == ["AT+CFUN?", "AT+CFUN=1,1"]
        assert second_at.commands == ["AT+CFUN?"]

    asyncio.run(run())


def test_persistent_usbcfg_treats_libusb_no_device_as_reenumeration():
    async def run():
        legacy = '+QCFG: "USBCFG",0x2C7C,0x0125,1,1,1,1,1,0,1'
        full = '+QCFG: "USBCFG",0x2C7C,0x0125,1,1,1,1,1,1,1'

        class DetachOnWrite(FakeAT):
            def command(self, command, timeout=3.0):
                if command.startswith('AT+QCFG="USBCFG",'):
                    self.commands.append(command)
                    raise RuntimeError("LIBUSB_ERROR_NO_DEVICE")
                return super().command(command, timeout)

        first_at = DetachOnWrite(legacy, legacy)
        second_at = FakeAT(full, full)
        sessions = iter((FakeSession(first_at), FakeSession(second_at)))
        service = LiveModuleService(Database(":memory:"), EventBus())
        service._session = lambda: next(sessions)
        service._wait_for_same_device = lambda previous: _snapshot()

        result = await service.at(
            'AT+QCFG="USBCFG",0x2C7C,0x0125,1,1,1,1,1,1,1'
        )
        assert result["reenumerated"] is True
        assert result["before"]["adb"] is False
        assert result["after"]["adb"] is True

    asyncio.run(run())


def test_monitor_accepts_direct_cmt_pdu_without_sms_storage_index():
    async def run():
        database = Database(":memory:")
        service = LiveModuleService(database, EventBus())
        stop = asyncio.Event()
        pdu = "00040D91683108108300F0000862805221436500046D4B8BD5"

        class DirectCMT:
            def __init__(self):
                self.chunks = iter((
                    b'+CMT: "+8613800138000",23\r\n',
                    (pdu + "\r\n").encode("ascii"),
                ))

            def read(self, _timeout):
                chunk = next(self.chunks)
                if chunk.startswith(pdu.encode("ascii")):
                    stop.set()
                return chunk

            @staticmethod
            def feed(data):
                return [line for line in data.decode("ascii").splitlines() if line]

        await service._read_monitor(DirectCMT(), stop)
        rows = database.list_sms()
        assert len(rows) == 1
        assert rows[0]["body"] == "测试"

    asyncio.run(run())


def test_monitor_accepts_standard_ring_for_crc_zero_modules():
    async def run():
        service = LiveModuleService(Database(":memory:"), EventBus())
        stop = asyncio.Event()
        incoming = []

        class StandardRing:
            def __init__(self):
                self.chunks = iter((b"RING\r\n", b""))

            def read(self, _timeout):
                chunk = next(self.chunks)
                if not chunk:
                    stop.set()
                return chunk

            @staticmethod
            def feed(data):
                return [line for line in data.decode("ascii").splitlines() if line]

        async def on_incoming(number):
            incoming.append(number)

        service._on_incoming_call = on_incoming
        await service._read_monitor(StandardRing(), stop)
        await asyncio.sleep(0.55)
        await asyncio.gather(*service._monitor_callback_tasks)
        assert incoming == [None]

    asyncio.run(run())


def test_monitor_waits_for_clip_after_ring_before_dispatching_incoming_call():
    async def run():
        service = LiveModuleService(Database(":memory:"), EventBus())
        stop = asyncio.Event()
        incoming = []

        class RingThenClip:
            def __init__(self):
                self.chunks = iter((
                    b"RING\r\n",
                    b'+CLIP: "+12045550100",145,,,,0\r\n',
                ))

            def read(self, _timeout):
                chunk = next(self.chunks)
                if chunk.startswith(b"+CLIP"):
                    stop.set()
                return chunk

            @staticmethod
            def feed(data):
                return [line for line in data.decode("ascii").splitlines() if line]

        async def on_incoming(number):
            incoming.append(number)

        service._on_incoming_call = on_incoming
        await service._read_monitor(RingThenClip(), stop)
        await asyncio.gather(*service._monitor_callback_tasks)
        assert incoming == ["+12045550100"]

    asyncio.run(run())


def test_monitor_enables_clip_as_volatile_connection_initialization():
    commands = []

    class AT:
        @staticmethod
        def command(command, timeout):
            commands.append((command, timeout))
            return ATResponse((), "OK")

    assert LiveModuleService._enable_caller_id(AT())
    assert commands == [("AT+CLIP=1", 2.0)]


def test_monitor_persists_cellular_disconnect_reason_before_cleanup():
    async def run():
        persisted = []

        async def persist(event):
            persisted.append(event)

        service = LiveModuleService(Database(":memory:"), EventBus(persist=persist))
        stop = asyncio.Event()
        disconnected = []

        class NoAnswer:
            @staticmethod
            def read(_timeout):
                stop.set()
                return b"NO ANSWER\r\n"

            @staticmethod
            def feed(data):
                return [line for line in data.decode("ascii").splitlines() if line]

        async def on_disconnected():
            disconnected.append(True)

        service._on_call_disconnected = on_disconnected
        await service._read_monitor(NoAnswer(), stop)
        await asyncio.gather(*service._monitor_callback_tasks)

        assert disconnected == [True]
        assert persisted[-1].type == "call.cellular.disconnected"
        assert persisted[-1].payload == {"reason": "NO ANSWER"}

    asyncio.run(run())


def test_monitor_connection_state_is_reflected_in_rest_state():
    state = {
        "status": {"service": "test", "module_state": "connected"},
        "module": {"connected": True, "identity": "2C7C:0125"},
    }
    service = LiveModuleService(Database(":memory:"), EventBus(), state=state)

    service._set_connection_state(False, "LiveUSBError")
    assert state["status"]["module_state"] == "disconnected"
    assert state["module"]["connected"] is False
    assert state["module"]["identity"] is None
    assert state["module"]["error"] == "LiveUSBError"

    service._set_connection_state(True)
    assert state["status"]["module_state"] == "connected"
    assert state["module"]["connected"] is True
    assert state["module"]["identity"] == "2C7C:0125"
    assert "error" not in state["module"]


def test_at_waits_for_monitor_handle_instead_of_opening_competing_session():
    async def run():
        service = LiveModuleService(Database(":memory:"), EventBus())
        release_monitor = asyncio.Event()
        short_session_used = False

        class MonitorAT:
            urcs = []

            @staticmethod
            def command(command, _timeout):
                assert command == "AT"
                return ATResponse((), "OK")

        def fail_short_session(*_args):
            nonlocal short_session_used
            short_session_used = True
            raise AssertionError("a second USB owner must not be opened")

        service._monitor_task = asyncio.create_task(release_monitor.wait())
        service._command_short = fail_short_session
        pending = asyncio.create_task(service.at("AT"))
        await asyncio.sleep(0)
        assert not pending.done()

        service._monitor_at = MonitorAT()
        service._monitor_ready.set()
        result = await pending
        assert result["ok"] is True
        assert short_session_used is False

        release_monitor.set()
        await service._monitor_task

    asyncio.run(run())


def test_monitor_transport_disconnect_invokes_call_cleanup_callback():
    async def run():
        service = LiveModuleService(Database(":memory:"), EventBus())
        stop = asyncio.Event()
        callback_count = []
        callback_tasks = []

        class Session:
            def close(self):
                return None

        async def disconnected():
            callback_count.append(True)
            callback_tasks.append(asyncio.current_task())
            stop.set()

        service._monitor_stop = stop
        service._monitor_connected = True
        service._incoming_ring_notified = True
        service._on_call_disconnected = disconnected
        service._open_monitor_session = lambda: (Session(), object())

        async def disconnected_read(_at, _stop):
            raise RuntimeError("LIBUSB_ERROR_NO_DEVICE")

        service._read_monitor = disconnected_read
        await service._monitor_loop()
        assert len(callback_count) == 1
        assert callback_tasks[0] is not service._monitor_task
        assert service._incoming_ring_notified is False

    asyncio.run(run())


def test_monitor_callback_can_run_exclusive_cleanup_without_self_await():
    async def run():
        service = LiveModuleService(Database(":memory:"), EventBus())
        monitor_stop = asyncio.Event()
        operation_done = asyncio.Event()
        restarted = []

        async def monitor():
            await monitor_stop.wait()

        async def fake_start_monitor(**_kwargs):
            restarted.append(True)

        async def disconnected():
            await service.run_exclusive(operation_done.set)

        service._monitor_stop = monitor_stop
        service._monitor_task = asyncio.create_task(monitor())
        service.start_monitor = fake_start_monitor
        service._schedule_monitor_callback(
            disconnected,
            "call.disconnect_error",
        )

        await asyncio.wait_for(operation_done.wait(), timeout=1)
        await asyncio.gather(*service._monitor_callback_tasks)
        assert restarted == [True]

    asyncio.run(run())
