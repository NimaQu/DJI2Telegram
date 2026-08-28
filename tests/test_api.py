from __future__ import annotations

from fastapi.testclient import TestClient

from qdc507_gateway.api.app import create_app, sse_event_stream
from qdc507_gateway.events import EventBus
from qdc507_gateway.models import GatewayEvent
from qdc507_gateway.security import AuthFailureLimiter, hash_token
from qdc507_gateway.storage.database import Database


def _client():
    database = Database(":memory:")
    token = "offline-test-token"
    database.save_token("test", hash_token(token), "2026-01-01T00:00:00Z")
    state = {"status": {"service": "test", "module_state": "disconnected"}}
    async def at(command, timeout_ms):
        return {"command": command, "timeout_ms": timeout_ms}

    state["at"] = at
    app = create_app(database, EventBus(), state)
    return TestClient(app), token


def test_any_valid_token_has_full_api_access():
    client, token = _client()

    assert client.get("/api/v1/status").status_code == 401
    assert client.get("/api/v1/status", headers={"Authorization": "Bearer " + token}).status_code == 200
    assert client.get(
        "/api/v1/sms", headers={"Authorization": "Bearer " + token}
    ).status_code == 200


def test_sse_keepalive_does_not_close_event_subscription():
    async def run():
        events = EventBus()
        stream = sse_event_stream(events, keepalive_seconds=0.001)
        assert "stream connected" in await anext(stream)
        assert "keep-alive" in await anext(stream)

        received = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await events.publish(GatewayEvent("module.connected", {"found": True}))
        message = await asyncio.wait_for(received, timeout=1)
        assert message.startswith("event: module.connected\n")
        assert '"found": true' in message
        await stream.aclose()

    import asyncio

    asyncio.run(run())


def test_status_can_include_live_transport_state():
    database = Database(":memory:")
    token = "status-token"
    database.save_token("status", hash_token(token), "2026-01-01T00:00:00Z")
    state = {
        "status": {"service": "test", "module_state": "connected"},
        "get_status": lambda: {
            "service": "test",
            "module_state": "connected",
            "telegram_state": "disabled",
            "audio": {"bridge": {"running": False}},
        },
    }
    client = TestClient(create_app(database, EventBus(), state))
    response = client.get(
        "/api/v1/status", headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 200
    assert response.json()["telegram_state"] == "disabled"
    assert response.json()["audio"]["bridge"]["running"] is False


def test_module_status_can_be_refreshed_explicitly():
    database = Database(":memory:")
    token = "module-status-token"
    database.save_token("status", hash_token(token), "2026-01-01T00:00:00Z")

    async def refresh():
        return {
            "connected": True,
            "phone_number": "14312764514",
            "operator": {"name": "Lucky", "radio": "LTE"},
            "signal": {"dbm": -79, "bars": 5},
        }

    state = {
        "module": {"connected": True, "phone_number": None},
        "refresh_module_status": refresh,
    }
    client = TestClient(create_app(database, EventBus(), state))
    headers = {"Authorization": "Bearer " + token}
    assert client.get("/api/v1/module", headers=headers).json()["phone_number"] is None
    refreshed = client.get("/api/v1/module?refresh=true", headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.json()["phone_number"] == "14312764514"
    assert refreshed.json()["operator"]["name"] == "Lucky"


def test_repeated_token_failures_are_temporarily_blocked():
    database = Database(":memory:")
    token = "valid-token"
    database.save_token("valid", hash_token(token), "2026-01-01T00:00:00Z")
    now = [100.0]
    limiter = AuthFailureLimiter(
        max_failures=3,
        window_seconds=60,
        block_seconds=120,
        clock=lambda: now[0],
    )
    client = TestClient(create_app(
        database,
        EventBus(),
        {"status": {"service": "test"}, "auth_limiter": limiter},
    ))
    invalid = {"Authorization": "Bearer wrong"}
    assert client.get("/api/v1/status", headers=invalid).status_code == 401
    assert client.get("/api/v1/status", headers=invalid).status_code == 401
    blocked = client.get("/api/v1/status", headers=invalid)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "120"
    assert client.get(
        "/api/v1/status",
        headers={"Authorization": "Bearer " + token},
    ).status_code == 429
    now[0] += 121
    assert client.get(
        "/api/v1/status",
        headers={"Authorization": "Bearer " + token},
    ).status_code == 200


def test_dangerous_at_requires_confirmation_and_qadbkey_is_dedicated():
    client, token = _client()
    headers = {"Authorization": "Bearer " + token}

    response = client.post("/api/v1/module/at", json={"command": 'AT+QCFG="usb",0'}, headers=headers)
    assert response.status_code == 409

    response = client.post(
        "/api/v1/module/at",
        json={"command": 'AT+QCFG="usb",0', "confirm_persistent": True},
        headers=headers,
    )
    assert response.status_code == 200

    response = client.post(
        "/api/v1/module/at",
        json={"command": "AT+QADBKEY=12345678", "confirm_persistent": True},
        headers=headers,
    )
    assert response.status_code == 403


def test_token_database_stores_only_hash():
    database = Database(":memory:")
    database.save_token("id", hash_token("one-time-token"), "2026-01-01T00:00:00Z")
    row = dict(database.tokens()[0])
    assert row["token_id"] == "id"
    assert "one-time-token" not in row["token_hash"]
    assert "one-time-token" not in str(row)


def test_revoked_token_is_not_accepted():
    database = Database(":memory:")
    token = "revocable-token"
    database.save_token("revocable", hash_token(token), "2026-01-01T00:00:00Z")
    assert database.revoke_token("revocable", "2026-01-02T00:00:00Z")
    assert database.tokens() == []
    assert not database.revoke_token("revocable", "2026-01-03T00:00:00Z")


def test_at_input_bounds_are_rejected():
    client, token = _client()
    headers = {"Authorization": "Bearer " + token}
    assert client.post(
        "/api/v1/module/at",
        json={"command": "AT+CSQ", "timeout_ms": 99},
        headers=headers,
    ).status_code == 422
    assert client.post(
        "/api/v1/module/at",
        json={"command": "AT+CSQ\u0000", "timeout_ms": 3000},
        headers=headers,
    ).status_code == 422


def test_live_operation_failure_is_not_reported_as_success():
    database = Database(":memory:")
    token = "offline-admin-token"
    database.save_token("admin", hash_token(token), "2026-01-01T00:00:00Z")

    async def unavailable(*args):
        raise RuntimeError("QDC507 module operation unavailable: LiveUSBError")

    app = create_app(database, EventBus(), {"at": unavailable, "send_sms": unavailable})
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + token}
    assert client.post(
        "/api/v1/module/at", json={"command": "AT+CSQ"}, headers=headers
    ).status_code == 503
    assert client.post(
        "/api/v1/sms/send", json={"to": "+12045550100", "text": "hi"}, headers=headers
    ).status_code == 503
