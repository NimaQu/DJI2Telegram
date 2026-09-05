from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ConfigurationError(ValueError):
    pass


PROJECT_CONFIG_FILE = "config.toml"


def _table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"[{name}] must be a TOML table")
    return value


def _path(value: Any, base_dir: Path, field: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ConfigurationError(f"{field} must be a non-empty path")
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def _optional_path(value: Any, base_dir: Path, field: str) -> Path | None:
    if value in (None, ""):
        return None
    return _path(value, base_dir, field)


def _optional_int(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ConfigurationError(f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field} must be an integer") from exc


def _optional_string(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _boolean(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigurationError(f"{field} must be a boolean")


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field} must be a positive integer") from exc
    if result <= 0:
        raise ConfigurationError(f"{field} must be a positive integer")
    return result


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/var/lib/qdc507-gateway")
    lock_path: Path = Path("/run/qdc507-gateway/device.lock")
    web_enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8787
    telegram_session: Path = Path("/var/lib/qdc507-gateway/telegram.session")
    telegram_bot_session: Path = Path("/var/lib/qdc507-gateway/telegram-bot.session")
    telegram_user_id: int | None = None
    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_bot_token: str | None = None
    telegram_allow_service_restart: bool = False
    module_voice_manifest: Path | None = None
    module_voice_resource_dir: Path | None = None
    incoming_call_frontend: str = "telegram"
    log_level: str = "INFO"
    auth_max_failures: int = 10
    auth_failure_window_seconds: int = 300
    auth_block_seconds: int = 900
    config_path: Path | None = None

    def __post_init__(self) -> None:
        if self.incoming_call_frontend not in {"web", "telegram", "auto"}:
            raise ConfigurationError("calls.incoming_frontend must be web, telegram, or auto")
        if not self.web_enabled and self.incoming_call_frontend == "web":
            raise ConfigurationError(
                "calls.incoming_frontend cannot be web when server.enabled is false"
            )
        if not 1 <= self.port <= 65535:
            raise ConfigurationError("server.port must be between 1 and 65535")
        if self.log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ConfigurationError(
                "logging.level must be CRITICAL, ERROR, WARNING, INFO, or DEBUG"
            )
        for field, value in (
            ("security.auth_max_failures", self.auth_max_failures),
            ("security.auth_failure_window_seconds", self.auth_failure_window_seconds),
            ("security.auth_block_seconds", self.auth_block_seconds),
        ):
            if value <= 0:
                raise ConfigurationError(f"{field} must be a positive integer")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "gateway.sqlite3"

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Settings":
        """Load project TOML with environment variables retained as overrides."""
        env = os.environ if environ is None else environ
        requested = path or env.get("QDC507_CONFIG")
        config_path: Path | None
        if requested:
            config_path = Path(requested).expanduser().resolve()
            if not config_path.is_file():
                raise ConfigurationError(f"configuration file not found: {config_path}")
        else:
            candidate = (Path.cwd() / "config.toml").resolve()
            config_path = candidate if candidate.is_file() else None

        document: Mapping[str, Any] = {}
        if config_path is not None:
            try:
                with config_path.open("rb") as handle:
                    document = tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigurationError(f"invalid TOML in {config_path}: {exc}") from exc
        base_dir = config_path.parent if config_path is not None else Path.cwd()
        app = _table(document, "app")
        server = _table(document, "server")
        telegram = _table(document, "telegram")
        calls = _table(document, "calls")
        module = _table(document, "module")
        logging_config = _table(document, "logging")
        security = _table(document, "security")
        legacy_telegram_fields = {"personal_user_id", "admin_user_ids"} & set(telegram)
        if legacy_telegram_fields:
            raise ConfigurationError(
                "replace telegram.personal_user_id/admin_user_ids with telegram.user_id"
            )

        data_dir = _path(
            env.get("QDC507_DATA_DIR", app.get("data_dir", "/var/lib/qdc507-gateway")),
            base_dir,
            "app.data_dir",
        )
        lock_path = _path(
            env.get("QDC507_LOCK_PATH", app.get("lock_path", str(data_dir / "device.lock"))),
            base_dir,
            "app.lock_path",
        )
        session = _path(
            env.get(
                "QDC507_TELEGRAM_SESSION",
                telegram.get("session", str(data_dir / "telegram.session")),
            ),
            base_dir,
            "telegram.session",
        )
        bot_session = _path(
            env.get(
                "QDC507_TELEGRAM_BOT_SESSION",
                telegram.get("bot_session", str(data_dir / "telegram-bot.session")),
            ),
            base_dir,
            "telegram.bot_session",
        )
        host = env.get("QDC507_HOST", server.get("host", "127.0.0.1"))
        if not isinstance(host, str) or not host.strip():
            raise ConfigurationError("server.host must be a non-empty string")
        frontend = env.get(
            "QDC507_INCOMING_CALL_FRONTEND",
            calls.get("incoming_frontend", "telegram"),
        )
        if not isinstance(frontend, str):
            raise ConfigurationError("calls.incoming_frontend must be a string")

        try:
            port = int(env.get("QDC507_PORT", server.get("port", 8787)))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("server.port must be an integer") from exc
        log_level_value = env.get("QDC507_LOG_LEVEL", logging_config.get("level", "INFO"))
        if not isinstance(log_level_value, str) or not log_level_value.strip():
            raise ConfigurationError("logging.level must be a non-empty string")

        return cls(
            data_dir=data_dir,
            lock_path=lock_path,
            web_enabled=_boolean(
                env.get("QDC507_SERVER_ENABLED", server.get("enabled", True)),
                "server.enabled",
            ),
            host=host.strip(),
            port=port,
            telegram_session=session,
            telegram_bot_session=bot_session,
            telegram_user_id=_optional_int(
                env.get("QDC507_TELEGRAM_USER_ID", telegram.get("user_id")),
                "telegram.user_id",
            ),
            telegram_api_id=_optional_int(
                env.get("QDC507_TELEGRAM_API_ID", telegram.get("api_id")),
                "telegram.api_id",
            ),
            telegram_api_hash=_optional_string(
                env.get("QDC507_TELEGRAM_API_HASH", telegram.get("api_hash")),
                "telegram.api_hash",
            ),
            telegram_bot_token=_optional_string(
                env.get("QDC507_TELEGRAM_BOT_TOKEN", telegram.get("bot_token")),
                "telegram.bot_token",
            ),
            telegram_allow_service_restart=_boolean(
                env.get(
                    "QDC507_TELEGRAM_ALLOW_SERVICE_RESTART",
                    telegram.get("allow_service_restart", False),
                ),
                "telegram.allow_service_restart",
            ),
            module_voice_manifest=_optional_path(
                env.get("QDC507_MODULE_VOICE_MANIFEST", module.get("voice_manifest")),
                base_dir,
                "module.voice_manifest",
            ),
            module_voice_resource_dir=_optional_path(
                env.get("QDC507_MODULE_VOICE_RESOURCE_DIR", module.get("voice_resource_dir")),
                base_dir,
                "module.voice_resource_dir",
            ),
            incoming_call_frontend=frontend.strip().lower(),
            log_level=log_level_value.strip().upper(),
            auth_max_failures=_positive_int(
                env.get(
                    "QDC507_AUTH_MAX_FAILURES",
                    security.get("auth_max_failures", 10),
                ),
                "security.auth_max_failures",
            ),
            auth_failure_window_seconds=_positive_int(
                env.get(
                    "QDC507_AUTH_FAILURE_WINDOW_SECONDS",
                    security.get("auth_failure_window_seconds", 300),
                ),
                "security.auth_failure_window_seconds",
            ),
            auth_block_seconds=_positive_int(
                env.get(
                    "QDC507_AUTH_BLOCK_SECONDS",
                    security.get("auth_block_seconds", 900),
                ),
                "security.auth_block_seconds",
            ),
            config_path=config_path,
        )
