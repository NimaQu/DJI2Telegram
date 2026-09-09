"""Short-lived VoIP invitations. Never use the SMS outbox/ACK mechanism."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx

from qdc507_gateway.models import CallDirection, CallState

logger = logging.getLogger(__name__)


class VoIPPushService:
    def __init__(self, apns, current_call):
        self.apns = apns
        self.current_call = current_call
        self.tasks = {}
        self.invited = set()
        self.paused = False
        self.last_error = None

    def status(self):
        return {"paused": self.paused, "last_error": self.last_error,
                "active_devices": len(self.apns.database.voip_devices(self.apns.environment, self.apns.bundle_id))}

    def on_state(self, record):
        ringing = (record.frontend == "app" and record.direction == CallDirection.inbound_cellular
                   and record.state == CallState.ringing_cellular and record.owner_installation_id is None)
        if not ringing:
            task = self.tasks.get(record.id)
            if task is not None:
                task.cancel()
            self.invited.discard(record.id)
            return
        if not self.apns.settings.apns_enabled or self.paused or record.id in self.invited:
            return
        self.invited.add(record.id)
        task = asyncio.create_task(self._fanout(record.id), name="voip-invitation")
        self.tasks[record.id] = task
        def completed(finished):
            self.tasks.pop(record.id, None)
            if not finished.cancelled() and finished.exception() is not None:
                self.last_error = "voip_error"
                logger.warning("VoIP invitation task failed")
        task.add_done_callback(completed)

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.invited.clear()

    async def _ringing(self, call_id):
        record = await self.current_call()
        if (record is None or record.id != call_id or record.frontend != "app"
                or record.state != CallState.ringing_cellular or record.owner_installation_id
                or record.expires_at is None or record.expires_at.timestamp() <= time.time()):
            return None
        return record

    async def _fanout(self, call_id):
        devices = self.apns.database.voip_devices(self.apns.environment, self.apns.bundle_id)
        semaphore = asyncio.Semaphore(8)
        async def deliver(device):
            async with semaphore:
                await self._send(call_id, device)
        await asyncio.gather(*(deliver(device) for device in devices))

    async def _send(self, call_id, device):
        notification_id = str(uuid.uuid4())
        for attempt in range(3):
            record = await self._ringing(call_id)
            if record is None or self.paused:
                return
            # Token deletion/rotation while waiting to send must take effect.
            current_devices = self.apns.database.voip_devices(self.apns.environment, self.apns.bundle_id)
            if not any(d["installation_id"] == device["installation_id"] and d["version"] == device["version"] for d in current_devices):
                return
            host = "api.sandbox.push.apple.com" if self.apns.settings.apns_sandbox else "api.push.apple.com"
            try:
                response = await self.apns.client.post(
                    f"https://{host}/3/device/{device['device_token']}", timeout=5.0,
                    headers={"authorization": "bearer " + self.apns.provider_token(),
                             "apns-topic": self.apns.bundle_id + ".voip", "apns-push-type": "voip",
                             "apns-priority": "10", "apns-expiration": "0", "apns-id": notification_id,
                             "content-type": "application/json"},
                    content=json.dumps({"aps": {}, "type": "call.incoming", "call_id": record.id,
                                        "caller": record.cellular_number, "expires_at": record.expires_at.isoformat(),
                                        "timestamp": record.started_at.isoformat()}).encode(),
                )
                if response.status_code == 200:
                    self.last_error = None
                    return
                try:
                    result = response.json()
                    if not isinstance(result, dict):
                        result = {}
                except ValueError:
                    result = {}
                reason = result.get("reason")
                self.last_error = f"http_{response.status_code}"
                if response.status_code == 410:
                    timestamp = result.get("timestamp")
                    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
                        self.apns.database.invalidate_voip_device(device, timestamp / 1000)
                    return
                if reason in ("BadDeviceToken", "DeviceTokenNotForTopic"):
                    self.apns.database.invalidate_voip_device(device)
                    return
                if response.status_code == 403 or reason in ("BadTopic", "TopicDisallowed", "TooManyProviderTokenUpdates"):
                    self.paused = True
                    return
                if response.status_code != 429 and response.status_code < 500:
                    return
                # A throttled invitation is dropped rather than ringing much later.
                if response.headers.get("retry-after"):
                    return
            except httpx.TransportError:
                self.last_error = "transport_error"
            except Exception:
                self.last_error = "voip_error"
                logger.warning("VoIP invitation failed")
                return
            if attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
