from __future__ import annotations

import os
import re
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
    allow_service_restart: bool = False
    systemd_unit: str = "djisimhub.service"
    module_voice_manifest: Path | None = None
    module_voice_resource_dir: Path | None = None
    incoming_call_frontend: str = "app"
    log_level: str = "INFO"
    auth_max_failures: int = 10
    auth_failure_window_seconds: int = 300
    auth_block_seconds: int = 900
    config_path: Path | None = None
    network_apn: str | None = None
    network_pdp_type: str = "IP"

    public_base_url: str | None = None
    apns_enabled: bool = False
    apns_sandbox: bool = True
    apns_key_path: Path | None = None
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_bundle_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.systemd_unit, str) or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service", self.systemd_unit) is None:
            raise ConfigurationError("server.systemd_unit must be a systemd service name")
        if self.public_base_url:
            from urllib.parse import urlsplit
            url = urlsplit(self.public_base_url)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
                raise ConfigurationError("server.public_base_url must be an HTTPS origin")
        if self.apns_enabled:
            if not all((self.apns_key_path, self.apns_key_id, self.apns_team_id, self.apns_bundle_id)):
                raise ConfigurationError("apns requires key_path, key_id, team_id and bundle_id")
            try:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.asymmetric import ec
                import jwt
                key = serialization.load_pem_private_key(self.apns_key_path.read_bytes(), password=None)
                if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
                    raise ValueError("expected P-256")
                jwt.encode({"iss": self.apns_team_id, "iat": 0}, key, algorithm="ES256", headers={"kid": self.apns_key_id})
            except Exception:
                raise ConfigurationError("apns.key_path must contain a readable ES256 P-256 private key") from None

        if self.network_apn is not None and (
            not isinstance(self.network_apn, str)
            or len(self.network_apn) > 100
            or re.fullmatch(r"[A-Za-z0-9_.-]*", self.network_apn) is None
        ):
            raise ConfigurationError("network.apn must be empty or an APN of up to 100 ASCII letters, digits, dots, underscores or hyphens")
        if not isinstance(self.network_pdp_type, str) or self.network_pdp_type not in {"IP", "IPV6", "IPV4V6"}:
            raise ConfigurationError("network.pdp_type must be IP, IPV6, or IPV4V6")
        if self.incoming_call_frontend not in {"web", "app", "auto"}:
            raise ConfigurationError("calls.incoming_frontend must be app, web, or auto")
        if not self.web_enabled and self.incoming_call_frontend in {"web", "app"}:
            raise ConfigurationError(
                "calls.incoming_frontend cannot be web or app when server.enabled is false"
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
        apns = _table(document, "apns")
        app = _table(document, "app")
        server = _table(document, "server")
        calls = _table(document, "calls")
        module = _table(document, "module")
        network = _table(document, "network")
        logging_config = _table(document, "logging")
        security = _table(document, "security")
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
        host = env.get("QDC507_HOST", server.get("host", "127.0.0.1"))
        if not isinstance(host, str) or not host.strip():
            raise ConfigurationError("server.host must be a non-empty string")
        frontend = env.get(
            "QDC507_INCOMING_CALL_FRONTEND",
            calls.get("incoming_frontend", "app"),
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
            allow_service_restart=_boolean(env.get("QDC507_ALLOW_SERVICE_RESTART", server.get("allow_service_restart", False)), "server.allow_service_restart"),
            systemd_unit=env.get("QDC507_SYSTEMD_UNIT", server.get("systemd_unit", "djisimhub.service")),
            public_base_url=_optional_string(env.get("QDC507_PUBLIC_BASE_URL", server.get("public_base_url")), "server.public_base_url"),
            apns_enabled=_boolean(env.get("QDC507_APNS_ENABLED", apns.get("enabled", False)), "apns.enabled"),
            apns_sandbox=_boolean(env.get("QDC507_APNS_SANDBOX", apns.get("sandbox", True)), "apns.sandbox"),
            apns_key_path=_optional_path(env.get("QDC507_APNS_KEY_PATH", apns.get("key_path")), base_dir, "apns.key_path"),
            apns_key_id=_optional_string(env.get("QDC507_APNS_KEY_ID", apns.get("key_id")), "apns.key_id"),
            apns_team_id=_optional_string(env.get("QDC507_APNS_TEAM_ID", apns.get("team_id")), "apns.team_id"),
            apns_bundle_id=_optional_string(env.get("QDC507_APNS_BUNDLE_ID", apns.get("bundle_id")), "apns.bundle_id"),
            data_dir=data_dir,
            lock_path=lock_path,
            web_enabled=_boolean(
                env.get("QDC507_SERVER_ENABLED", server.get("enabled", True)),
                "server.enabled",
            ),
            host=host.strip(),
            port=port,
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
            network_apn=env.get("QDC507_NETWORK_APN", network.get("apn")),
            network_pdp_type=env.get("QDC507_NETWORK_PDP_TYPE", network.get("pdp_type", "IP")),
        )
