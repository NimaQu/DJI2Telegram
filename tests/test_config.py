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
host = "192.168.88.245"
port = 8787

[calls]
incoming_frontend = "auto"

[telegram]
session = "state/gateway.session"
bot_session = "state/bot.session"
api_id = 123
api_hash = "secret-hash"
bot_token = "123:do-not-print"
user_id = 456
allow_service_restart = true

[logging]
level = "DEBUG"

[security]
auth_max_failures = 7
auth_failure_window_seconds = 120
auth_block_seconds = 600
""",
        encoding="utf-8",
    )

    settings = Settings.load(config, environ={})
    assert settings.config_path == config.resolve()
    assert settings.data_dir == (tmp_path / "state").resolve()
    assert settings.lock_path == (tmp_path / "state/device.lock").resolve()
    assert settings.telegram_session == (tmp_path / "state/gateway.session").resolve()
    assert settings.telegram_bot_session == (tmp_path / "state/bot.session").resolve()
    assert settings.telegram_bot_token == "123:do-not-print"
    assert settings.host == "192.168.88.245"
    assert settings.port == 8787
    assert settings.telegram_user_id == 456
    assert settings.telegram_allow_service_restart is True
    assert settings.incoming_call_frontend == "auto"
    assert settings.log_level == "DEBUG"
    assert settings.auth_max_failures == 7
    assert settings.auth_failure_window_seconds == 120
    assert settings.auth_block_seconds == 600


def test_environment_can_override_toml_during_debugging(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[server]\nhost='127.0.0.1'\nport=8787\n", encoding="utf-8")
    settings = Settings.load(
        config,
        environ={
            "QDC507_HOST": "0.0.0.0",
            "QDC507_PORT": "9000",
            "QDC507_TELEGRAM_USER_ID": "789",
        },
    )
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.telegram_user_id == 789
    assert settings.incoming_call_frontend == "telegram"


def test_legacy_multiple_user_telegram_configuration_is_rejected(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "[telegram]\npersonal_user_id=456\nadmin_user_ids=[789]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="telegram.user_id"):
        Settings.load(config, environ={})


def test_invalid_log_level_and_auth_limits_are_rejected(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[logging]\nlevel='verbose'\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="logging.level"):
        Settings.load(config, environ={})
    config.write_text(
        "[security]\nauth_max_failures=0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="auth_max_failures"):
        Settings.load(config, environ={})


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


def test_config_check_never_prints_telegram_api_hash(tmp_path, capsys, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        """
[telegram]
api_id = 123
api_hash = "do-not-print-this"
bot_token = "123:also-do-not-print-this"
user_id = 456
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["config-check"]) == 0
    output = capsys.readouterr().out
    assert "do-not-print-this" not in output
    assert "also-do-not-print-this" not in output
    assert json.loads(output)["telegram"]["configured"] is True
    assert json.loads(output)["telegram"]["bot_configured"] is True
