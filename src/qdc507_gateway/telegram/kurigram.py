from __future__ import annotations

import importlib
import importlib.metadata
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol

from .commands import CommandError, TelegramResponse


class KurigramRuntimeError(RuntimeError):
    pass


NTGCALLS_AUDIO_SAMPLE_RATE = 8000
NTGCALLS_AUDIO_FRAME_MS = 10
NTGCALLS_AUDIO_FRAME_BYTES = (
    NTGCALLS_AUDIO_SAMPLE_RATE * 2 * NTGCALLS_AUDIO_FRAME_MS // 1000
)


@dataclass
class PrivateCallHandle:
    user_id: int
    task: Any
    disconnect_handler: Any = None


@dataclass(frozen=True)
class PCMCallBinding:
    chat_id: int
    handler: Any
    pump_task: Any


def _installed_telegram_distributions() -> list[str]:
    packages_distributions = getattr(importlib.metadata, "packages_distributions", None)
    if packages_distributions is not None:
        distributions = packages_distributions()
        return [name.lower() for name in distributions.get("pyrogram", [])]
    try:
        importlib.metadata.distribution("kurigram")
    except importlib.metadata.PackageNotFoundError:
        return []
    return ["kurigram"]


def create_kurigram_client(
    session_name: str,
    api_id: int,
    api_hash: str,
    workdir: str | os.PathLike[str],
    **kwargs: Any,
) -> Any:
    """Create Kurigram through its compatible ``pyrogram`` import namespace.

    Kurigram intentionally keeps the Pyrogram import surface. The distribution
    check prevents accidentally running against the abandoned upstream package.
    """
    installed = _installed_telegram_distributions()
    if "kurigram" not in installed:
        raise KurigramRuntimeError(
            "Kurigram is not installed as the provider of the pyrogram namespace"
        )
    if "pyrogram" in installed:
        raise KurigramRuntimeError(
            "the abandoned Pyrogram distribution must not coexist with Kurigram"
        )
    try:
        pyrogram = importlib.import_module("pyrogram")
        client_type = pyrogram.Client
    except (ImportError, AttributeError) as exc:
        raise KurigramRuntimeError("Kurigram pyrogram-compatible Client is unavailable") from exc
    session_dir = Path(workdir)
    session_dir.mkdir(parents=True, exist_ok=True)
    session_dir.chmod(0o700)
    return client_type(
        session_name,
        api_id=api_id,
        api_hash=api_hash,
        workdir=str(session_dir),
        **kwargs,
    )


def ensure_session_permissions(session_path: str | os.PathLike[str]) -> None:
    path = Path(session_path)
    if path.exists():
        path.chmod(0o600)


class TelegramMessageClient(Protocol):
    async def send_message(self, chat_id: Any, text: str) -> Any:
        ...

    async def resolve_peer(self, peer: Any) -> Any:
        ...


class TelegramClientProtocol(TelegramMessageClient, Protocol):
    """Backward-compatible name for the message-client contract."""


class TelegramCallSignaling(Protocol):
    async def request_call(self, request: Any) -> Any:
        ...

    async def accept_call(self, request: Any) -> Any:
        ...

    async def confirm_call(self, request: Any) -> Any:
        ...

    async def send_signaling(self, request: Any) -> Any:
        ...

    async def discard_call(self, request: Any) -> Any:
        ...


