import json

import pytest

from qdc507_gateway.cli import main
from qdc507_gateway.config import ConfigurationError, Settings
from qdc507_gateway.storage.database import Database


def test_toml_paths_are_relative_to_configuration_file(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
[app]
data_dir = "state"
lock_path = "state/device.lock"

[server]
enabled = false
host = "192.168.88.245"
port = 8787

[calls]
incoming_frontend = "auto"

[logging]
level = "DEBUG"

[security]
auth_max_failures = 7
auth_failure_window_seconds = 120
auth_block_seconds = 600
""",
        encoding="utf-8",
    )

    settings = Settings.load(config)
    assert settings.config_path == config.resolve()
    assert settings.data_dir == (tmp_path / "state").resolve()
    assert settings.lock_path == (tmp_path / "state/device.lock").resolve()
    assert settings.web_enabled is False
    assert settings.host == "192.168.88.245"
    assert settings.port == 8787
    assert settings.incoming_call_frontend == "auto"
    assert settings.log_level == "DEBUG"
    assert settings.auth_max_failures == 7
    assert settings.auth_failure_window_seconds == 120
    assert settings.auth_block_seconds == 600


def test_environment_cannot_override_file_or_defaults(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("[server]\nhost='127.0.0.1'\nport=8787\n")
    monkeypatch.chdir(tmp_path)
    expected = Settings.load()
    # Cover every former override, including config selection and absent fields.
    for name in (
        "CONFIG", "DATA_DIR", "LOCK_PATH", "HOST", "PORT", "SERVER_ENABLED",
        "ALLOW_SERVICE_RESTART", "SYSTEMD_UNIT", "PUBLIC_BASE_URL", "LOG_LEVEL",
        "INCOMING_CALL_FRONTEND", "MODULE_VOICE_MANIFEST", "MODULE_VOICE_RESOURCE_DIR",
        "NETWORK_APN", "NETWORK_PDP_TYPE", "AUTH_MAX_FAILURES",
        "AUTH_FAILURE_WINDOW_SECONDS", "AUTH_BLOCK_SECONDS", "APNS_ENABLED",
        "APNS_SANDBOX", "APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID",
    ):
        monkeypatch.setenv("QDC507_" + name, "invalid-environment-value")
    assert Settings.load() == expected
    assert Settings.load(config) == expected


def test_missing_default_config_is_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QDC507_CONFIG", str(tmp_path / "other.toml"))
    (tmp_path / "other.toml").write_text("[server]\nport=9000\n")
    with pytest.raises(ConfigurationError, match="configuration file not found"):
        Settings.load()


def test_disabled_server_rejects_web_only_incoming_call_frontend(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[server]\nenabled=false\n[calls]\nincoming_frontend='web'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="server.enabled"):
        Settings.load(config)


def test_invalid_log_level_and_auth_limits_are_rejected(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[logging]\nlevel='verbose'\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="logging.level"):
        Settings.load(config)
    config.write_text(
        "[security]\nauth_max_failures=0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="auth_max_failures"):
        Settings.load(config)


def test_token_commands_replace_and_delete_the_single_token(tmp_path, capsys, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        """
[app]
data_dir = "data"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["token"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert set(first) == {"token", "replaced_existing"}
    assert first["replaced_existing"] is False

    assert main(["token"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["replaced_existing"] is True
    assert second["token"] != first["token"]

    database = Database(tmp_path / "data/gateway.sqlite3")
    row = dict(database.token())
    database.close()
    assert first["token"] not in row["token_hash"]
    assert second["token"] not in row["token_hash"]

    assert main(["token-delete"]) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": True}
    database = Database(tmp_path / "data/gateway.sqlite3")
    assert database.token() is None
    database.close()


def test_usb_owner_ignores_environment_lock_path(tmp_path, monkeypatch):
    from qdc507_gateway.usb.owner import DeviceOwnerLock
    monkeypatch.setenv("QDC507_LOCK_PATH", str(tmp_path / "environment.lock"))
    assert DeviceOwnerLock().path == Settings().lock_path
    explicit = tmp_path / "configured.lock"
    assert DeviceOwnerLock(explicit).path == explicit
