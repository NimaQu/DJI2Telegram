from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict
from typing import Optional, Sequence

from qdc507_gateway import __version__
from qdc507_gateway.api.app import create_app
from qdc507_gateway.apns import APNsService
from qdc507_gateway.adb.runtime import ModuleVoiceController, RuntimeManifest
from qdc507_gateway.audio.bridge import AlsaAudioAdapter
from qdc507_gateway.config import PROJECT_CONFIG_FILE, Settings
from qdc507_gateway.events import EventBus
from qdc507_gateway.modem.call_monitor import monitor_cellular_call_status
from qdc507_gateway.modem.service import LiveModuleService
from qdc507_gateway.models import GatewayEvent
from qdc507_gateway.runtime import GatewayRuntime
from qdc507_gateway.security import AuthFailureLimiter
from qdc507_gateway.storage.database import Database
from qdc507_gateway.calls.core import CallCoordinator, CallBridgeError
from qdc507_gateway.calls.voip import VoIPPushService
from qdc507_gateway.calls.controller import ClientCallController
from qdc507_gateway.usb.descriptors import LibUSBDeviceLocator
from qdc507_gateway.web.calls import (
    AudioTicketStore,
    WebAudioDiagnosticService,
    WebAudioSession,
)


logger = logging.getLogger("qdc507_gateway.server")


def configure_application_logging(level: str) -> None:
    application_logger = logging.getLogger("qdc507_gateway")
    application_logger.setLevel(level)
    application_logger.propagate = False
    if not any(getattr(handler, "_qdc507_handler", False) for handler in application_logger.handlers):
        handler = logging.StreamHandler()
        handler._qdc507_handler = True
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
        application_logger.addHandler(handler)
    for handler in application_logger.handlers:
        handler.setLevel(level)


