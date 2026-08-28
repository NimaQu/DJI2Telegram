from __future__ import annotations

import asyncio
import inspect
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from qdc507_gateway.events import EventBus
from qdc507_gateway.models import GatewayEvent

from .commands import TelegramCommandRouter
from .kurigram import (
    KurigramMessageClient,
    KurigramPyTgCallsBridge,
    create_kurigram_client,
    ensure_session_permissions,
)


class TelegramServiceError(RuntimeError):
    pass


@dataclass
class UserLoginAttempt:
    client: Any
    session_path: Path
    phone_number: str
    phone_code_hash: str
    expires_at: float
    password_required: bool = False


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class KurigramTelegramService:
    """User session for calls plus a separate bot for messages and commands."""

    def __init__(
        self,
        *,
        session_path: str | Path,
        bot_session_path: str | Path,
        api_id: Optional[int],
        api_hash: Optional[str],
        bot_token: Optional[str],
        user_id: Optional[int],
        events: EventBus,
        status: Callable[[], Any],
        start_call: Callable[[str, int], Any],
        send_sms: Callable[[str, str], Any],
        hangup: Callable[[Optional[str]], Any],
        client_factory: Optional[Callable[..., Any]] = None,
        bridge_factory: Optional[Callable[[Any], Any]] = None,
        telegram_call_connected: Optional[Callable[[Any], Awaitable[Any]]] = None,
        telegram_call_failed: Optional[Callable[[Any, Exception], Awaitable[Any]]] = None,
        telegram_call_disconnected: Optional[Callable[[Any], Awaitable[Any]]] = None,
        restart_service: Optional[Callable[[], Any]] = None,
        user_login_allowed: Optional[Callable[[], Any]] = None,
    ):
        self.session_path = Path(session_path)
        self.bot_session_path = Path(bot_session_path)
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        self.user_id = user_id
        self.events = events
        self.status_callback = status
        self.start_call = start_call
        self.send_sms_callback = send_sms
        self.hangup_callback = hangup
        self.client_factory = client_factory or create_kurigram_client
        self.bridge_factory = bridge_factory or KurigramPyTgCallsBridge
        self.telegram_call_connected = telegram_call_connected
        self.telegram_call_failed = telegram_call_failed
        self.telegram_call_disconnected = telegram_call_disconnected
        self.restart_service_callback = restart_service
        self.user_login_allowed = user_login_allowed
        self.client: Any = None
        self.bot_client: Any = None
        self.message_client: Optional[KurigramMessageClient] = None
        self.call_bridge: Any = None
        self.command_router: Optional[TelegramCommandRouter] = None
        self.account_user_id: Optional[int] = None
        self.bot_account_user_id: Optional[int] = None
        self.state = "disabled"
        self.last_error: Optional[str] = None
        self.bot_state = "disabled"
        self.bot_last_error: Optional[str] = None
        self._login_attempt: Optional[UserLoginAttempt] = None
        self._login_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash and self.user_id)

    @property
    def bot_configured(self) -> bool:
        return bool(self.api_id and self.api_hash and self.user_id and self.bot_token)

    async def start(self) -> bool:
        bot_started = await self._start_bot()
        if not self.configured:
            self.state = "disabled"
            return bot_started
        if not self.session_path.is_file():
            self.state = "login_required"
            self.last_error = "Telegram session is not initialized"
            await self.events.publish(GatewayEvent("telegram.login_required", {
                "session": str(self.session_path),
            }))
            return bot_started
        user_started = await self._start_user_session()
        return user_started or bot_started

    async def _start_user_session(self) -> bool:
        try:
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            self.session_path.parent.chmod(0o700)
            self.client = self.client_factory(
                self.session_path.stem,
                self.api_id,
                self.api_hash,
                self.session_path.parent,
            )
            connector = getattr(self.client, "connect", None)
            disconnect = getattr(self.client, "disconnect", None)
            if callable(connector) and callable(disconnect):
                authorized = bool(await _maybe_await(connector()))
                await _maybe_await(disconnect())
                if not authorized:
                    self.client = None
                    self.state = "login_required"
                    self.last_error = "Telegram session is not authorized"
                    await self.events.publish(GatewayEvent("telegram.login_required", {
                        "session": str(self.session_path),
                    }))
                    return False
            await _maybe_await(self.client.start())
            ensure_session_permissions(self.session_path)
            me = await _maybe_await(self.client.get_me()) if callable(getattr(self.client, "get_me", None)) else None
            self.account_user_id = getattr(me, "id", None)
            if self.account_user_id is not None and self.account_user_id == self.user_id:
                raise TelegramServiceError(
                    "gateway and target Telegram accounts must be different"
                )
            self.state = "connected"
            self.last_error = None
            await self.events.publish(GatewayEvent("telegram.connected", {
                "account_user_id": self.account_user_id,
                "user_id": self.user_id,
            }))
            try:
                self.call_bridge = self.bridge_factory(self.client)
                await _maybe_await(self.call_bridge.start())
            except Exception as exc:
                self.call_bridge = None
                self.last_error = f"call bridge unavailable: {type(exc).__name__}"
                await self.events.publish(GatewayEvent("telegram.error", {"error": self.last_error}))
            return True
        except Exception as exc:
            await self._stop_user_runtime()
            self.state = "error"
            self.last_error = type(exc).__name__
            await self.events.publish(GatewayEvent("telegram.error", {"error": self.last_error}))
            return False

    async def stop(self) -> None:
        async with self._login_lock:
            await self._abort_user_login_locked()
        await self._stop_user_runtime()
        await self._stop_bot_client()
        if self.state == "connected":
            self.state = "stopped"
        await self.events.publish(GatewayEvent("telegram.disconnected", {}))

    async def begin_user_login(self, phone_number: str) -> str:
        if not self.configured:
            raise TelegramServiceError("Telegram User API is not configured")
        if self.user_login_allowed is not None:
            allowed = await _maybe_await(self.user_login_allowed())
            if not allowed:
                raise TelegramServiceError("通话或音频会话期间不能重新登录 User")
        async with self._login_lock:
            await self._abort_user_login_locked()
            await self._stop_user_runtime()
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            self.session_path.parent.chmod(0o700)
            temporary_name = f".{self.session_path.stem}.relogin-{secrets.token_hex(6)}"
            temporary_path = self.session_path.parent / f"{temporary_name}.session"
            client = self.client_factory(
                temporary_name,
                self.api_id,
                self.api_hash,
                self.session_path.parent,
            )
            try:
                connector = getattr(client, "connect", None)
                sender = getattr(client, "send_code", None)
                if not callable(connector) or not callable(sender):
                    raise TelegramServiceError("Kurigram interactive login API is unavailable")
                await _maybe_await(connector())
                sent_code = await _maybe_await(sender(phone_number))
                phone_code_hash = getattr(sent_code, "phone_code_hash", None)
                if not isinstance(phone_code_hash, str) or not phone_code_hash:
                    raise TelegramServiceError("Telegram did not return a login challenge")
                self._login_attempt = UserLoginAttempt(
                    client=client,
                    session_path=temporary_path,
                    phone_number=phone_number,
                    phone_code_hash=phone_code_hash,
                    expires_at=time.monotonic() + 600.0,
                )
                self.state = "login_code_required"
                self.last_error = None
                await self.events.publish(GatewayEvent("telegram.login_code_required", {}))
                return "验证码已发送。"
            except Exception as exc:
                await self._disconnect_login_client(client)
                self._remove_login_files(temporary_path)
                self.state = "error"
                self.last_error = type(exc).__name__
                raise TelegramServiceError("Telegram 无法发送登录验证码") from exc

    async def submit_user_code(self, code: str) -> str:
        normalized = code.replace(" ", "").replace("-", "")
        if not normalized.isdigit() or not 3 <= len(normalized) <= 10:
            raise TelegramServiceError("验证码格式无效")
        async with self._login_lock:
            attempt = await self._require_login_attempt_locked()
            signer = getattr(attempt.client, "sign_in", None)
            if not callable(signer):
                raise TelegramServiceError("Kurigram sign_in API is unavailable")
            try:
                user = await _maybe_await(signer(
                    attempt.phone_number,
                    attempt.phone_code_hash,
                    normalized,
                ))
            except Exception as exc:
                if type(exc).__name__ == "SessionPasswordNeeded":
                    attempt.password_required = True
                    self.state = "login_password_required"
                    return "该账号启用了两步验证。"
                self.last_error = type(exc).__name__
                raise TelegramServiceError("验证码无效或已过期") from exc
            return await self._complete_user_login_locked(attempt, user)

    async def submit_user_password(self, password: str) -> str:
        if not password:
            raise TelegramServiceError("两步验证密码不能为空")
        async with self._login_lock:
            attempt = await self._require_login_attempt_locked()
            if not attempt.password_required:
                raise TelegramServiceError("当前登录流程不需要两步验证密码")
            checker = getattr(attempt.client, "check_password", None)
            if not callable(checker):
                raise TelegramServiceError("Kurigram check_password API is unavailable")
            try:
                user = await _maybe_await(checker(password))
            except Exception as exc:
                self.last_error = type(exc).__name__
                raise TelegramServiceError("两步验证密码不正确") from exc
            return await self._complete_user_login_locked(attempt, user)

    async def cancel_user_login(self) -> str:
        async with self._login_lock:
            had_attempt = self._login_attempt is not None
            await self._abort_user_login_locked()
            if had_attempt and self.session_path.is_file():
                self.state = "stopped"
                if await self._start_user_session():
                    return "User 登录已取消，原有通话 session 已恢复。"
            if had_attempt:
                self.state = "login_required"
            return "User 登录已取消。"

    async def _complete_user_login_locked(
        self,
        attempt: UserLoginAttempt,
        user: Any,
    ) -> str:
        account_user_id = getattr(user, "id", None)
        if account_user_id is None:
            getter = getattr(attempt.client, "get_me", None)
            if callable(getter):
                account_user_id = getattr(await _maybe_await(getter()), "id", None)
        if account_user_id is None:
            await self._abort_user_login_locked()
            self.state = "login_required"
            raise TelegramServiceError("Telegram 登录未返回 User 账号")
        if int(account_user_id) == int(self.user_id or 0):
            await self._abort_user_login_locked()
            self.state = "login_required"
            raise TelegramServiceError("通话 User 账号不能与 Bot 管理账号相同")
        await self._disconnect_login_client(attempt.client)
        if not attempt.session_path.is_file():
            self._login_attempt = None
            self.state = "login_required"
            raise TelegramServiceError("Kurigram 没有生成 User session 文件")
        backup_path = self.session_path.with_name(self.session_path.name + ".bak")
        if self.session_path.is_file():
            shutil.copy2(self.session_path, backup_path)
            backup_path.chmod(0o600)
        os.replace(attempt.session_path, self.session_path)
        ensure_session_permissions(self.session_path)
        self._remove_login_files(attempt.session_path)
        self._login_attempt = None
        self.state = "stopped"
        if not await self._start_user_session():
            raise TelegramServiceError("User session 已保存，但通话客户端启动失败")
        await self.events.publish(GatewayEvent("telegram.relogin_completed", {
            "account_user_id": self.account_user_id,
        }))
        return "User session 已重新登录，Telegram 通话功能已恢复。"

    async def _require_login_attempt_locked(self) -> UserLoginAttempt:
        attempt = self._login_attempt
        if attempt is None:
            raise TelegramServiceError("没有待处理的 User 登录，请先使用 /userlogin")
        if attempt.expires_at <= time.monotonic():
            await self._abort_user_login_locked()
            self.state = "login_required"
            raise TelegramServiceError("登录流程已过期，请重新使用 /userlogin")
        return attempt

    async def _abort_user_login_locked(self) -> None:
        attempt = self._login_attempt
        self._login_attempt = None
        if attempt is None:
            return
        await self._disconnect_login_client(attempt.client)
        self._remove_login_files(attempt.session_path)

    @staticmethod
    async def _disconnect_login_client(client: Any) -> None:
        disconnect = getattr(client, "disconnect", None)
        if callable(disconnect):
            try:
                await _maybe_await(disconnect())
            except Exception:
                pass

    @staticmethod
    def _remove_login_files(session_path: Path) -> None:
        for suffix in ("", "-journal", "-shm", "-wal"):
            candidate = Path(str(session_path) + suffix)
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass

    async def forward_sms(self, message: dict[str, object]) -> Any:
        if self.message_client is None or self.user_id is None:
            raise TelegramServiceError("Telegram bot message client is unavailable")
        text = "[接收短信]\n发件人: %s\n内容: %s" % (
            message.get("sender", "unknown"),
            message.get("body", ""),
        )
        result = await self.message_client.send_message(self.user_id, text)
        await self.events.publish(GatewayEvent("sms.forwarded", {
            "id": message.get("id"),
            "telegram_user_id": self.user_id,
        }))
        return result

    async def notify_incoming_cellular_call(self, number: Optional[str]) -> Any:
        """Tell the configured user which cellular number is ringing."""
        if self.message_client is None or self.user_id is None:
            raise TelegramServiceError("Telegram bot message client is unavailable")
        displayed = number or "unknown"
        result = await self.message_client.send_message(
            self.user_id,
            "[蜂窝来电]\n电话号码: %s\n"
            "正在呼叫你的 Telegram；接通后才会接听模块来电。" % displayed,
        )
        await self.events.publish(GatewayEvent("call.notification_sent", {
            "telegram_user_id": self.user_id,
            "cellular_number": number,
        }))
        return result

    async def request_private_call(self, user_id: int, stream: Any = None) -> Any:
        if self.call_bridge is None:
            raise TelegramServiceError("Kurigram call bridge is unavailable")
        starter = getattr(self.call_bridge, "start_private_call", None)
        if not callable(starter):
            raise TelegramServiceError("Kurigram bridge has no private-call starter")
        await self.events.publish(GatewayEvent("telegram.call.requested", {
            "telegram_user_id": user_id,
        }))

        async def connected(handle: Any) -> None:
            await self.events.publish(GatewayEvent("telegram.call.connected", {
                "telegram_user_id": user_id,
            }))
            if self.telegram_call_connected is not None:
                await self.telegram_call_connected(handle)

        async def failed(handle: Any, error: Exception) -> None:
            await self.events.publish(GatewayEvent("telegram.call.failed", {
                "telegram_user_id": user_id,
                "error": type(error).__name__,
            }))
            if self.telegram_call_failed is not None:
                await self.telegram_call_failed(handle, error)

        async def disconnected(handle: Any) -> None:
            await self.events.publish(GatewayEvent("telegram.call.disconnected", {
                "telegram_user_id": user_id,
            }))
            if self.telegram_call_disconnected is not None:
                await self.telegram_call_disconnected(handle)

        try:
            kwargs = {
                "stream": stream,
                "on_connected": connected,
                "on_failed": failed,
            }
            if self.telegram_call_disconnected is not None:
                try:
                    if "on_disconnected" in inspect.signature(starter).parameters:
                        kwargs["on_disconnected"] = disconnected
                except (TypeError, ValueError):
                    pass
            return await starter(user_id, **kwargs)
        except Exception as exc:
            await self.events.publish(GatewayEvent("telegram.call.failed", {
                "telegram_user_id": user_id,
                "error": type(exc).__name__,
            }))
            raise

    async def hangup_private_call(self, handle: Any) -> Any:
        if self.call_bridge is None:
            return None
        stopper = getattr(self.call_bridge, "stop_private_call", None)
        if callable(stopper):
            result = await stopper(handle)
        else:
            result = await self.call_bridge.leave_call(
                handle.user_id if hasattr(handle, "user_id") else handle,
                close=False,
            )
        await self.events.publish(GatewayEvent("telegram.call.hung_up", {
            "telegram_user_id": getattr(handle, "user_id", handle),
        }))
        return result

    async def _start_bot(self) -> bool:
        if not self.bot_configured:
            self.bot_state = "disabled"
            self.bot_last_error = None
            return False
        try:
            self.bot_session_path.parent.mkdir(parents=True, exist_ok=True)
            self.bot_session_path.parent.chmod(0o700)
            self.bot_client = self.client_factory(
                self.bot_session_path.stem,
                self.api_id,
                self.api_hash,
                self.bot_session_path.parent,
                bot_token=self.bot_token,
            )
            await _maybe_await(self.bot_client.start())
            ensure_session_permissions(self.bot_session_path)
            me = (
                await _maybe_await(self.bot_client.get_me())
                if callable(getattr(self.bot_client, "get_me", None))
                else None
            )
            if not bool(getattr(me, "is_bot", False)):
                raise TelegramServiceError("configured bot token did not start a bot account")
            self.bot_account_user_id = getattr(me, "id", None)
            self.command_router = TelegramCommandRouter(
                self.user_id,
                self.status_callback,
                self.start_call,
                self.send_sms_callback,
                self.hangup_callback,
                begin_user_login=self.begin_user_login,
                submit_user_code=self.submit_user_code,
                submit_user_password=self.submit_user_password,
                cancel_user_login=self.cancel_user_login,
                user_login_state=lambda: self.state,
                restart_service=self.restart_service_callback,
            )
            self.message_client = KurigramMessageClient(self.bot_client)
            self.message_client.install_command_router(self.command_router)
            self.bot_state = "connected"
            self.bot_last_error = None
            await self.events.publish(GatewayEvent("telegram.bot_connected", {
                "account_user_id": self.bot_account_user_id,
                "user_id": self.user_id,
            }))
            return True
        except Exception as exc:
            await self._stop_bot_client()
            self.bot_state = "error"
            self.bot_last_error = type(exc).__name__
            await self.events.publish(GatewayEvent("telegram.bot_error", {
                "error": self.bot_last_error,
            }))
            return False

    async def _stop_bot_client(self) -> None:
        if self.message_client is not None:
            try:
                self.message_client.remove_command_router()
            except Exception:
                pass
        if self.bot_client is not None:
            try:
                await _maybe_await(self.bot_client.stop())
            except Exception:
                pass
        self.bot_client = None
        self.message_client = None
        self.command_router = None
        self.bot_account_user_id = None
        if self.bot_state == "connected":
            self.bot_state = "stopped"

    async def _stop_user_runtime(self) -> None:
        if self.call_bridge is not None:
            try:
                await _maybe_await(self.call_bridge.stop())
            except Exception:
                pass
            self.call_bridge = None
        await self._stop_user_client()

    async def _stop_user_client(self) -> None:
        if self.client is not None:
            try:
                await _maybe_await(self.client.stop())
            except Exception:
                pass
        self.client = None
        self.account_user_id = None
