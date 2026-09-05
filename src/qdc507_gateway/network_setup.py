from __future__ import annotations

import asyncio
import csv
import json
import os
from pathlib import Path
import re
import tempfile
import time

from qdc507_gateway.config import Settings


class NetworkSetupError(RuntimeError):
    pass


async def _command(service, command: str, timeout_ms: int = 5000):
    result = await service.at(command, timeout_ms=timeout_ms)
    if not (result.get("ok") or (result.get("operation") == "cfun" and "changed" in result)):
        raise NetworkSetupError(f"{command.split('=')[0]} was not accepted")
    return result


def parse_contexts(lines) -> dict[int, dict]:
    result = {}
    for line in lines:
        if not str(line).startswith("+CGDCONT:"):
            continue
        try:
            fields = next(csv.reader([line.split(":", 1)[1].strip()], skipinitialspace=True, strict=True))
            cid = int(fields[0])
            if len(fields) < 3 or cid in result:
                raise ValueError("invalid context")
        except (ValueError, IndexError, csv.Error) as exc:
            raise NetworkSetupError("invalid CGDCONT readback") from exc
        result[cid] = {"pdp_type": fields[1], "apn": fields[2]}
    return result


async def _contexts(service):
    return parse_contexts((await _command(service, "AT+CGDCONT?"))["lines"])


async def _registration(service):
    response = await _command(service, "AT+CEREG?")
    for line in response.get("lines", []):
        match = re.match(r"\+CEREG:\s*\d+\s*,\s*(\d+)(?:\s*,|\s*$)", line)
        if match:
            status = int(match[1])
            return {"status": status, "registered": status in (1, 5), "denied": status == 3}
    raise NetworkSetupError("invalid CEREG readback")


async def _numeric(service, command, prefix):
    response = await _command(service, command)
    for line in response.get("lines", []):
        match = re.match(re.escape(prefix) + r"\s*(\d+)(?:\s*,|\s*$)", line)
        if match:
            return int(match[1])
    raise NetworkSetupError(f"invalid {prefix} readback")


async def setup_network(service, settings: Settings, *, backup_dir: Path, progress=None, wait_seconds: float = 60):
    """Apply only CID 1 and automatic selection, then report actual LTE registration.

    APN None is read-only; empty APN explicitly requests subscription defaults.
    No fallback guesses, SMS, calls, PDP activation or host networking are used.
    """
    notify = progress or (lambda message: None)
    before = await _contexts(service)
    registration = await _registration(service)
    mode = await _numeric(service, "AT+COPS?", "+COPS:")
    result = {"mode": "keep" if settings.network_apn is None else ("subscription" if settings.network_apn == "" else "manual"),
              "changed": False, "reattached": False, "before": before, "after": before,
              "operator_mode": mode, "registration": registration, "registered": registration["registered"], "backup_path": None}
    if settings.network_apn is None:
        return result
    target = {"pdp_type": settings.network_pdp_type, "apn": settings.network_apn}
    current = before.get(1)
    if current and current["apn"].lower() in {"ims", "sos"}:
        raise NetworkSetupError("CID 1 is reserved for IMS/SOS; refusing to replace it")
    changed = current != target
    if not changed and mode == 0 and registration["registered"]:
        return result
    if await _numeric(service, "AT+CFUN?", "+CFUN:") != 1:
        raise NetworkSetupError("radio is not in CFUN=1; restore its intended state before network-setup")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, filename = tempfile.mkstemp(prefix="network-before-", suffix=".json", dir=backup_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"contexts": before, "operator_mode": mode, "registration": registration}, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    result["backup_path"] = filename
    notify(f"Saved network settings: {filename}")
    notify("Temporarily disabling radio to apply CID 1 APN")
    try:
        # Resume even if CFUN=0 times out after the module accepted it.
        try:
            await _command(service, "AT+CFUN=0", timeout_ms=15000)
            if changed:
                await _command(service, f'AT+CGDCONT=1,"{settings.network_pdp_type}","{settings.network_apn}"')
            after = await _contexts(service)
            if after.get(1) != target:
                raise NetworkSetupError("CID 1 APN readback did not match the requested configuration")
            if {k: v for k, v in after.items() if k != 1} != {k: v for k, v in before.items() if k != 1}:
                raise NetworkSetupError("non-default PDP contexts changed unexpectedly")
            result["after"] = after
            result["changed"] = changed
        finally:
            await _command(service, "AT+CFUN=1", timeout_ms=15000)
        if mode != 0:
            notify("Restoring automatic operator selection")
            await _command(service, "AT+COPS=0", timeout_ms=180000)
        result["operator_mode"] = await _numeric(service, "AT+COPS?", "+COPS:")
        if result["operator_mode"] != 0:
            raise NetworkSetupError("automatic operator selection did not persist")
        result["reattached"] = True
        notify("Waiting for LTE registration")
        deadline = time.monotonic() + wait_seconds
        last_error = None
        while True:
            try:
                registration = await _registration(service)
                last_error = None
            except NetworkSetupError as exc:
                last_error = str(exc)
            if last_error is None and registration["registered"]:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
        result["registration"] = registration if last_error is None else {"registered": False, "error": last_error}
        result["registered"] = bool(result["registration"]["registered"])
        result["after"] = await _contexts(service)
        return result
    except Exception as exc:
        raise NetworkSetupError(f"{exc}; original network settings saved in {filename}") from exc
