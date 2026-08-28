from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import secrets
import sys
import tempfile
from pathlib import Path

from qdc507_gateway.config import PROJECT_CONFIG_FILE, Settings
from qdc507_gateway.models import USBProbeReport
from qdc507_gateway.security import hash_token, new_token
from qdc507_gateway.storage.database import Database
from qdc507_gateway.usb.descriptors import JSONDeviceLocator, LibUSBDeviceLocator


def _probe(args: argparse.Namespace, _settings: Settings) -> int:
    locator = JSONDeviceLocator(args.fixture) if args.fixture else LibUSBDeviceLocator()
    try:
        devices = locator.find()
    finally:
        closer = getattr(locator, "close", None)
        if callable(closer):
            closer()
    if not devices:
        report = USBProbeReport(False, None, ("QDC507 2C7C:0125 was not found",))
    else:
        device = devices[0]
        warnings = []
        if not device.adb_interfaces:
            warnings.append("no descriptor-qualified ADB interface FF/42/01 found")
        if not device.uac_interfaces:
            warnings.append("no UAC interface found")
        if not device.uac_audio_endpoints:
            warnings.append("no descriptor-qualified QDC507 UAC audio endpoint found")
        report = USBProbeReport(True, device, tuple(warnings))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.found else 2


def _token(args: argparse.Namespace, settings: Settings) -> int:
    token = new_token()
    token_id = args.token_id or secrets.token_hex(8)
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else settings.data_dir
    database = Database(data_dir / "gateway.sqlite3")
    database.save_token(
        token_id,
        hash_token(token),
        dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    database.close()
    print(json.dumps({"token_id": token_id, "token": token}, indent=2))
    return 0


def _token_revoke(args: argparse.Namespace, settings: Settings) -> int:
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else settings.data_dir
    database = Database(data_dir / "gateway.sqlite3")
    try:
        revoked = database.revoke_token(
            args.token_id,
            dt.datetime.now(dt.timezone.utc).isoformat(),
        )
    finally:
        database.close()
    print(json.dumps({"token_id": args.token_id, "revoked": revoked}, indent=2))
    return 0 if revoked else 1


def _telegram_login(args: argparse.Namespace, settings: Settings) -> int:
    from qdc507_gateway.telegram.kurigram import create_kurigram_client, ensure_session_permissions

    api_id = args.api_id or settings.telegram_api_id
    api_hash = args.api_hash or settings.telegram_api_hash
    if not api_id or not api_hash:
        raise SystemExit(
            "telegram-login requires telegram.api_id and telegram.api_hash in config.toml "
            "or --api-id/--api-hash"
        )
    session_path = Path(args.session).expanduser() if args.session else settings.telegram_session
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.parent.chmod(0o700)
    client = create_kurigram_client(
        session_path.stem,
        int(api_id),
        api_hash,
        session_path.parent,
    )

    async def login():
        await client.start()
        await client.stop()

    asyncio.run(login())
    ensure_session_permissions(session_path)
    print(f"Telegram session ready: {session_path}")
    return 0


def _telegram_compat(_args: argparse.Namespace, _settings: Settings) -> int:
    """Probe Kurigram/PyTgCalls surfaces without login or a phone call."""
    from qdc507_gateway.telegram.compatibility import (
        probe_kurigram_client,
        probe_kurigram_pytgcalls_runtime,
        probe_kurigram_raw_phone_types,
    )
    from qdc507_gateway.telegram.kurigram import create_kurigram_client

    with tempfile.TemporaryDirectory(prefix="qdc507-telegram-compat-") as workdir:
        client = create_kurigram_client("compat", 1, "0" * 32, workdir)
        client_report = probe_kurigram_client(client)
        raw_report = probe_kurigram_raw_phone_types()
        bridge_report = probe_kurigram_pytgcalls_runtime(client)
    result = {
        "provider": "kurigram",
        "import_namespace": "pyrogram",
        "login_performed": False,
        "call_performed": False,
        "message_client": {
            "passed": client_report.passed,
            "missing": list(client_report.missing),
        },
        "pytgcalls_runtime": {
            "passed": bridge_report.passed,
            "missing": list(bridge_report.missing),
        },
        "raw_phone_types": {
            "passed": raw_report.passed,
            "missing": list(raw_report.missing),
        },
    }
    print(json.dumps(result, indent=2))
    return 0 if client_report.passed and raw_report.passed and bridge_report.passed else 2


def _adb_authorize(args: argparse.Namespace, settings: Settings) -> int:
    if not args.confirm:
        raise SystemExit("adb-authorize requires --confirm")
    from qdc507_gateway.events import EventBus
    from qdc507_gateway.modem.service import LiveModuleService

    database = Database(":memory:")
    service = LiveModuleService(database, EventBus(), lock_path=settings.lock_path)

    async def authorize() -> bool:
        try:
            return await service.authorize_adb()
        finally:
            service.close()
            database.close()

    result = asyncio.run(authorize())
    print(json.dumps({"authorized": bool(result)}, indent=2))
    return 0 if result else 2


def _module_setup(args: argparse.Namespace, settings: Settings) -> int:
    if not args.confirm:
        raise SystemExit(
            "module-setup changes persistent USBCFG and may restart the module; "
            "pass --confirm"
        )
    from qdc507_gateway.adb.runtime import ModuleVoiceController, RuntimeManifest
    from qdc507_gateway.events import EventBus
    from qdc507_gateway.modem.service import LiveModuleService
    from qdc507_gateway.module_setup import setup_module

    locator = LibUSBDeviceLocator()
    database = Database(":memory:")
    service = LiveModuleService(
        database,
        EventBus(),
        lock_path=settings.lock_path,
        locator=locator,
    )
    voice_controller = None
    if settings.module_voice_manifest is not None:
        manifest = RuntimeManifest.load(settings.module_voice_manifest)
        resource_dir = (
            settings.module_voice_resource_dir or settings.module_voice_manifest.parent
        )
        voice_controller = ModuleVoiceController(
            service.open_adb_client,
            manifest,
            resource_dir,
            exclusive_runner=service.run_exclusive,
        )

    def progress(message: str) -> None:
        print(f"module-setup: {message}", file=sys.stderr, flush=True)

    async def run_setup() -> dict[str, object]:
        return await setup_module(service, voice_controller, progress=progress)

    try:
        result = asyncio.run(run_setup())
    except Exception as exc:
        print(json.dumps({
            "ready": False,
            "error": type(exc).__name__,
            "message": " ".join(str(exc).split())[-800:],
        }, ensure_ascii=False, indent=2))
        return 2
    finally:
        service.close()
        locator.close()
        database.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _config_check(_args: argparse.Namespace, settings: Settings) -> int:
    result = {
        "config": None if settings.config_path is None else str(settings.config_path),
        "data_dir": str(settings.data_dir),
        "database": str(settings.database_path),
        "lock_path": str(settings.lock_path),
        "server": {
            "host": settings.host,
            "port": settings.port,
        },
        "logging": {"level": settings.log_level},
        "security": {
            "auth_max_failures": settings.auth_max_failures,
            "auth_failure_window_seconds": settings.auth_failure_window_seconds,
            "auth_block_seconds": settings.auth_block_seconds,
        },
        "telegram": {
            "configured": bool(
                settings.telegram_api_id
                and settings.telegram_api_hash
                and settings.telegram_user_id
            ),
            "session": str(settings.telegram_session),
            "user_id": settings.telegram_user_id,
            "bot_configured": bool(settings.telegram_bot_token),
            "bot_session": str(settings.telegram_bot_session),
            "allow_service_restart": settings.telegram_allow_service_restart,
        },
        "incoming_call_frontend": settings.incoming_call_frontend,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _serve(_args: argparse.Namespace, settings: Settings) -> int:
    from qdc507_gateway.server import run

    return run(settings)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="qdc507-gateway")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    serve = subparsers.add_parser("serve", help="run the gateway web/API service")
    serve.set_defaults(handler=_serve)

    probe = subparsers.add_parser("probe", help="descriptor-only USB probe")
    probe.add_argument("--fixture", help="offline JSON descriptor fixture")
    probe.add_argument("--json", action="store_true", help="emit JSON output")
    probe.set_defaults(handler=_probe)

    token = subparsers.add_parser("token", help="create a token; only its hash is persisted")
    token.add_argument("--data-dir", help="override app.data_dir")
    token.add_argument("--token-id")
    token.set_defaults(handler=_token)

    revoke = subparsers.add_parser("token-revoke", help="revoke an API token")
    revoke.add_argument("token_id")
    revoke.add_argument("--data-dir", help="override app.data_dir")
    revoke.set_defaults(handler=_token_revoke)

    telegram = subparsers.add_parser("telegram-login", help="create a Kurigram User API session")
    telegram.add_argument("--api-id", type=int)
    telegram.add_argument("--api-hash")
    telegram.add_argument("--session")
    telegram.set_defaults(handler=_telegram_login)

    compat = subparsers.add_parser(
        "telegram-compat",
        help="probe Kurigram/PyTgCalls APIs without login or a call",
    )
    compat.set_defaults(handler=_telegram_compat)

    adb_authorize = subparsers.add_parser(
        "adb-authorize",
        help="perform confirmed QADBKEY authorization for module ADB",
    )
    adb_authorize.add_argument("--confirm", action="store_true")
    adb_authorize.set_defaults(handler=_adb_authorize)

    module_setup = subparsers.add_parser(
        "module-setup",
        help="apply complete USBCFG, authorize ADB, and test the voice runtime",
    )
    module_setup.add_argument(
        "--confirm",
        action="store_true",
        help="confirm the persistent USBCFG change and one module restart",
    )
    module_setup.set_defaults(handler=_module_setup)

    config_check = subparsers.add_parser(
        "config-check",
        help="validate and print a redacted configuration summary",
    )
    config_check.set_defaults(handler=_config_check)

    args = parser.parse_args(argv)
    settings = Settings.load(PROJECT_CONFIG_FILE)
    return args.handler(args, settings)


if __name__ == "__main__":
    sys.exit(main())
