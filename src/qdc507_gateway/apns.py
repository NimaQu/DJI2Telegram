"""Durable SMS delivery via the APNs HTTP/2 provider API."""
from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import random
import time
from email.utils import parsedate_to_datetime

import httpx
import jwt

logger = logging.getLogger(__name__)


def notification_payload(job) -> bytes:
    body = job["body"]
    payload = {
        "aps": {"alert": {"title": job["sender"][:256], "body": body}, "sound": "default"},
        "type": "sms.received", "sms_id": job["sms_id"],
        "timestamp": job["timestamp"], "body_truncated": False,
    }

    def encode():
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    encoded = encode()
    if len(encoded) <= 4096:
        return encoded
    payload["body_truncated"] = True
    low, high = 0, len(body)
    while low < high:
        mid = (low + high + 1) // 2
        payload["aps"]["alert"]["body"] = body[:mid] + "…"
        if len(encode()) <= 4096:
            low = mid
        else:
            high = mid - 1
    payload["aps"]["alert"]["body"] = body[:low] + "…"
    return encode()


class APNsService:
    def __init__(self, settings, database, *, client=None, clock=time.time):
        self.settings = settings
        self.database = database
        self.environment = "sandbox" if settings.apns_sandbox else "production"
        self.bundle_id = settings.apns_bundle_id or ""
        self.clock = clock
        self.client = client
        self.task = None
        self.last_error = None
        self.paused = False
        self._jwt = None
        self._jwt_at = 0
        self._key = None
        database.reconcile_push_scope(self.environment, self.bundle_id)
        database.push_scope = (self.environment, self.bundle_id) if settings.apns_enabled else None

    def status(self):
        return {
            "enabled": self.settings.apns_enabled, "environment": self.environment,
            "paused": self.paused, "last_error": self.last_error,
            **self.database.push_counts(self.environment, self.bundle_id),
        }

    def register(self, installation_id, device_token):
        self.database.register_push_device(installation_id, device_token, self.environment, self.bundle_id)
        return {"installation_id": installation_id, "environment": self.environment,
                "enabled": self.settings.apns_enabled}

    async def start(self):
        if not self.settings.apns_enabled:
            return
        self._key = self.settings.apns_key_path.read_bytes()
        if self.client is None:
            self.client = httpx.AsyncClient(http2=True, timeout=15.0, follow_redirects=False)
        self.task = asyncio.create_task(self._run(), name="apns-worker")

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.client is not None:
            await self.client.aclose()

    def provider_token(self):
        now = self.clock()
        if self._jwt is None or not 0 <= now - self._jwt_at < 3000:
            self._jwt = jwt.encode(
                {"iss": self.settings.apns_team_id, "iat": int(now)},
                self._key or self.settings.apns_key_path.read_bytes(), algorithm="ES256",
                headers={"kid": self.settings.apns_key_id},
            )
            self._jwt_at = now
        return self._jwt

    def error(self, reason):
        self.last_error = reason
        logger.warning("APNs delivery: %s", reason)

    def retry(self, job, response=None):
        now = self.clock()
        delay = min(3600, 2 ** min(job["attempts"] + 1, 12)) + random.uniform(0, 1)
        if response is not None:
            value = response.headers.get("retry-after")
            if value:
                try:
                    requested = float(value)
                except ValueError:
                    try:
                        requested = parsedate_to_datetime(value).timestamp() - now
                    except (ValueError, TypeError, OverflowError):
                        requested = 0
                delay = max(delay, requested)
        self.database.retry_push_job(job["id"], now + delay)

    async def send_one(self):
        if not self.settings.apns_enabled:
            return False
        job = self.database.next_push_job(self.environment, self.bundle_id, self.clock())
        if job is None or self.paused:
            return False
        host = "api.sandbox.push.apple.com" if self.settings.apns_sandbox else "api.push.apple.com"
        try:
            response = await self.client.post(
                f"https://{host}/3/device/{job['device_token']}",
                headers={"authorization": "bearer " + self.provider_token(),
                         "apns-topic": self.bundle_id, "apns-push-type": "alert",
                         "apns-priority": "10", "apns-id": job["id"],
                         "apns-expiration": str(int(job["created_at"] + 86400)),
                         "apns-collapse-id": hashlib.sha256(job["sms_id"].encode()).hexdigest(),
                         "content-type": "application/json"},
                content=notification_payload(job),
            )
        except httpx.TransportError:
            self.error("transport_error")
            self.retry(job)
            return True
        if response.status_code == 200:
            self.database.finish_push_job(job["id"])
            self.last_error = None
            return True
        try:
            result = response.json()
            if not isinstance(result, dict):
                result = {}
        except ValueError:
            result = {}
        reason = result.get("reason")
        # Log only known constants, never remote response text or request URLs.
        known = {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered", "ExpiredProviderToken",
                 "InvalidProviderToken", "TooManyProviderTokenUpdates", "MissingProviderToken",
                 "BadTopic", "TopicDisallowed", "PayloadTooLarge", "TooManyRequests",
                 "InternalServerError", "ServiceUnavailable", "Shutdown"}
        self.error(reason if isinstance(reason, str) and reason in known else f"http_{response.status_code}")
        if response.status_code == 410:
            timestamp = result.get("timestamp")
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
                self.database.invalidate_push_device(job, timestamp / 1000)
            else:
                self.database.finish_push_job(job["id"])
        elif reason in ("BadDeviceToken", "DeviceTokenNotForTopic"):
            self.database.invalidate_push_device(job)
        elif response.status_code == 403 or reason in ("BadTopic", "TopicDisallowed", "MissingProviderToken", "TooManyProviderTokenUpdates"):
            self.paused = True  # Correct configuration and restart to resume queued jobs.
        elif response.status_code == 429 or response.status_code >= 500:
            self.retry(job, response)
        else:
            self.database.finish_push_job(job["id"])
        return True

    async def _run(self):
        while True:
            try:
                worked = await self.send_one()
            except Exception:
                self.error("worker_error")
                worked = False
            await asyncio.sleep(0.01 if worked else 1.0)