class KurigramMessageClient:
    """Adapter around an already-created Kurigram User API client.

    Kurigram is installed as the ``kurigram`` distribution but exposes the
    compatible ``pyrogram`` import namespace.
    """

    def __init__(self, client: TelegramMessageClient):
        self.client = client
        self._command_handlers: list[Any] = []

    async def send_message(self, chat_id: Any, text: str) -> Any:
        return await self.client.send_message(chat_id, text)

    async def resolve_peer(self, peer: Any) -> Any:
        return await self.client.resolve_peer(peer)

    def install_command_router(self, router: Any) -> Any:
        """Register a compatible message handler; router owns authorization."""
        self.remove_command_router()
        try:
            from pyrogram.handlers import CallbackQueryHandler, MessageHandler
            from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        except (ImportError, AttributeError) as exc:
            raise KurigramRuntimeError("Kurigram message handlers are unavailable") from exc

        def reply_markup(response: TelegramResponse):
            if not response.buttons:
                return None
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(button.text, callback_data=button.callback_data)
                    for button in row
                ]
                for row in response.buttons
            ])

        async def message_handler(_, message):
            sender = getattr(getattr(message, "from_user", None), "id", None)
            text = getattr(message, "text", None)
            if sender is None or not text:
                return
            sensitive = router.is_sensitive_input(text)

            async def remove_sensitive_message() -> None:
                if not sensitive:
                    return
                deleter = getattr(message, "delete", None)
                if callable(deleter):
                    try:
                        await deleter()
                    except Exception:
                        pass

            try:
                result = await router.dispatch(int(sender), text)
            except PermissionError:
                await remove_sensitive_message()
                return
            except CommandError as exc:
                await message.reply_text(f"操作失败: {exc}")
                await remove_sensitive_message()
                return
            except Exception as exc:
                await message.reply_text(f"操作失败，请查看网关日志 ({type(exc).__name__})")
                await remove_sensitive_message()
                return
            if result is not None:
                await message.reply_text(result.text, reply_markup=reply_markup(result))
            await remove_sensitive_message()

        async def callback_handler(_, query):
            sender = getattr(getattr(query, "from_user", None), "id", None)
            data = getattr(query, "data", None)
            if sender is None or not isinstance(data, str):
                return
            try:
                result = await router.dispatch_callback(int(sender), data)
            except PermissionError:
                await query.answer("无权执行此操作", show_alert=True)
                return
            except CommandError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            except Exception as exc:
                await query.answer(
                    f"操作失败，请查看网关日志 ({type(exc).__name__})",
                    show_alert=True,
                )
                return
            if result is None:
                return
            message = getattr(query, "message", None)
            editor = None if message is None else getattr(message, "edit_text", None)
            if callable(editor):
                await editor(result.text, reply_markup=reply_markup(result))
            else:
                await self.client.send_message(router.user_id, result.text)
            await query.answer("已处理")

        self._command_handlers = [
            self.client.add_handler(MessageHandler(message_handler)),
            self.client.add_handler(CallbackQueryHandler(callback_handler)),
        ]
        return tuple(self._command_handlers)

    def remove_command_router(self) -> None:
        if not self._command_handlers:
            return
        remover = getattr(self.client, "remove_handler", None)
        if callable(remover):
            for registered in self._command_handlers:
                if isinstance(registered, tuple):
                    remover(*registered)
                else:
                    remover(registered)
        self._command_handlers = []


class KurigramCallSignaling:
    """Small raw-call boundary used by the call coordinator and NTgCalls adapter."""

    def __init__(self, invoke: Callable[..., Awaitable[Any]]):
        self.invoke = invoke

    async def request_call(self, request: Any) -> Any:
        return await self.invoke(request)

    async def accept_call(self, request: Any) -> Any:
        return await self.invoke(request)

    async def confirm_call(self, request: Any) -> Any:
        return await self.invoke(request)

    async def send_signaling(self, request: Any) -> Any:
        return await self.invoke(request)

    async def discard_call(self, request: Any) -> Any:
        return await self.invoke(request)


