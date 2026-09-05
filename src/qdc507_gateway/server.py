from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from contextlib import asynccontextmanager
from typing import Optional, Sequence

from qdc507_gateway import __version__
from qdc507_gateway.api.app import create_app
from qdc507_gateway.adb.runtime import ModuleVoiceController, RuntimeManifest
from qdc507_gateway.audio.bridge import AlsaNTgCallsAudioAdapter
from qdc507_gateway.config import PROJECT_CONFIG_FILE, Settings
from qdc507_gateway.events import EventBus
from qdc507_gateway.modem.call_monitor import monitor_cellular_call_status
from qdc507_gateway.modem.service import LiveModuleService
from qdc507_gateway.models import GatewayEvent
from qdc507_gateway.runtime import GatewayRuntime
from qdc507_gateway.security import AuthFailureLimiter
from qdc507_gateway.storage.database import Database
from qdc507_gateway.telegram.calls import CallBridgeOrchestrator, CallCoordinator
from qdc507_gateway.telegram.service import KurigramTelegramService
from qdc507_gateway.usb.descriptors import LibUSBDeviceLocator
from qdc507_gateway.web.calls import (
    AudioTicketStore,
    WebAudioDiagnosticService,
    WebAudioSession,
    WebCallController,
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

    async def persist_event(event: GatewayEvent) -> None:
        await asyncio.to_thread(
            database.insert_event,
            event.type,
            json.dumps(event.payload, ensure_ascii=False, default=str),
            event.timestamp.isoformat(),
        )

    events = EventBus(persist=persist_event)
    locator = LibUSBDeviceLocator(vendor_id=settings.usb_vendor_id, product_id=settings.usb_product_id)
    state = {
        "status": {
            "service": "DJI2Telegram",
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
    module_service = LiveModuleService(
        database,
        events,
        lock_path=settings.lock_path,
        locator=locator,
        vendor_id=settings.usb_vendor_id, product_id=settings.usb_product_id,
        state=state,
    )
    runtime = GatewayRuntime(locator, database, events, state)
    call_coordinator = CallCoordinator()
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
    audio_adapter: AlsaNTgCallsAudioAdapter
    telegram_service: KurigramTelegramService
    web_call_controller: WebCallController

    async def request_telegram_call(user_id: int):
        return await telegram_service.request_private_call(user_id, stream=audio_adapter.stream())

    async def hangup_telegram_call(handle):
        return await telegram_service.hangup_private_call(handle)

    async def telegram_connected(_handle):
        await call_orchestrator.telegram_connected()

    async def telegram_failed(_handle, _error):
        await call_orchestrator.telegram_disconnected()

    async def telegram_disconnected(_handle):
        await call_orchestrator.telegram_disconnected()

    async def incoming_cellular_call(number):
        await web_audio_diagnostic.stop()
        try:
            await telegram_service.notify_incoming_cellular_call(number)
        except Exception as exc:
            await events.publish(GatewayEvent("call.notification_error", {
                "error": type(exc).__name__,
            }))
        frontend = settings.incoming_call_frontend
        if frontend == "auto":
            if not settings.web_enabled:
                frontend = "telegram"
            else:
                frontend = (
                    "telegram"
                    if telegram_service.state == "connected"
                    and telegram_service.call_bridge is not None
                    else "web"
                )
        if frontend == "telegram":
            return await call_orchestrator.start_inbound(number)
        return await web_call_controller.start_inbound(number)

    async def record_call(record):
        await asyncio.to_thread(database.save_call, record)
        await events.publish(GatewayEvent("call.state", {
            "id": record.id,
            "direction": record.direction.value,
            "state": record.state.value,
            "cellular_number": record.cellular_number,
            "telegram_user_id": record.telegram_user_id,
            "frontend": record.frontend,
            "last_error": record.last_error,
        }))

    async def gateway_status():
        telegram = state.get("telegram")
        current_call = await call_coordinator.current()
        return {
            **state.get("status", {}),
            "uptime_seconds": round(time.monotonic() - service_started_at, 3),
            "module": state.get("module", {"connected": False}),
            "telegram_state": None if telegram is None else telegram.state,
            "telegram_last_error": None if telegram is None else telegram.last_error,
            "telegram_bot_state": None if telegram is None else telegram.bot_state,
            "telegram_bot_last_error": None if telegram is None else telegram.bot_last_error,
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

    audio_adapter = AlsaNTgCallsAudioAdapter(
        lambda: telegram_service.call_bridge,
        event_publisher=events.publish,
        module_runtime=module_voice_runtime,
        vendor_id=settings.usb_vendor_id, product_id=settings.usb_product_id,
    )
    call_orchestrator = CallBridgeOrchestrator(
        coordinator=call_coordinator,
        user_id=settings.telegram_user_id or 0,
        request_telegram=request_telegram_call,
        telegram_hangup=hangup_telegram_call,
        cellular_dial=module_service.dial,
        cellular_answer=module_service.answer,
        cellular_hangup=module_service.hangup,
        audio_start=audio_adapter.start,
        audio_stop=audio_adapter.stop,
        audio_dial_cue=audio_adapter.play_telegram_dial_cue,
        record_sink=record_call,
    )
    web_call_controller = WebCallController(
        coordinator=call_coordinator,
        cellular_dial=module_service.dial,
        cellular_answer=module_service.answer,
        cellular_hangup=module_service.hangup,
        audio_start=audio_adapter.start_web,
        audio_stop=audio_adapter.stop,
        record_sink=record_call,
    )
    audio_tickets = AudioTicketStore()
    web_audio_session = WebAudioSession(web_call_controller, audio_adapter)
    web_audio_diagnostic = WebAudioDiagnosticService(
        call_coordinator,
        audio_adapter,
        web_audio_session,
        audio_tickets,
    )

    async def active_frontend():
        record = await call_coordinator.current()
        return None if record is None else record.frontend

    async def cellular_connected():
        frontend = await active_frontend()
        if frontend is None:
            return None
        if frontend == "web":
            return await web_call_controller.cellular_connected()
        return await call_orchestrator.cellular_connected()

    async def cellular_disconnected():
        frontend = await active_frontend()
        if frontend is None:
            return None
        if frontend == "web":
            return await web_call_controller.cellular_disconnected()
        return await call_orchestrator.cellular_disconnected()

    async def hangup_active(call_id=None, reason="hangup"):
        if await active_frontend() == "web":
            return await web_call_controller.hangup(call_id, reason)
        return await call_orchestrator.hangup(call_id, reason)

    async def issue_audio_ticket(call_id: str):
        await web_call_controller.require_call(call_id)
        return audio_tickets.issue(call_id)

    async def start_web_outbound(number: str):
        await web_audio_diagnostic.stop()
        return await web_call_controller.start_outbound(number)

    async def start_telegram_outbound(number: str, user_id: int):
        await web_audio_diagnostic.stop()
        return await call_orchestrator.start_outbound(number, user_id)

    async def user_login_allowed() -> bool:
        return await call_coordinator.current() is None and not web_audio_diagnostic.active

    maintenance_tasks: set[asyncio.Task] = set()

    async def restart_gateway_service() -> str:
        if not settings.telegram_allow_service_restart:
            raise RuntimeError("请先在 config.toml 启用 telegram.allow_service_restart")

        async def restart_later() -> None:
            await asyncio.sleep(1.0)
            try:
                process = await asyncio.create_subprocess_exec(
                    "systemctl",
                    "--no-block",
                    "restart",
                    "dji2telegram.service",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await process.communicate()
                if process.returncode:
                    logger.error(
                        "bot-requested service restart failed: %s",
                        stderr.decode("utf-8", "replace").strip(),
                    )
            except Exception:
                logger.exception("bot-requested service restart failed")

        task = asyncio.create_task(restart_later())
        maintenance_tasks.add(task)
        task.add_done_callback(maintenance_tasks.discard)
        return "已安排 systemd 重启，Bot 预计数秒后恢复连接。"

    async def restart_module():
        current_call = await call_coordinator.current()
        if current_call is not None or web_audio_diagnostic.active:
            raise RuntimeError("通话或音频诊断期间不能重启模块")
        await events.publish(GatewayEvent("module.restart_requested", {
            "command": "AT+CFUN=1,1",
            "source": "telegram_bot",
        }))
        result = await module_service.at("AT+CFUN=1,1", timeout_ms=10000)
        await events.publish(GatewayEvent("module.restart_completed", {
            "command": "AT+CFUN=1,1",
            "reenumerated": result.get("reenumerated") if isinstance(result, dict) else None,
        }))
        return result

    telegram_service = KurigramTelegramService(
        session_path=settings.telegram_session,
        bot_session_path=settings.telegram_bot_session,
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        bot_token=settings.telegram_bot_token,
        user_id=settings.telegram_user_id,
        events=events,
        status=gateway_status,
        start_call=start_telegram_outbound,
        send_sms=module_service.send_sms,
        hangup=hangup_active,
        telegram_call_connected=telegram_connected,
        telegram_call_failed=telegram_failed,
        telegram_call_disconnected=telegram_disconnected,
        restart_service=(
            restart_gateway_service
            if settings.telegram_allow_service_restart
            else None
        ),
        send_at=module_service.at,
        restart_module=restart_module,
        user_login_allowed=user_login_allowed,
    )
    module_service.sms_forwarder = telegram_service.forward_sms

    async def reconnect_module():
        await web_audio_diagnostic.stop()
        await hangup_active(reason="module reconnect")
        callbacks = (
            incoming_cellular_call,
            cellular_disconnected,
            cellular_connected,
        )
        was_monitoring = module_service.monitoring
        if was_monitoring:
            await module_service.stop_monitor()
        try:
            return await runtime.reconnect()
        finally:
            if was_monitoring:
                await module_service.start_monitor(
                    on_incoming_call=callbacks[0],
                    on_call_disconnected=callbacks[1],
                    on_cellular_connected=callbacks[2],
                )

    state["reconnect"] = reconnect_module
    state["at"] = module_service.at
    state["send_sms"] = module_service.send_sms
    state["authorize_adb"] = module_service.authorize_adb
    state["current_call"] = call_coordinator.current
    state["hangup"] = hangup_active
    state["start_web_call"] = start_web_outbound
    state["answer_web_call"] = web_call_controller.answer
    state["issue_audio_ticket"] = issue_audio_ticket
    state["consume_audio_ticket"] = audio_tickets.consume
    state["run_audio_websocket"] = web_audio_session.run
    state["start_audio_diagnostic"] = web_audio_diagnostic.create
    state["issue_audio_diagnostic_ticket"] = web_audio_diagnostic.issue_ticket
    state["consume_audio_diagnostic_ticket"] = web_audio_diagnostic.consume_ticket
    state["run_audio_diagnostic_websocket"] = web_audio_diagnostic.run
    state["get_audio"] = lambda: {
        **audio_adapter.stats(),
        "websocket": web_audio_session.stats(),
        "diagnostic_active": web_audio_diagnostic.active,
    }
    state["telegram"] = telegram_service
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
            await runtime.start()
            await telegram_service.start()
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
            try:
                await web_audio_diagnostic.stop()
            finally:
                try:
                    await hangup_active(reason="service shutdown")
                finally:
                    try:
                        await module_service.stop_monitor()
                    finally:
                        try:
                            module_service.close()
                        finally:
                            try:
                                await telegram_service.stop()
                            finally:
                                try:
                                    await runtime.stop()
                                finally:
                                    database.close()

    app = create_app(database, events, state, lifespan=lifespan)
    app.state.gateway_database = database
    app.state.gateway_events = events
    app.state.gateway_settings = settings
    app.state.gateway_runtime = runtime
    app.state.gateway_module_service = module_service
    app.state.gateway_call_coordinator = call_coordinator
    app.state.gateway_web_calls = web_call_controller
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

    parser = argparse.ArgumentParser(prog="dji2telegram-server")
    parser.parse_args(argv)
    return run(Settings.load(PROJECT_CONFIG_FILE))


if __name__ == "__main__":
    main()
