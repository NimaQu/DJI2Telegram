from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple


@dataclass(frozen=True)
class CompatibilityReport:
    client_name: str
    passed: bool
    missing: Tuple[str, ...]


def probe_kurigram_client(client: Any) -> CompatibilityReport:
    required = ("send_message", "resolve_peer")
    missing = tuple(name for name in required if not callable(getattr(client, name, None)))
    return CompatibilityReport("kurigram", not missing, missing)


def probe_kurigram_raw_phone_types() -> CompatibilityReport:
    """Check the raw User API constructors used by private-call signaling."""
    try:
        from pyrogram.raw.functions import phone
    except (ImportError, AttributeError) as exc:
        return CompatibilityReport("kurigram-raw-phone", False, (type(exc).__name__,))
    required = (
        "RequestCall", "AcceptCall", "ConfirmCall", "SendSignalingData", "DiscardCall",
    )
    missing = tuple(name for name in required if not callable(getattr(phone, name, None)))
    return CompatibilityReport("kurigram-raw-phone", not missing, missing)


def probe_pytgcalls_bridge(bridge: Any) -> CompatibilityReport:
    required = ("request_call", "accept_call", "confirm_call", "send_signaling", "discard_call")
    missing = tuple(name for name in required if not callable(getattr(bridge, name, None)))
    return CompatibilityReport("kurigram-pytgcalls-bridge", not missing, missing)


def probe_kurigram_pytgcalls_runtime(client: Any) -> CompatibilityReport:
    """Instantiate the optional bridge without starting Telegram or NTgCalls."""
    try:
        from .kurigram import KurigramPyTgCallsBridge
        bridge = KurigramPyTgCallsBridge(client)
    except Exception as exc:
        return CompatibilityReport("kurigram-pytgcalls-runtime", False, (type(exc).__name__,))
    required = (
        "start", "stop", "play", "record", "leave_call", "request_call", "accept_call",
        "confirm_call", "send_signaling", "discard_call", "external_audio_stream",
        "external_record_stream",
        "attach_pcm", "detach_pcm", "start_private_call", "stop_private_call",
    )
    missing = [name for name in required if not callable(getattr(bridge, name, None))]
    try:
        bridge.external_audio_stream()
    except Exception as exc:
        missing.append("external_audio_stream:" + type(exc).__name__)
    try:
        bridge.external_record_stream()
    except Exception as exc:
        missing.append("external_record_stream:" + type(exc).__name__)
    raw = getattr(bridge, "mtproto", None)
    if raw is None:
        missing.append("mtproto")
    else:
        for name in ("request_call", "accept_call", "confirm_call", "send_signaling", "discard_call"):
            if not callable(getattr(raw, name, None)):
                missing.append("mtproto." + name)
    return CompatibilityReport("kurigram-pytgcalls-runtime", not missing, tuple(missing))