def build_app(settings: Optional[Settings] = None):
    settings = settings or Settings()
    service_started_at = time.monotonic()
    database = Database(settings.database_path)
    database.recover_client_calls()

    async def persist_event(event: GatewayEvent) -> None:
        await asyncio.to_thread(
            database.insert_event,
            event.type,
            json.dumps(event.payload, ensure_ascii=False, default=str),
            event.timestamp.isoformat(),
        )

    events = EventBus(persist=persist_event)
    locator = LibUSBDeviceLocator()
    state = {
        "status": {
            "service": "djisimhub",
            "version": __version__,
            "module_state": "disconnected",
            "web_enabled": settings.web_enabled,
        },
        "module": {"connected": False, "identity": None},
        "auth_limiter": AuthFailureLimiter(
            settings.auth_max_failures,
            settings.auth_failure_window_seconds,
            settings.auth_block_seconds,
        ),
    }
    apns_service = APNsService(settings, database)
    state["apns"] = apns_service
    module_service = LiveModuleService(
        database,
        events,
        lock_path=settings.lock_path,
        locator=locator,
        state=state,
    )
    runtime = GatewayRuntime(locator, events, state)
    call_coordinator = CallCoordinator()
    voip_service = VoIPPushService(apns_service, call_coordinator.current)
    module_voice_runtime = None
    if settings.module_voice_manifest is not None:
        manifest = RuntimeManifest.load(settings.module_voice_manifest)
        resource_dir = settings.module_voice_resource_dir or settings.module_voice_manifest.parent
        module_voice_runtime = ModuleVoiceController(
            module_service.open_adb_client,
            manifest,
            resource_dir,
            exclusive_runner=module_service.run_exclusive,
        )
    audio_adapter: AlsaAudioAdapter
    web_call_controller: ClientCallController

    async def incoming_cellular_call(number):
        await web_audio_diagnostic.stop()
        frontend = settings.incoming_call_frontend
        if frontend == "auto":
            frontend = "app"
        return await web_call_controller.start_inbound(number, frontend=frontend)

    async def record_call(record):
        await asyncio.to_thread(database.save_call, record)
        voip_service.on_state(record)
        await events.publish(GatewayEvent("call.state", {
            key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in asdict(record).items()
        }))

    async def gateway_status():
        current_call = await call_coordinator.current()
        return {
            **state.get("status", {}),
            "apns": {**apns_service.status(), "voip": voip_service.status()},
            "public_base_url": settings.public_base_url,
            "uptime_seconds": round(time.monotonic() - service_started_at, 3),
            "module": state.get("module", {"connected": False}),
            "audio": audio_adapter.stats(),
            "web_audio": web_audio_session.stats(),
            "audio_diagnostic_active": web_audio_diagnostic.active,
            "incoming_call_frontend": settings.incoming_call_frontend,
            "current_call": None if current_call is None else {
                "id": current_call.id,
                "direction": current_call.direction.value,
                "state": current_call.state.value,
                "cellular_number": current_call.cellular_number,
                "frontend": current_call.frontend,
            },
            "security": state["auth_limiter"].status(),
        }

    audio_adapter = AlsaAudioAdapter(
        event_publisher=events.publish,
        module_runtime=module_voice_runtime,
    )
    web_call_controller = ClientCallController(
        coordinator=call_coordinator,
        cellular_dial=module_service.dial,
        cellular_answer=module_service.answer,
        cellular_hangup=module_service.hangup,
        cellular_dtmf=module_service.send_dtmf,
        audio_start=audio_adapter.start_web,
        audio_stop=audio_adapter.stop,
        record_sink=record_call,
    )
    audio_tickets = AudioTicketStore()
    web_audio_session = WebAudioSession(web_call_controller, audio_adapter, audio_gain=settings.audio_gain)
    web_audio_diagnostic = WebAudioDiagnosticService(
        call_coordinator,
        audio_adapter,
        web_audio_session,
        audio_tickets,
    )

    async def cellular_connected():
        return await web_call_controller.cellular_connected()

    async def cellular_disconnected():
        return await web_call_controller.cellular_disconnected()

    async def hangup_active(call_id=None, reason="hangup"):
        return await web_call_controller.hangup(call_id, reason)

    def require_installation(installation_id):
        if not installation_id or not database.registered_installation(installation_id, apns_service.environment, apns_service.bundle_id):
            raise CallBridgeError("installation is not registered in this environment")

    async def issue_audio_ticket(call_id: str, installation_id=None):
        record = await web_call_controller.require_owner(call_id, installation_id)
        if record.frontend == "app":
            require_installation(installation_id)
        return audio_tickets.issue(call_id, installation_id if record.frontend == "app" else None)

    async def consume_audio_ticket(call_id: str, ticket: str):
        context = audio_tickets.take(call_id, ticket)
        if context is None:
            return False
        try:
            record = await web_call_controller.require_owner(call_id, context["installation_id"])
            if record.frontend == "app":
                require_installation(context["installation_id"])
        except CallBridgeError:
            return False
        return context

    async def answer_client_call(call_id: str, installation_id=None):
        record = await web_call_controller.require_call(call_id)
        if record.frontend == "app":
            require_installation(installation_id)
        return await web_call_controller.answer(call_id, installation_id)

    async def hangup_client_call(call_id: str, installation_id=None):
        record = await call_coordinator.current()
        if record is not None and record.frontend == "app":
            require_installation(installation_id)
        return await web_call_controller.hangup(call_id, installation_id=installation_id, client=True)

    async def send_client_dtmf(call_id: str, installation_id: str, digit: str):
        require_installation(installation_id)
        return await web_call_controller.send_dtmf(call_id, installation_id, digit)

    async def current_client_call():
        record = await call_coordinator.current()
        return record if record is not None and record.frontend in {"web", "app"} else None

    async def start_web_outbound(number: str, installation_id=None):
        if installation_id is not None:
            require_installation(installation_id)
        await web_audio_diagnostic.stop()
        return await web_call_controller.start_outbound(number, installation_id)

    async def reconnect_module():
        await web_audio_diagnostic.stop()
        await hangup_active(reason="module reconnect")
        was_monitoring = module_service.monitoring
        if was_monitoring:
            await module_service.stop_monitor()
        try:
            return await runtime.probe_once(reason="reconnect")
        finally:
            if was_monitoring:
                await module_service.start_monitor(
                    on_incoming_call=incoming_cellular_call,
                    on_call_disconnected=cellular_disconnected,
                    on_cellular_connected=cellular_connected,
                )

    state["reconnect"] = reconnect_module
    from qdc507_gateway.maintenance import MaintenanceController
    maintenance = MaintenanceController(settings, module_service, call_coordinator, web_audio_diagnostic)
    maintenance.lock = web_call_controller._operation_lock
    web_call_controller.start_guard = maintenance.require_available
    state["at"] = maintenance.at
    state["restart_service"] = maintenance.restart_service
    state["restart_module"] = maintenance.restart_module
    state["send_sms"] = module_service.send_sms
    state["authorize_adb"] = module_service.authorize_adb
    state["current_call"] = current_client_call
    state["hangup"] = hangup_client_call
    state["send_dtmf"] = send_client_dtmf
    state["start_web_call"] = start_web_outbound
    state["answer_web_call"] = answer_client_call
    state["issue_audio_ticket"] = issue_audio_ticket
    state["consume_audio_ticket"] = consume_audio_ticket
    state["run_audio_websocket"] = web_audio_session.run
    async def start_audio_diagnostic():
        async with maintenance.lock:
            maintenance.require_available()
            return await web_audio_diagnostic.create()

    state["start_audio_diagnostic"] = start_audio_diagnostic
    state["issue_audio_diagnostic_ticket"] = web_audio_diagnostic.issue_ticket
    state["consume_audio_diagnostic_ticket"] = web_audio_diagnostic.consume_ticket
    state["run_audio_diagnostic_websocket"] = web_audio_diagnostic.run
    state["get_audio"] = lambda: {
        **audio_adapter.stats(),
        "websocket": web_audio_session.stats(),
        "diagnostic_active": web_audio_diagnostic.active,
    }
    state["get_status"] = gateway_status

    async def refresh_module_status():
        current_call = await call_coordinator.current()
        if current_call is not None or web_audio_diagnostic.active:
            return {
                **state.get("module", {"connected": False}),
                "refresh_skipped": "audio_or_call_active",
            }
        network = await module_service.network_status()
        previous = state.get("module", {})
        updated = {
            **previous,
            "phone_number": network["phone_number"],
            "subscriber": network["subscriber"],
            "operator": network["operator"],
            "signal": network["signal"] or previous.get("signal"),
            "radio_metrics": network.get("radio_metrics"),
            "network_measured_at": network["measured_at"],
            "network_errors": network["errors"],
        }
        state["module"] = updated
        return updated

    state["refresh_module_status"] = refresh_module_status

    async def signal_monitor() -> None:
        last_marker = None
        while True:
            try:
                current_call = await call_coordinator.current()
                if current_call is None and not web_audio_diagnostic.active:
                    signal = await module_service.signal()
                    state["module"] = {**state.get("module", {}), "signal": signal}
                    marker = (
                        signal.get("available"), signal.get("rssi"),
                        signal.get("dbm"), signal.get("bars"), signal.get("ber"),
                    )
                    if marker != last_marker:
                        last_marker = marker
                        await events.publish(GatewayEvent("module.signal", signal))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                signal = {
                    "available": False,
                    "rssi": None,
                    "dbm": None,
                    "bars": 0,
                    "ber": None,
                    "error": type(exc).__name__,
                }
                state["module"] = {**state.get("module", {}), "signal": signal}
                marker = ("error", type(exc).__name__)
                if marker != last_marker:
                    last_marker = marker
                    await events.publish(GatewayEvent("module.signal", signal))
            await asyncio.sleep(10.0)

    async def network_status_monitor() -> None:
        last_marker = None
        while True:
            try:
                updated = await refresh_module_status()
                operator = updated.get("operator", {})
                marker = (
                    updated.get("phone_number"),
                    operator.get("name"),
                    operator.get("radio"),
                    tuple(sorted(updated.get("network_errors", {}).items())),
                )
                if marker != last_marker:
                    last_marker = marker
                    await events.publish(GatewayEvent("module.network_status", {
                        "phone_number": updated.get("phone_number"),
                        "operator": operator,
                        "network_errors": updated.get("network_errors", {}),
                        "measured_at": updated.get("network_measured_at"),
                    }))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state["module"] = {
                    **state.get("module", {}),
                    "network_error": type(exc).__name__,
                }
                await events.publish(GatewayEvent("module.network_status_error", {
                    "error": type(exc).__name__,
                }))
            await asyncio.sleep(300.0)

    @asynccontextmanager
    async def lifespan(_app):
        signal_task = None
        network_status_task = None
        call_status_task = None
        try:
            await apns_service.start()
            await runtime.probe_once(reason="startup")
            await module_service.start_monitor(
                on_incoming_call=incoming_cellular_call,
                on_call_disconnected=cellular_disconnected,
                on_cellular_connected=cellular_connected,
            )
            signal_task = asyncio.create_task(signal_monitor())
            network_status_task = asyncio.create_task(network_status_monitor())
            call_status_task = asyncio.create_task(monitor_cellular_call_status(
                call_coordinator.current,
                module_service.voice_call_status,
                cellular_connected,
                events.publish,
            ))
            yield
        finally:
            if call_status_task is not None:
                call_status_task.cancel()
                await asyncio.gather(call_status_task, return_exceptions=True)
            if signal_task is not None:
                signal_task.cancel()
                await asyncio.gather(signal_task, return_exceptions=True)
            if network_status_task is not None:
                network_status_task.cancel()
                await asyncio.gather(network_status_task, return_exceptions=True)
            # Registered in reverse shutdown order; every cleanup runs even
            # when an earlier one raises, just like nested finally blocks.
            async with AsyncExitStack() as cleanup:
                cleanup.callback(database.close)
                cleanup.push_async_callback(maintenance.stop)
                cleanup.push_async_callback(apns_service.stop)
                cleanup.push_async_callback(voip_service.stop)
                cleanup.push_async_callback(runtime.stop)
                cleanup.callback(module_service.close)
                cleanup.push_async_callback(module_service.stop_monitor)
                cleanup.push_async_callback(hangup_active, reason="service shutdown")
                cleanup.push_async_callback(web_audio_diagnostic.stop)

    app = create_app(database, events, state, lifespan=lifespan)
    app.state.gateway_database = database
    app.state.gateway_events = events
    app.state.gateway_settings = settings
    app.state.gateway_runtime = runtime
    app.state.gateway_module_service = module_service
    app.state.gateway_call_coordinator = call_coordinator
    app.state.gateway_web_calls = web_call_controller
    app.state.gateway_voip = voip_service
    app.state.gateway_audio_adapter = audio_adapter
    app.state.gateway_audio_diagnostic = web_audio_diagnostic
    return app


async def run_headless(app, stop_event: asyncio.Event | None = None) -> None:
    """Run the gateway lifespan without creating an HTTP listening socket."""
    loop = asyncio.get_running_loop()
    stop = stop_event or asyncio.Event()
    installed_signals = []
    if stop_event is None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stop.set)
                installed_signals.append(signum)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        async with app.router.lifespan_context(app):
            await stop.wait()
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)


def run(settings: Settings) -> int:
    configure_application_logging(settings.log_level)
    app = build_app(settings)
    if not settings.web_enabled:
        logger.info("Web console and API disabled; no HTTP socket will be opened")
        asyncio.run(run_headless(app))
        return 0
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("uvicorn is required to run the daemon") from exc
    kwargs = {
        "host": settings.host,
        "port": settings.port,
        "log_level": settings.log_level.lower(),
        # SSE and WebSocket clients can stay connected indefinitely. Give
        # them a bounded drain window so systemd stop/restart never hangs on
        # an open browser tab.
        "timeout_graceful_shutdown": 10,
    }
    uvicorn.run(app, **kwargs)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="djisimhub-server")
    parser.parse_args(argv)
    return run(Settings.load(PROJECT_CONFIG_FILE))


if __name__ == "__main__":
    main()
