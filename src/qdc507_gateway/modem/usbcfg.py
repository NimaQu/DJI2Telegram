from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class USBConfiguration:
    vendor_id: int
    product_id: int
    diagnostic: bool
    nmea: bool
    at: bool
    modem: bool
    network: bool
    adb: bool
    audio: bool

    @property
    def is_legacy_uac_target(self) -> bool:
        return self.vendor_id == 0x2C7C and self.product_id == 0x0125 and self.audio and not self.adb

    @property
    def is_full_target(self) -> bool:
        return self.is_full_target_for(0x2C7C, 0x0125)

    def is_full_target_for(self, vendor_id: int, product_id: int) -> bool:
        return (
            self.vendor_id == vendor_id
            and self.product_id == product_id
            and self.diagnostic and self.nmea and self.at and self.modem
            and self.network and self.adb and self.audio
        )

    @property
    def command(self) -> str:
        flags = ",".join("1" if value else "0" for value in (
            self.diagnostic, self.nmea, self.at, self.modem, self.network, self.adb, self.audio
        ))
        return 'AT+QCFG="USBCFG",0x%04X,0x%04X,%s' % (self.vendor_id, self.product_id, flags)


class PersistentConfigError(RuntimeError):
    pass


def is_reenumeration_signal(error: BaseException) -> bool:
    """Return true for a transport result that may mean the USB device detached.

    A QDC507 can accept a persistent command and then tear down the USB
    configuration before libusb returns a normal AT terminal line.  Only
    explicit no-device/detach signals take this path; ordinary I/O failures
    remain failures and are never retried.
    """
    value = getattr(error, "value", None)
    if value in {-4, -19}:  # LIBUSB_ERROR_NO_DEVICE / USBDEVFS device gone
        return True
    description = "%s %s" % (type(error).__name__, error)
    normalized = description.upper().replace("_", " ")
    return any(marker in normalized for marker in (
        "NO DEVICE",
        "NOT CONNECTED",
        "DETACHED",
        "DISCONNECTED",
        "USBERRORNODEVICE",
    ))


@dataclass(frozen=True)
class USBConfigApplyResult:
    changed: bool
    reenumerated: bool
    before: USBConfiguration
    after: USBConfiguration


def apply_usbcfg_once(
    *,
    readback: Callable[[], USBConfiguration],
    write: Callable[[str], Any],
    close_handle: Callable[[], None],
    wait_for_same_device: Callable[[], Any],
    readback_after_reconnect: Callable[[Any], USBConfiguration],
    target: USBConfiguration,
    confirm_persistent: bool = False,
) -> USBConfigApplyResult:
    """Apply USBCFG with one explicit confirmation and no blind retries.

    The callbacks make the detach/re-enumeration boundary explicit. A caller
    must close the old handle before waiting for the same physical device and
    must provide a fresh readback after reconnect. If the modem reports a
    normal error, no reconnect is attempted.
    """
    before = readback()
    if before == target:
        return USBConfigApplyResult(False, False, before, before)
    if not confirm_persistent:
        raise PersistentConfigError("explicit persistent-command confirmation required")
    if not target.is_full_target:
        raise PersistentConfigError("target USBCFG is not the complete QDC507 target")
    try:
        response = write(target.command)
        terminal = str(getattr(response, "terminal", "")).upper()
    except Exception as exc:
        if not is_reenumeration_signal(exc):
            raise
        terminal = "DETACHED"
    if terminal.replace(" ", "_") not in {"OK", "NO_DEVICE", "DETACHED", "NOT_CONNECTED"}:
        raise PersistentConfigError("USBCFG write was not accepted")
    close_handle()
    device = wait_for_same_device()
    after = readback_after_reconnect(device)
    if after not in (before, target):
        raise PersistentConfigError("post-reconnect USBCFG readback is unexpected")
    return USBConfigApplyResult(True, True, before, after)


def _integer(value: str) -> int:
    return int(value.strip().strip('"'), 0)


def parse_usb_configuration(response: str) -> Optional[USBConfiguration]:
    match = re.search(r"\+QCFG:\s*\"usbcfg\"\s*,([^\r\n]+)", response, re.IGNORECASE)
    if not match:
        return None
    fields = [part.strip() for part in match.group(1).split(",")]
    if len(fields) != 9:
        return None
    try:
        values = [_integer(fields[0]), _integer(fields[1])] + [int(x) for x in fields[2:]]
    except ValueError:
        return None
    if len(values) != 9 or any(value not in (0, 1) for value in values[2:]):
        return None
    return USBConfiguration(values[0], values[1], *(bool(value) for value in values[2:]))


def parse_usbcfg_command(command: str) -> Optional[USBConfiguration]:
    """Parse an explicit ``AT+QCFG=\"USBCFG\"`` write without executing it."""
    match = re.fullmatch(r'AT\+QCFG\s*=\s*"USBCFG"\s*,(.+)', command.strip(), re.IGNORECASE)
    if match is None:
        return None
    fields = [part.strip() for part in match.group(1).split(",")]
    if len(fields) != 9:
        return None
    try:
        values = [_integer(fields[0]), _integer(fields[1])] + [int(value) for value in fields[2:]]
    except ValueError:
        return None
    if len(values) != 9 or any(value not in (0, 1) for value in values[2:]):
        return None
    return USBConfiguration(values[0], values[1], *(bool(value) for value in values[2:]))
