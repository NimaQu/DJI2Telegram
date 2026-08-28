import asyncio
import json

from qdc507_gateway.events import EventBus
from qdc507_gateway.models import GatewayEvent
from qdc507_gateway.storage.database import Database


def test_event_bus_persists_gateway_events_in_order():
    async def run():
        database = Database(":memory:")

        async def persist(event):
            database.insert_event(
                event.type,
                json.dumps(event.payload, ensure_ascii=False),
                event.timestamp.isoformat(),
            )

        events = EventBus(persist=persist)
        await events.publish(GatewayEvent("telegram.connected", {"account_user_id": 42}))
        await events.publish(GatewayEvent("audio.state", {"running": False}))
        rows = database.connection.execute(
            "SELECT event_type, payload FROM module_events ORDER BY id"
        ).fetchall()

        assert [row["event_type"] for row in rows] == ["telegram.connected", "audio.state"]
        assert json.loads(rows[0]["payload"])["account_user_id"] == 42

    asyncio.run(run())
