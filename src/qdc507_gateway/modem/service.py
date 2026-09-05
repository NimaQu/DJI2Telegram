from __future__ import annotations

import asyncio
import csv
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from qdc507_gateway.events import EventBus
from qdc507_gateway.models import GatewayEvent
from qdc507_gateway.modem.qadbkey import authorize_qadbkey
from qdc507_gateway.modem.sms import SMSIngress, encode_sms
from qdc507_gateway.modem.usbcfg import (
    is_reenumeration_signal,
    parse_usb_configuration,
    parse_usbcfg_command,
)
from qdc507_gateway.security import forbidden_generic_at_command
from qdc507_gateway.storage.database import Database
from qdc507_gateway.usb.descriptors import DeviceLocator
from qdc507_gateway.usb.live import LibUSBDeviceSession, LiveUSBError
from qdc507_gateway.usb.owner import DeviceOwnerLock
from qdc507_gateway.usb.reenumeration import ReenumerationCoordinator


class ModuleServiceError(RuntimeError):
    pass


MONITOR_READY_TIMEOUT_SECONDS = 5.0
CSQ_PATTERN = re.compile(r"^\+CSQ:\s*(\d+)\s*,\s*(\d+)\s*$", re.IGNORECASE)
CLCC_PATTERN = re.compile(
    r'^\+CLCC:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'
    r'(\d+)\s*,\s*(\d+)(?:\s*,\s*"([^"]*)"\s*,\s*(\d+))?',
    re.IGNORECASE,
)
CPAS_PATTERN = re.compile(r"^\+CPAS:\s*(\d+)\s*$", re.IGNORECASE)


def _csv_payload(line: str, prefix: str) -> list[str] | None:
    if not line.strip().upper().startswith(prefix):
        return None
    try:
        return next(csv.reader([line.split(":", 1)[1]], skipinitialspace=True))
    except (csv.Error, IndexError, StopIteration):
        return None


