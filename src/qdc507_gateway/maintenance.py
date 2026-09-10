"""Authenticated maintenance operations, independent of notification transports."""

from __future__ import annotations

import asyncio
import logging

from qdc507_gateway.calls.core import CallBridgeError

logger = logging.getLogger(__name__)


class MaintenanceController:
    def __init__(self, settings, module, calls, diagnostic):
        self.settings = settings
        self.module = module
        self.calls = calls
        self.diagnostic = diagnostic
        self.lock = asyncio.Lock()
        self.restart_pending = False
        self.tasks = set()

    def require_available(self):
        if self.restart_pending:
            raise CallBridgeError("service restart is pending")

    async def require_idle(self):
        if await self.calls.current() is not None or self.diagnostic.active:
            raise CallBridgeError("maintenance is unavailable during a call or audio diagnostic")

    async def at(self, command, timeout_ms=3000):
        async with self.lock:
            await self.require_idle()
            if self.restart_pending:
                raise CallBridgeError("service restart is pending")
            return await self.module.at(command, timeout_ms=timeout_ms)

    async def restart_module(self):
        return await self.at("AT+CFUN=1,1", timeout_ms=10000)

    async def restart_service(self):
        if not self.settings.allow_service_restart:
            raise PermissionError("server.allow_service_restart is disabled")
        async with self.lock:
            await self.require_idle()
            if not self.restart_pending:
                self.restart_pending = True
                task = asyncio.create_task(self._restart_later())
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        return {"accepted": True, "target": "service", "unit": self.settings.systemd_unit}

    async def _restart_later(self):
        # Let the accepted response reach the client before systemd stops us.
        await asyncio.sleep(1)
        try:
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "--no-block",
                "restart",
                self.settings.systemd_unit,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await process.wait():
                logger.error("service restart failed")
                self.restart_pending = False
        except OSError:
            logger.exception("systemd service restart could not be started")
            self.restart_pending = False

    async def stop(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
