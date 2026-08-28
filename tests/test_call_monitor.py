from __future__ import annotations

import asyncio

from qdc507_gateway.modem.call_monitor import monitor_cellular_call_status
from qdc507_gateway.models import CallState
from qdc507_gateway.telegram.calls import CallCoordinator


def test_call_status_monitor_marks_answered_call_active_and_stops_polling():
    async def run():
        coordinator = CallCoordinator()
        record = await coordinator.start_outbound(
            "+16479178964",
            None,
            frontend="web",
            initial_state=CallState.waiting_cellular,
        )
        statuses = iter((
            {"state": "dialing", "source": "clcc", "voice_calls": [{"status_code": 3}]},
            {"state": "active", "source": "clcc", "voice_calls": [{"status_code": 0}]},
        ))
        reads = []
        events = []

        async def read_status():
            value = next(statuses)
            reads.append(value["state"])
            return value

        async def connected():
            await coordinator.transition(CallState.active)

        async def publish(event):
            events.append(event)

        task = asyncio.create_task(monitor_cellular_call_status(
            coordinator.current,
            read_status,
            connected,
            publish,
            poll_seconds=0.001,
        ))
        try:
            for _ in range(100):
                if (await coordinator.current()).state == CallState.active:
                    break
                await asyncio.sleep(0.001)
            assert (await coordinator.current()).state == CallState.active
            await asyncio.sleep(0.005)
            assert reads == ["dialing", "active"]
            assert [event.payload["state"] for event in events] == ["dialing", "active"]
            assert events[-1].payload["call_id"] == record.id
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