def parse_cnum_subscriber(lines: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Parse one or more +CNUM rows without inferring a missing SIM number."""
    numbers = []
    for line in lines:
        fields = _csv_payload(line, "+CNUM:")
        if fields is None or len(fields) < 2:
            continue
        number = fields[1].strip()
        if not number:
            continue
        try:
            number_type = int(fields[2]) if len(fields) > 2 and fields[2] else None
        except ValueError:
            number_type = None
        numbers.append({
            "label": fields[0].strip() or None,
            "number": number,
            "type": number_type,
        })
    return {
        "available": bool(numbers),
        "phone_number": numbers[0]["number"] if numbers else None,
        "numbers": numbers,
    }


def parse_cops_operator(lines: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Parse +COPS using the modem-selected display format."""
    radio_names = {
        0: "GSM",
        2: "UTRAN",
        3: "GSM/EGPRS",
        7: "LTE",
        11: "NR",
        13: "E-UTRAN/NR",
    }
    for line in lines:
        fields = _csv_payload(line, "+COPS:")
        if fields is None or len(fields) < 2:
            continue
        try:
            mode = int(fields[0])
            display_format = int(fields[1])
            access_technology = (
                int(fields[3]) if len(fields) > 3 and fields[3] else None
            )
        except ValueError as exc:
            raise ModuleServiceError("modem returned an invalid COPS value") from exc
        name = fields[2].strip() if len(fields) > 2 else ""
        return {
            "available": bool(name),
            "name": name or None,
            "mode": mode,
            "format": display_format,
            "access_technology": access_technology,
            "radio": radio_names.get(access_technology),
        }
    return {
        "available": False,
        "name": None,
        "mode": None,
        "format": None,
        "access_technology": None,
        "radio": None,
    }


def parse_clcc_voice_status(lines: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Summarize only mode=0 voice calls from a +CLCC response.

    The QDC507 can expose long-lived mode=1 data entries even while the phone
    service is idle.  Those entries must never satisfy the voice-call state
    machine.
    """
    calls: list[dict[str, Any]] = []
    total_calls = 0
    status_names = {
        0: "active",
        1: "held",
        2: "dialing",
        3: "alerting",
        4: "incoming",
        5: "waiting",
        6: "disconnected",
    }
    for line in lines:
        match = CLCC_PATTERN.match(line.strip())
        if match is None:
            continue
        total_calls += 1
        mode = int(match.group(4))
        if mode != 0:
            continue
        status = int(match.group(3))
        calls.append({
            "index": int(match.group(1)),
            "direction": "inbound" if int(match.group(2)) == 1 else "outbound",
            "status": status_names.get(status, "unknown"),
            "status_code": status,
            "multiparty": bool(int(match.group(5))),
            "number": match.group(6) or None,
        })

    statuses = {call["status_code"] for call in calls}
    if statuses & {0, 1}:
        state = "active"
    elif statuses & {2, 3}:
        state = "dialing"
    elif statuses & {4, 5}:
        state = "ringing"
    elif statuses == {6}:
        state = "disconnected"
    elif calls:
        state = "unknown"
    else:
        state = "idle"
    return {
        "state": state,
        "source": "clcc",
        "voice_calls": calls,
        "total_calls": total_calls,
    }


def parse_cpas_call_status(lines: list[str] | tuple[str, ...]) -> dict[str, Any]:
    for line in lines:
        match = CPAS_PATTERN.match(line.strip())
        if match is None:
            continue
        value = int(match.group(1))
        return {
            "state": {0: "idle", 3: "ringing", 4: "active"}.get(value, "unknown"),
            "source": "cpas",
            "cpas": value,
            "voice_calls": [],
            "total_calls": 0,
        }
    raise ModuleServiceError("modem returned no CPAS call state")


def parse_csq_signal(lines: list[str] | tuple[str, ...]) -> dict[str, Any]:
    match = None
    for line in lines:
        match = CSQ_PATTERN.match(line.strip())
        if match is not None:
            break
    if match is None:
        raise ModuleServiceError("modem returned no CSQ signal value")
    rssi = int(match.group(1))
    ber_value = int(match.group(2))
    if rssi == 99:
        dbm = None
        bars = 0
    elif 0 <= rssi <= 31:
        dbm = -113 + (2 * rssi)
        if dbm <= -110:
            bars = 1
        elif dbm <= -100:
            bars = 2
        elif dbm <= -90:
            bars = 3
        elif dbm <= -80:
            bars = 4
        else:
            bars = 5
    else:
        raise ModuleServiceError("modem returned an invalid CSQ RSSI value")
    return {
        "available": dbm is not None,
        "rssi": rssi,
        "dbm": dbm,
        "bars": bars,
        "ber": None if ber_value == 99 else ber_value,
        "measured_at": _timestamp(),
    }


class LiveModuleService:
    """Explicit AT/SMS/ADB operations over one short-lived live USB lease."""

    def __init__(
        self,
        database: Database,
        events: EventBus,
        lock_path: str | Path | None = None,
        locator: Optional[DeviceLocator] = None,
        state: Optional[dict[str, Any]] = None,
    ):
        self.database = database
        self.events = events
        self.lock_path = lock_path
        self.locator = locator
        self.state = state
        self.sms_ingress = SMSIngress(database)
        self.sms_forwarder: Optional[Callable[[dict[str, object]], Awaitable[Any]]] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._monitor_stop: Optional[asyncio.Event] = None
        self._monitor_session: Optional[LibUSBDeviceSession] = None
        self._monitor_at = None
        self._monitor_lock = asyncio.Lock()
        self._monitor_ready = asyncio.Event()
        self._persistent_lock = asyncio.Lock()
        self._monitor_callback_lock = asyncio.Lock()
        self._monitor_callback_tasks: set[asyncio.Task] = set()
        self._incoming_ring_delay_task: Optional[asyncio.Task] = None
        self._incoming_ring_notified = False
        self._monitor_connected = False
        self._on_incoming_call: Optional[Callable[[Optional[str]], Awaitable[Any]]] = None
        self._on_call_disconnected: Optional[Callable[[], Awaitable[Any]]] = None
        self._on_cellular_connected: Optional[Callable[[], Awaitable[Any]]] = None
        try:
            from qdc507_gateway.usb.udev import PyUdevUSBMonitor, UdevUnavailable
            self._udev_monitor = PyUdevUSBMonitor()
        except (ImportError, UdevUnavailable):
            self._udev_monitor = None

    def _session(self) -> LibUSBDeviceSession:
        return LibUSBDeviceSession(owner=DeviceOwnerLock(self.lock_path))

    @property
    def monitoring(self) -> bool:
        return self._monitor_task is not None and not self._monitor_task.done()

    def open_adb_client(self):
        """Open a connected ADB client and its owning session for one transition."""
        session = self._session()
        try:
            session.open()
            client = session.open_adb()
            client.connect()
            return client, session.close
        except Exception:
            session.close()
            raise

    async def run_exclusive(self, operation: Callable[[], Any]) -> Any:
        """Run a synchronous USB owner operation without racing the AT monitor."""
        async with self._persistent_lock:
            was_monitoring = self._monitor_task is not None and not self._monitor_task.done()
            if was_monitoring and self._monitor_task is asyncio.current_task():
                raise ModuleServiceError(
                    "exclusive USB operation cannot run inside the AT monitor task"
                )
            callbacks = (
                self._on_incoming_call,
                self._on_call_disconnected,
                self._on_cellular_connected,
            )
            if was_monitoring:
                await self.stop_monitor()
            try:
                return await asyncio.to_thread(operation)
            finally:
                if was_monitoring:
                    await self.start_monitor(
                        on_incoming_call=callbacks[0],
                        on_call_disconnected=callbacks[1],
                        on_cellular_connected=callbacks[2],
                    )

    async def at(self, command: str, timeout_ms: int = 3000) -> dict[str, Any]:
        if not isinstance(command, str):
            raise ModuleServiceError("AT command must be a string")
        normalized = command.strip().upper()
        if forbidden_generic_at_command(command):
            raise ModuleServiceError(
                "QADBKEY requires the dedicated authorization operation"
            )
        if re.match(r'AT\+QCFG\s*=\s*"USBCFG"\s*,', normalized) or re.match(
            r"AT\+CFUN\s*=", normalized
        ):
            return await self._persistent_at(command, timeout_ms)
        used_monitor = False
        try:
            if self.monitoring:
                try:
                    await asyncio.wait_for(
                        self._monitor_ready.wait(),
                        timeout=MONITOR_READY_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as exc:
                    raise ModuleServiceError(
                        "AT monitor did not become ready after the USB transition"
                    ) from exc
            async with self._monitor_lock:
                if self._monitor_at is not None:
                    used_monitor = True
                    result = await asyncio.to_thread(
                        self._command_on_at, self._monitor_at, command, timeout_ms,
                    )
                elif self.monitoring:
                    raise ModuleServiceError("AT monitor connection is unavailable")
                else:
                    return await asyncio.to_thread(self._command_short, command, timeout_ms)
            if result["terminal"] is None:
                await self._restart_monitor_session()
                raise ModuleServiceError(
                    "QDC507 AT command timed out; monitor connection was reset"
                )
            return result
        except ModuleServiceError:
            raise
        except Exception as exc:
            if used_monitor:
                await self._restart_monitor_session()
            raise ModuleServiceError(
                f"QDC507 module operation unavailable: {type(exc).__name__}"
            ) from exc

    async def _restart_monitor_session(self) -> None:
        if not self.monitoring:
            return
        callbacks = (
            self._on_incoming_call,
            self._on_call_disconnected,
            self._on_cellular_connected,
        )
        await self.stop_monitor()
        await self.start_monitor(
            on_incoming_call=callbacks[0],
            on_call_disconnected=callbacks[1],
            on_cellular_connected=callbacks[2],
        )

    async def _persistent_at(self, command: str, timeout_ms: int) -> dict[str, Any]:
        """Run one confirmed persistent operation across a possible re-enumeration."""
        operation = "usbcfg" if re.match(
            r'AT\+QCFG\s*=\s*"USBCFG"\s*,', command.strip().upper()
        ) else "cfun"
        async with self._persistent_lock:
            was_monitoring = self._monitor_task is not None and not self._monitor_task.done()
            callbacks = (
                self._on_incoming_call,
                self._on_call_disconnected,
                self._on_cellular_connected,
            )
            if was_monitoring:
                await self.stop_monitor()
            await self.events.publish(GatewayEvent("module.reenumerating", {"operation": operation}))
            try:
                result = await asyncio.to_thread(
                    self._persistent_work, operation, command, timeout_ms,
                )
            except ModuleServiceError:
                await self.events.publish(GatewayEvent("module.reenumeration_error", {
                    "operation": operation,
                    "error": "module operation unavailable",
                }))
                raise
            except Exception as exc:
                await self.events.publish(GatewayEvent("module.reenumeration_error", {
                    "operation": operation,
                    "error": type(exc).__name__,
                }))
                raise ModuleServiceError(
                    f"QDC507 module operation unavailable: {type(exc).__name__}"
                ) from exc
            finally:
                if was_monitoring:
                    await self.start_monitor(
                        on_incoming_call=callbacks[0],
                        on_call_disconnected=callbacks[1],
                        on_cellular_connected=callbacks[2],
                    )
            if result.get("reenumerated"):
                await self.events.publish(GatewayEvent("module.reenumerated", {
                    "operation": operation,
                    "identity": "2C7C:0125",
                }))
            return result

    def _persistent_work(self, operation: str, command: str, timeout_ms: int) -> dict[str, Any]:
        if operation == "usbcfg":
            return self._persistent_usbcfg(command, timeout_ms)
        return self._persistent_cfun(command, timeout_ms)

    def _persistent_usbcfg(self, command: str, timeout_ms: int) -> dict[str, Any]:
        target = parse_usbcfg_command(command)
        if target is None:
            raise ModuleServiceError("invalid USBCFG command")
        if not target.is_full_target:
            raise ModuleServiceError("target USBCFG is not the complete QDC507 target")

        session = self._session()
        try:
            session.open()
            at = session.open_at(handshake=True)
            previous = session.snapshot
            before = self._read_usbcfg(at, timeout_ms)
            if before == target:
                return {
                    "operation": "usbcfg",
                    "changed": False,
                    "reenumerated": False,
                    "before": _config_dict(before),
                    "after": _config_dict(before),
                }
            try:
                response = at.command(target.command, timeout_ms / 1000)
                terminal = (response.terminal or "").upper().replace(" ", "_")
            except Exception as exc:
                if not is_reenumeration_signal(exc):
                    raise
                terminal = "DETACHED"
            if terminal not in {"OK", "NO_DEVICE", "NOT_CONNECTED", "DETACHED"}:
                raise ModuleServiceError("USBCFG write was not accepted")
            if previous is None:
                raise ModuleServiceError("USB snapshot was lost before re-enumeration")
            session.close()
            rebound = self._wait_for_same_device(previous)
            refreshed = self._session()
            try:
                refreshed.open(preferred=rebound)
                after = self._read_usbcfg(refreshed.open_at(handshake=True), timeout_ms)
            finally:
                refreshed.close()
            if after not in (before, target):
                raise ModuleServiceError("post-reconnect USBCFG readback is unexpected")
            return {
                "operation": "usbcfg",
                "changed": True,
                "reenumerated": True,
                "before": _config_dict(before),
                "after": _config_dict(after),
            }
        finally:
            session.close()

    def _persistent_cfun(self, command: str, timeout_ms: int) -> dict[str, Any]:
        match = re.fullmatch(
            r"AT\+CFUN\s*=\s*(\d+)(?:\s*,\s*(\d+))?",
            command.strip(),
            re.IGNORECASE,
        )
        if match is None:
            raise ModuleServiceError("invalid CFUN command")
        target = int(match.group(1))
        reset = match.group(2) == "1"
        session = self._session()
        try:
            session.open()
            at = session.open_at(handshake=True)
            previous = session.snapshot
            before = self._read_cfun(at, timeout_ms)
            try:
                response = at.command(command, timeout_ms / 1000)
                terminal = (response.terminal or "").upper().replace(" ", "_")
            except Exception as exc:
                if not is_reenumeration_signal(exc):
                    raise
                terminal = "DETACHED"
            if terminal not in {"OK", "NO_DEVICE", "NOT_CONNECTED", "DETACHED"}:
                raise ModuleServiceError("CFUN write was not accepted")
            if not reset and terminal == "OK":
                return {
                    "operation": "cfun",
                    "changed": before != target,
                    "reenumerated": False,
                    "before": before,
                    "after": target,
                }
            if previous is None:
                raise ModuleServiceError("USB snapshot was lost before re-enumeration")
            session.close()
            rebound = self._wait_for_same_device(previous)
            refreshed = self._session()
            try:
                refreshed.open(preferred=rebound)
                after = self._read_cfun(refreshed.open_at(handshake=True), timeout_ms)
            finally:
                refreshed.close()
            if after not in (before, target):
                raise ModuleServiceError("post-reconnect CFUN readback is unexpected")
            return {
                "operation": "cfun",
                "changed": before != target,
                "reenumerated": True,
                "before": before,
                "after": after,
            }
        finally:
            session.close()

    @staticmethod
    def _read_usbcfg(at, timeout_ms: int):
        response = at.command('AT+QCFG="USBCFG"', timeout_ms / 1000)
        if not response.ok:
            raise ModuleServiceError("USBCFG readback was not accepted")
        value = parse_usb_configuration(
            "\n".join(response.lines + ((response.terminal or ""),))
        )
        if value is None:
            raise ModuleServiceError("modem returned an invalid USBCFG readback")
        return value

    @staticmethod
    def _read_cfun(at, timeout_ms: int) -> int:
        response = at.command("AT+CFUN?", timeout_ms / 1000)
        if not response.ok:
            raise ModuleServiceError("CFUN readback was not accepted")
        for line in response.lines:
            match = re.fullmatch(r"\+CFUN:\s*(\d+)", line.strip(), re.IGNORECASE)
            if match:
                return int(match.group(1))
        raise ModuleServiceError("modem returned an invalid CFUN readback")

    def _wait_for_same_device(self, previous):
        locator = self.locator
        if locator is None:
            from qdc507_gateway.usb.descriptors import LibUSBDeviceLocator
            locator = LibUSBDeviceLocator()
            self.locator = locator
        event_waiter = None if self._udev_monitor is None else self._udev_monitor.wait
        return ReenumerationCoordinator(
            locator,
            event_waiter=event_waiter,
        ).wait_for_device(previous)

    @staticmethod
    def _command_on_at(at, command: str, timeout_ms: int) -> dict[str, Any]:
        response = at.command(command, timeout_ms / 1000)
        return {
            "lines": list(response.lines),
            "urcs": list(at.urcs),
            "terminal": response.terminal,
            "ok": response.ok,
        }

    def _command_short(self, command: str, timeout_ms: int) -> dict[str, Any]:
        with self._session() as session:
            at = session.open_at(handshake=True)
            return self._command_on_at(at, command, timeout_ms)

    async def start_monitor(
        self,
        *,
        on_incoming_call: Optional[Callable[[Optional[str]], Awaitable[Any]]] = None,
        on_call_disconnected: Optional[Callable[[], Awaitable[Any]]] = None,
        on_cellular_connected: Optional[Callable[[], Awaitable[Any]]] = None,
    ) -> None:
        self._on_incoming_call = on_incoming_call
        self._on_call_disconnected = on_call_disconnected
        self._on_cellular_connected = on_cellular_connected
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_ready.clear()
        self._monitor_stop = asyncio.Event()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop_monitor(self) -> None:
        self._monitor_ready.clear()
        self._reset_incoming_ring()
        if self._monitor_stop is not None:
            self._monitor_stop.set()
        task = self._monitor_task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
        self._monitor_connected = False
        self._set_connection_state(False)
        self._monitor_task = None
        self._monitor_stop = None

    def close(self) -> None:
        if self._udev_monitor is not None:
            self._udev_monitor.close()
            self._udev_monitor = None

    def _set_connection_state(self, connected: bool, error: Optional[str] = None) -> None:
        """Keep REST state aligned with the descriptor/AT monitor lifecycle."""
        if self.state is None:
            return
        module = dict(self.state.get("module", {}))
        module["connected"] = connected
        module["identity"] = "2C7C:0125" if connected else None
        if not connected:
            module["signal"] = None
        if error:
            module["error"] = error
        else:
            module.pop("error", None)
        self.state["module"] = module
        status = dict(self.state.get("status", {}))
        status["module_state"] = "connected" if connected else "disconnected"
        if error:
            status["last_error"] = error
        self.state["status"] = status

    async def _monitor_loop(self) -> None:
        stop = self._monitor_stop
        if stop is None:
            return
        while not stop.is_set():
            disconnect_callback = None
            try:
                session, at = await asyncio.to_thread(self._open_monitor_session)
                await asyncio.to_thread(self._configure_direct_sms, at)
                caller_id_enabled = await asyncio.to_thread(self._enable_caller_id, at)
                async with self._monitor_lock:
                    self._monitor_session = session
                    self._monitor_at = at
                self._monitor_ready.set()
                if not caller_id_enabled:
                    await self.events.publish(GatewayEvent(
                        "module.caller_id_unavailable",
                        {"command": "AT+CLIP=1"},
                    ))
                if not self._monitor_connected:
                    self._monitor_connected = True
                    self._set_connection_state(True)
                    await self.events.publish(GatewayEvent("module.connected", {
                        "source": "at-monitor",
                        "identity": "2C7C:0125",
                    }))
                await self._read_monitor(at, stop)
            except Exception as exc:
                if not stop.is_set():
                    was_connected = self._monitor_connected
                    self._monitor_connected = False
                    self._reset_incoming_ring()
                    self._set_connection_state(False, type(exc).__name__)
                    if was_connected:
                        await self.events.publish(GatewayEvent("module.disconnected", {
                            "source": "at-monitor",
                            "error": type(exc).__name__,
                        }))
                        disconnect_callback = self._on_call_disconnected
                    if not isinstance(exc, LiveUSBError):
                        await self.events.publish(GatewayEvent("module.monitor_error", {
                            "error": type(exc).__name__,
                        }))
            finally:
                self._monitor_ready.clear()
                self._cancel_incoming_ring_delay()
                await self._close_monitor_session()
            if disconnect_callback is not None and not stop.is_set():
                self._schedule_monitor_callback(
                    disconnect_callback,
                    "call.disconnect_error",
                )
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

    def _open_monitor_session(self):
        session = self._session()
        try:
            session.open()
            at = session.open_at(handshake=True)
            return session, at
        except Exception:
            session.close()
            raise

    @staticmethod
    def _configure_direct_sms(at: Any) -> None:
        """Deliver SMS PDUs to this process without consuming modem storage."""
        command = getattr(at, "command", None)
        if not callable(command):
            raise ModuleServiceError("AT transport cannot configure direct SMS delivery")
        for value in ("AT+CMGF=0", "AT+CNMI=2,2,0,0,0"):
            try:
                response = command(value, 2.0)
            except Exception as exc:
                raise ModuleServiceError(
                    "modem direct SMS delivery configuration failed"
                ) from exc
            if not response.ok:
                raise ModuleServiceError("modem rejected direct SMS delivery configuration")

    @staticmethod
    def _enable_caller_id(at: Any) -> bool:
        command = getattr(at, "command", None)
        if not callable(command):
            return False
        try:
            return bool(command("AT+CLIP=1", 2.0).ok)
        except Exception:
            return False

    async def _close_monitor_session(self) -> None:
        async with self._monitor_lock:
            session = self._monitor_session
            self._monitor_session = None
            self._monitor_at = None
        if session is not None:
            await asyncio.to_thread(session.close)

    async def _read_monitor(self, at, stop: asyncio.Event) -> None:
        pending_number: Optional[str] = None
        pending_direct_sms = False
        while not stop.is_set():
            try:
                async with self._monitor_lock:
                    drain_urcs = getattr(at, "drain_urcs", None)
                    queued_lines = drain_urcs() if callable(drain_urcs) else []
                    if queued_lines:
                        lines = queued_lines
                    else:
                        chunk = await asyncio.to_thread(at.read, 0.5)
                        lines = at.feed(chunk)
            except Exception as exc:
                if "timeout" in type(exc).__name__.lower():
                    continue
                raise
            for line in lines:
                upper = line.upper()
                if pending_direct_sms and re.fullmatch(r"[0-9A-F]{20,}", upper):
                    pending_direct_sms = False
                    try:
                        await self.ingest_sms_pdu(upper)
                    except (ModuleServiceError, ValueError) as sms_error:
                        await self.events.publish(GatewayEvent("sms.receive_error", {
                            "error": type(sms_error).__name__,
                        }))
                    continue
                if upper.startswith("+CMT:"):
                    # Some modem profiles deliver the PDU directly after a
                    # +CMT header instead of emitting +CMTI plus a storage
                    # index.  Keep the header itself out of the SMS body.
                    pending_direct_sms = True
                elif upper.startswith("+CMTI:"):
                    match = re.search(r",\s*(\d+)\s*$", line)
                    if match:
                        try:
                            await self.read_sms(int(match.group(1)))
                        except (ModuleServiceError, ValueError) as sms_error:
                            await self.events.publish(GatewayEvent("sms.receive_error", {
                                "error": type(sms_error).__name__,
                            }))
                    pending_direct_sms = False
                elif upper.startswith("+CLIP:"):
                    pending_direct_sms = False
                    match = re.search(r'\+CLIP:\s*"([+0-9 -]+)"', line, re.IGNORECASE)
                    if match:
                        pending_number = match.group(1).replace(" ", "").replace("-", "")
                        await self.events.publish(GatewayEvent(
                            "call.cellular.caller_id",
                            {"number": pending_number},
                        ))
                        if self._incoming_ring_delay_task is not None:
                            self._deliver_incoming_call(pending_number)
                            pending_number = None
                elif upper == "RING" or upper.startswith("+CRING"):
                    pending_direct_sms = False
                    await self.events.publish(GatewayEvent(
                        "call.cellular.ringing",
                        {"number": pending_number},
                    ))
                    if pending_number is None:
                        self._defer_incoming_call()
                    else:
                        self._deliver_incoming_call(pending_number)
                    pending_number = None
                elif upper.startswith("+CIEV:") and re.search(r",\s*1\s*$", line):
                    pending_direct_sms = False
                    if self._on_cellular_connected is not None:
                        self._schedule_monitor_callback(
                            self._on_cellular_connected,
                            "call.connect_error",
                        )
                elif upper in {"NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE", "NO DIAL TONE"}:
                    pending_direct_sms = False
                    self._reset_incoming_ring()
                    await self.events.publish(GatewayEvent(
                        "call.cellular.disconnected",
                        {"reason": upper},
                    ))
                    if self._on_call_disconnected is not None:
                        self._schedule_monitor_callback(
                            self._on_call_disconnected,
                            "call.disconnect_error",
                        )

    def _defer_incoming_call(self) -> None:
        if self._on_incoming_call is None or self._incoming_ring_notified:
            return
        task = self._incoming_ring_delay_task
        if task is not None and not task.done():
            return

        async def wait_for_caller_id() -> None:
            await asyncio.sleep(0.5)
            self._incoming_ring_delay_task = None
            self._deliver_incoming_call(None)

        task = asyncio.create_task(wait_for_caller_id())
        self._incoming_ring_delay_task = task
        task.add_done_callback(self._incoming_ring_delay_done)

    def _deliver_incoming_call(self, number: Optional[str]) -> None:
        if self._on_incoming_call is None or self._incoming_ring_notified:
            return
        self._cancel_incoming_ring_delay()
        self._incoming_ring_notified = True
        self._schedule_monitor_callback(
            self._on_incoming_call,
            "call.receive_error",
            number,
        )

    def _incoming_ring_delay_done(self, task: asyncio.Task) -> None:
        if self._incoming_ring_delay_task is task:
            self._incoming_ring_delay_task = None

    def _cancel_incoming_ring_delay(self) -> None:
        task = self._incoming_ring_delay_task
        self._incoming_ring_delay_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _reset_incoming_ring(self) -> None:
        self._cancel_incoming_ring_delay()
        self._incoming_ring_notified = False

    def _schedule_monitor_callback(
        self,
        callback: Callable[..., Awaitable[Any]],
        error_event: str,
        *args: Any,
    ) -> None:
        """Run call orchestration outside the USB monitor task, in URC order."""
        task = asyncio.create_task(
            self._run_monitor_callback(callback, error_event, *args)
        )
        self._monitor_callback_tasks.add(task)
        task.add_done_callback(self._monitor_callback_tasks.discard)

    async def _run_monitor_callback(
        self,
        callback: Callable[..., Awaitable[Any]],
        error_event: str,
        *args: Any,
    ) -> None:
        async with self._monitor_callback_lock:
            try:
                await callback(*args)
            except Exception as call_error:
                await self.events.publish(GatewayEvent(error_event, {
                    "error": type(call_error).__name__,
                }))

    async def send_sms(self, destination: str, body: str) -> dict[str, Any]:
        try:
            segments = encode_sms(destination, body)
        except ValueError as exc:
            raise ModuleServiceError(f"invalid SMS payload: {exc}") from exc

        def work():
            with self._session() as session:
                at = session.open_at(handshake=True)
                responses = []
                for segment in segments:
                    response = at.send_pdu(segment.pdu, segment.tpdu_length)
                    responses.append(response)
                    if not response.ok:
                        raise ModuleServiceError("modem rejected SMS PDU")
                self.database.save_sms({
                    "id": str(uuid.uuid4()),
                    "sender": "self",
                    "body": body,
                    "timestamp": _timestamp(),
                    "is_read": True,
                    "raw_pdus": json.dumps([segment.pdu for segment in segments]),
                })
                return {"segments": len(segments), "accepted": True}

        if self._monitor_task is not None and not self._monitor_task.done():
            result = await self.run_exclusive(work)
        else:
            result = await self._run(work)
        await self.events.publish(GatewayEvent("sms.sent", result))
        return result

    async def ingest_sms_pdu(self, pdu: str) -> Optional[dict[str, object]]:
        message = await asyncio.to_thread(self.sms_ingress.ingest, pdu)
        if message is not None:
            await self.events.publish(GatewayEvent("sms.received", {
                "id": message["id"],
                "sender": message["sender"],
                "timestamp": message["timestamp"],
            }))
            if self.sms_forwarder is not None:
                try:
                    await self.sms_forwarder(message)
                except Exception as exc:
                    await self.events.publish(GatewayEvent("sms.forward_error", {
                        "id": message["id"],
                        "error": type(exc).__name__,
                    }))
        return message

    async def read_sms(self, index: int) -> Optional[dict[str, object]]:
        if index < 0 or index > 65535:
            raise ModuleServiceError("invalid SMS storage index")
        response = await self.at(f"AT+CMGR={index}")
        pdu = next(
            (line.strip().upper() for line in response["lines"]
             if re.fullmatch(r"[0-9A-Fa-f]{20,}", line.strip())),
            None,
        )
        if pdu is None:
            raise ModuleServiceError("modem returned no SMS PDU")
        return await self.ingest_sms_pdu(pdu)

    async def signal(self) -> dict[str, Any]:
        response = await self.at("AT+CSQ", timeout_ms=2000)
        if not response["ok"]:
            raise ModuleServiceError("modem rejected CSQ signal query")
        return parse_csq_signal(response["lines"])

    async def voice_call_status(self) -> dict[str, Any]:
        """Read the cellular voice-call state without changing modem state."""
        response = await self.at("AT+CLCC", timeout_ms=2000)
        if not response["ok"]:
            raise ModuleServiceError("modem rejected CLCC call-state query")
        status = parse_clcc_voice_status(response["lines"])
        if status["voice_calls"] or status["total_calls"]:
            return status

        # Some firmware omits CLCC rows while a call is being connected. CPAS
        # is a safe fallback only when CLCC returned no rows at all. In
        # particular, never reinterpret mode=1 CLCC data contexts as voice.
        response = await self.at("AT+CPAS", timeout_ms=2000)
        if not response["ok"]:
            raise ModuleServiceError("modem rejected CPAS call-state query")
        return parse_cpas_call_status(response["lines"])

    async def network_status(self) -> dict[str, Any]:
        """Read the SIM-provisioned number, selected operator and RSSI.

        CNUM is allowed to be empty: many operators do not provision the own
        number on the SIM.  Each field is reported independently so one
        unsupported query does not hide the remaining useful status.
        """
        result: dict[str, Any] = {
            "phone_number": None,
            "subscriber": {
                "available": False,
                "phone_number": None,
                "numbers": [],
            },
            "operator": {
                "available": False,
                "name": None,
                "mode": None,
                "format": None,
                "access_technology": None,
                "radio": None,
            },
            "signal": None,
            "measured_at": _timestamp(),
            "errors": {},
        }
        commands = (
            ("subscriber", "AT+CNUM", parse_cnum_subscriber),
            ("operator", "AT+COPS?", parse_cops_operator),
            ("signal", "AT+CSQ", parse_csq_signal),
        )
        for name, command, parser in commands:
            try:
                response = await self.at(command, timeout_ms=3000)
                if not response["ok"]:
                    raise ModuleServiceError(f"modem rejected {command}")
                parsed = parser(response["lines"])
                result[name] = parsed
                if name == "subscriber":
                    result["phone_number"] = parsed["phone_number"]
            except Exception as exc:
                result["errors"][name] = type(exc).__name__
        return result

    async def dial(self, number: str) -> dict[str, Any]:
        self._reset_incoming_ring()
        normalized = _normalize_phone_number(number)
        result = await self.at(f"ATD{normalized};")
        if not result["ok"]:
            raise ModuleServiceError("modem rejected outgoing call")
        await self.events.publish(GatewayEvent("call.cellular.dialing", {"number": normalized}))
        return result

    async def answer(self) -> dict[str, Any]:
        result = await self.at("ATA")
        if not result["ok"]:
            raise ModuleServiceError("modem rejected call answer")
        await self.events.publish(GatewayEvent("call.cellular.answered", {}))
        return result

    async def hangup(self) -> dict[str, Any]:
        result = await self.at("ATH")
        if not result["ok"]:
            raise ModuleServiceError("modem rejected call hangup")
        self._reset_incoming_ring()
        await self.events.publish(GatewayEvent("call.cellular.hung_up", {}))
        return result

    async def authorize_adb(self) -> bool:
        def work():
            with self._session() as session:
                authorize_qadbkey(session.open_at(handshake=True))
                adb = session.open_adb()
                adb.connect()
                return True

        return bool(await self.run_exclusive(work))

    async def _run(self, operation):
        try:
            return await asyncio.to_thread(operation)
        except ModuleServiceError:
            raise
        except Exception as exc:
            raise ModuleServiceError(f"QDC507 module operation unavailable: {type(exc).__name__}") from exc


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_phone_number(value: str) -> str:
    normalized = value.strip().replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\+?[0-9]{1,20}", normalized):
        raise ModuleServiceError("invalid phone number")
    return normalized


def _config_dict(config) -> dict[str, object]:
    return {
        "vendor_id": config.vendor_id,
        "product_id": config.product_id,
        "diagnostic": config.diagnostic,
        "nmea": config.nmea,
        "at": config.at,
        "modem": config.modem,
        "network": config.network,
        "adb": config.adb,
        "audio": config.audio,
    }