class KurigramPyTgCallsBridge:
    """Concrete PyTgCalls/NTgCalls bridge for a Kurigram client.

    PyTgCalls currently detects the compatible ``pyrogram`` namespace and
    routes private-call signaling through its raw ``phone.*`` adapter. The
    wrapper keeps that dependency optional and exposes a small lifecycle/API
    surface to the gateway without importing PyTgCalls during USB or SMS-only
    operation.
    """

    def __init__(self, client: Any, workers: int = 8, cache_duration: int = 3600):
        try:
            from pytgcalls import PyTgCalls
        except ImportError as exc:
            raise KurigramRuntimeError("py-tgcalls is required for Telegram calls") from exc
        try:
            self.client = client
            self.engine = PyTgCalls(client, workers=workers, cache_duration=cache_duration)
        except Exception as exc:
            raise KurigramRuntimeError("Kurigram is incompatible with the installed PyTgCalls adapter") from exc
        self._pcm_bindings: list[PCMCallBinding] = []
        self._private_calls: list[PrivateCallHandle] = []

    @property
    def mtproto(self) -> Any:
        """Return PyTgCalls' raw-call facade using Kurigram phone.* types.

        PyTgCalls 2.3.3 exposes ``mtproto_client`` as the underlying Kurigram
        client, while its actual signaling adapter is the private ``_app``
        ``MtProtoClient``.  Prefer a future public facade when it has the raw
        methods, then fall back to that 2.x adapter explicitly.
        """
        candidates = (
            getattr(self.engine, "mtproto_client", None),
            getattr(self.engine, "_app", None),
        )
        required = ("request_call", "accept_call", "confirm_call", "send_signaling", "discard_call")
        for candidate in candidates:
            if candidate is not None and all(callable(getattr(candidate, name, None)) for name in required):
                return candidate
        return candidates[0]

    async def start(self) -> None:
        await self.engine.start()

    async def stop(self) -> None:
        """Release gateway-owned media handlers.

        PyTgCalls 2.x has no public engine-wide ``stop`` method; the User API
        client is owned by ``KurigramTelegramService``.  The gateway therefore
        removes every external PCM pump it installed and leaves call teardown
        to ``stop_private_call``/the call coordinator.
        """
        for binding in tuple(self._pcm_bindings):
            try:
                await self.detach_pcm(binding)
            except Exception:
                pass
        self._pcm_bindings.clear()
        for handle in tuple(self._private_calls):
            try:
                await self.stop_private_call(handle)
            except Exception:
                pass
        self._private_calls.clear()

    async def play(self, chat_id: Any, media: Any, **kwargs: Any) -> Any:
        return await self.engine.play(chat_id, media, **kwargs)

    async def record(self, chat_id: Any, media: Any, **kwargs: Any) -> Any:
        return await self.engine.record(chat_id, media, **kwargs)

    async def leave_call(self, chat_id: Any, close: bool = False) -> Any:
        return await self.engine.leave_call(chat_id, close=close)

    async def request_call(self, user_id: int, g_a_hash: bytes, protocol: Any, has_video: bool = False) -> Any:
        return await self.mtproto.request_call(user_id, g_a_hash, protocol, has_video)

    async def accept_call(self, user_id: int, g_b: bytes, protocol: Any) -> Any:
        return await self.mtproto.accept_call(user_id, g_b, protocol)

    async def confirm_call(self, user_id: int, g_a: bytes, key_fingerprint: int, protocol: Any) -> Any:
        return await self.mtproto.confirm_call(user_id, g_a, key_fingerprint, protocol)

    async def send_signaling(self, user_id: int, data: bytes) -> Any:
        return await self.mtproto.send_signaling(user_id, data)

    async def discard_call(self, user_id: int, is_missed: bool = False) -> Any:
        return await self.mtproto.discard_call(user_id, is_missed)

    def external_audio_stream(self) -> Any:
        try:
            from pytgcalls.types import MediaStream
            from pytgcalls.types.raw.audio_parameters import AudioParameters
            from pytgcalls.types.stream.external_media import ExternalMedia
            return MediaStream(
                ExternalMedia.AUDIO,
                audio_parameters=AudioParameters(NTGCALLS_AUDIO_SAMPLE_RATE, 1),
                video_flags=MediaStream.Flags.IGNORE,
            )
        except (ImportError, AttributeError, TypeError) as exc:
            raise KurigramRuntimeError("PyTgCalls external audio stream is unavailable") from exc

    def external_record_stream(self) -> Any:
        """Describe remote speaker PCM as an external playback sink.

        Registering a ``StreamFrames`` handler alone is not enough: NTgCalls
        emits playback frames only after PyTgCalls configures a PLAYBACK
        source with ``record()``.  ``RecordStream(audio=True)`` selects that
        external sink without creating a file or host audio device.
        """
        try:
            from pytgcalls.types import RecordStream
            from pytgcalls.types.raw.audio_parameters import AudioParameters
            return RecordStream(
                audio=True,
                audio_parameters=AudioParameters(NTGCALLS_AUDIO_SAMPLE_RATE, 1),
            )
        except (ImportError, AttributeError, TypeError) as exc:
            raise KurigramRuntimeError("PyTgCalls external record stream is unavailable") from exc

    async def send_frame(self, chat_id: int, device: Any, data: bytes) -> Any:
        return await self.engine.send_frame(chat_id, device, data)

    async def attach_pcm(self, chat_id: int, pcm_bridge: Any) -> PCMCallBinding:
        try:
            from pytgcalls import filters
            from pytgcalls.types import Device, Direction
            from qdc507_gateway.audio.ring import PCMFrame
        except ImportError as exc:
            raise KurigramRuntimeError("PyTgCalls stream-frame API is unavailable") from exc

        async def on_frames(_, update):
            if getattr(update, "chat_id", None) != chat_id:
                return
            if getattr(update, "device", None) not in (Device.MICROPHONE, Device.SPEAKER):
                return
            for frame in update.frames:
                pcm_bridge.push_telegram(PCMFrame(
                    frame.frame,
                    NTGCALLS_AUDIO_SAMPLE_RATE,
                    1,
                    2,
                ))

        handler = self.engine.on_update(
            filters.stream_frame(Direction.INCOMING),
        )(on_frames)

        async def pump() -> None:
            from pytgcalls.types import Device
            from qdc507_gateway.audio.alsa import resample_pcm16_mono

            pending = bytearray()
            loop = asyncio.get_running_loop()
            next_send_at: Optional[float] = None
            while True:
                frame = pcm_bridge.pull_for_telegram()
                if frame is None:
                    await asyncio.sleep(0.002)
                    continue
                pending.extend(resample_pcm16_mono(
                    frame.data,
                    frame.sample_rate,
                    NTGCALLS_AUDIO_SAMPLE_RATE,
                ))
                while len(pending) >= NTGCALLS_AUDIO_FRAME_BYTES:
                    if next_send_at is not None:
                        delay = next_send_at - loop.time()
                        if delay > 0:
                            await asyncio.sleep(delay)
                    data = bytes(pending[:NTGCALLS_AUDIO_FRAME_BYTES])
                    del pending[:NTGCALLS_AUDIO_FRAME_BYTES]
                    data = pcm_bridge.mix_telegram_cue(
                        data,
                        NTGCALLS_AUDIO_SAMPLE_RATE,
                    )
                    try:
                        await self.send_frame(chat_id, Device.MICROPHONE, data)
                    except Exception as exc:
                        if self._is_not_in_call_error(exc):
                            return
                        raise
                    sent_at = loop.time()
                    frame_seconds = NTGCALLS_AUDIO_FRAME_MS / 1000
                    if next_send_at is None or next_send_at < sent_at - (2 * frame_seconds):
                        next_send_at = sent_at + frame_seconds
                    else:
                        next_send_at += frame_seconds

        try:
            await self.record(chat_id, self.external_record_stream())
        except Exception:
            remover = getattr(self.engine, "remove_handler", None)
            if callable(remover):
                remover(handler)
            raise

        binding = PCMCallBinding(chat_id, handler, asyncio.create_task(pump()))
        self._pcm_bindings.append(binding)
        return binding

    async def detach_pcm(self, binding: PCMCallBinding) -> None:
        binding.pump_task.cancel()
        error = None
        try:
            await binding.pump_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._is_not_in_call_error(exc):
                error = exc
        finally:
            remover = getattr(self.engine, "remove_handler", None)
            if callable(remover):
                remover(binding.handler)
            if binding in self._pcm_bindings:
                self._pcm_bindings.remove(binding)
        if error is not None:
            raise error

    @staticmethod
    def _is_not_in_call_error(error: Exception) -> bool:
        return type(error).__name__ in {"NotInCallError", "ConnectionNotFound"}

    async def start_private_call(
        self,
        user_id: int,
        stream: Any = None,
        on_connected: Optional[Callable[[PrivateCallHandle], Awaitable[Any]]] = None,
        on_failed: Optional[Callable[[PrivateCallHandle, Exception], Awaitable[Any]]] = None,
        on_disconnected: Optional[Callable[[PrivateCallHandle], Awaitable[Any]]] = None,
    ) -> PrivateCallHandle:
        """Start PyTgCalls' private P2P flow without blocking the caller.

        ``play`` completes only after NTgCalls has connected, so it is
        monitored in a task. This gives the gateway state machine a handle
        immediately while preserving PyTgCalls' own phone.* update handling.
        """
        task = asyncio.create_task(self.play(user_id, stream))
        handle = PrivateCallHandle(user_id, task)
        self._private_calls.append(handle)

        if on_disconnected is not None:
            try:
                from pytgcalls.types import ChatUpdate
            except ImportError as exc:
                self._private_calls.remove(handle)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise KurigramRuntimeError(
                    "PyTgCalls call-disconnect update API is unavailable"
                ) from exc

            async def on_update(_, update):
                if not isinstance(update, ChatUpdate):
                    return
                if update.chat_id != user_id:
                    return
                if not update.status & ChatUpdate.Status.DISCARDED_CALL:
                    return
                # A remote discard can be delivered more than once while the
                # coordinator is cleaning up. Remove this handler first so a
                # repeated update cannot keep scheduling teardown work.
                self._remove_disconnect_handler(handle)
                await on_disconnected(handle)

            handle.disconnect_handler = self.engine.on_update()(on_update)

        async def monitor() -> None:
            try:
                await task
                if on_connected is not None:
                    await on_connected(handle)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._remove_disconnect_handler(handle)
                if handle in self._private_calls:
                    self._private_calls.remove(handle)
                if on_failed is not None:
                    await on_failed(handle, exc)

        asyncio.create_task(monitor())
        return handle

    async def stop_private_call(self, handle: Any, is_missed: bool = False) -> Any:
        user_id = handle.user_id if isinstance(handle, PrivateCallHandle) else int(handle)
        task = handle.task if isinstance(handle, PrivateCallHandle) else None
        if isinstance(handle, PrivateCallHandle):
            self._remove_disconnect_handler(handle)
            if handle in self._private_calls:
                self._private_calls.remove(handle)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        try:
            return await self.leave_call(user_id, close=False)
        except Exception as leave_error:
            # A remote discard can race this cleanup, or a pending P2P call
            # may not have created an NTgCalls binding yet.  Preserve the
            # raw discard fallback without leaving a live media binding.
            try:
                return await self.discard_call(user_id, is_missed)
            except Exception:
                raise leave_error

    def _remove_disconnect_handler(self, handle: PrivateCallHandle) -> None:
        handler = handle.disconnect_handler
        if handler is None:
            return
        remover = getattr(self.engine, "remove_handler", None)
        if callable(remover):
            remover(handler)
        handle.disconnect_handler = None
