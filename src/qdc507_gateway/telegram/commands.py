from __future__ import annotations

import asyncio
import inspect
import re
import secrets
import shlex
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple


@dataclass(frozen=True)
class TelegramCommand:
    name: str
    args: Tuple[str, ...]


@dataclass(frozen=True)
class TelegramResponse:
    text: str
    buttons: Tuple[Tuple["TelegramButton", ...], ...] = ()


@dataclass(frozen=True)
class TelegramButton:
    text: str
    callback_data: str


@dataclass
class SMSDraft:
    number: str
    expires_at: float
    content: Optional[str] = None
    token: Optional[str] = None
    sending: bool = False


@dataclass
class UserLoginConversation:
    stage: str
    expires_at: float


class CommandError(ValueError):
    pass


def parse_command(text: str) -> TelegramCommand:
    try:
        words = shlex.split(text.strip())
    except ValueError as exc:
        raise CommandError("invalid command quoting") from exc
    if not words or not words[0].startswith("/"):
        raise CommandError("not a gateway command")
    name = words[0].split("@", 1)[0].lower()
    aliases = {
        "/start": "help",
        "/help": "help",
        "/call": "call",
        "/sendsms": "sendsms",
        "/confirm": "confirm",
        "/cancel": "cancel",
        "/status": "status",
        "/hangup": "hangup",
        "/userlogin": "userlogin",
        "/usercode": "usercode",
        "/userpassword": "userpassword",
        "/restart": "restart",
    }
    if name not in aliases:
        raise CommandError("unsupported gateway command")
    return TelegramCommand(aliases[name], tuple(words[1:]))


def authorize_sender(sender_id: int, allowed_ids: Tuple[int, ...]) -> None:
    if sender_id not in allowed_ids:
        raise PermissionError("Telegram sender is not an authorized gateway user")


def validate_call_number(value: str) -> str:
    normalized = value.replace(" ", "").replace("-", "")
    digits = normalized.lstrip("+")
    if (
        not digits
        or not digits.isdigit()
        or not normalized
        or normalized[0] not in "+0123456789"
        or any(char not in "+0123456789" for char in normalized)
    ):
        raise CommandError("invalid call number")
    if normalized.count("+") > 1 or ("+" in normalized and not normalized.startswith("+")):
        raise CommandError("invalid call number")
    if len(normalized.lstrip("+")) > 20:
        raise CommandError("call number is too long")
    return normalized


def format_human_status(status: Any) -> str:
    if not isinstance(status, dict):
        return "[网关状态]\n状态数据不可用"
    module = status.get("module") if isinstance(status.get("module"), dict) else {}
    signal = module.get("signal") if isinstance(module.get("signal"), dict) else {}
    operator = module.get("operator") if isinstance(module.get("operator"), dict) else {}
    call = status.get("current_call") if isinstance(status.get("current_call"), dict) else None
    audio = status.get("audio") if isinstance(status.get("audio"), dict) else {}
    state_labels = {
        "connected": "已连接",
        "disconnected": "未连接",
        "disabled": "未配置",
        "login_required": "需要登录",
        "login_code_required": "等待验证码",
        "login_password_required": "等待两步验证密码",
        "error": "错误",
        "stopped": "已停止",
    }

    def state_label(value: Any) -> str:
        value = str(value or "unknown")
        return state_labels.get(value, value)

    uptime = status.get("uptime_seconds")
    if isinstance(uptime, (int, float)):
        uptime_text = f"{int(uptime // 3600)}小时{int(uptime % 3600 // 60)}分钟"
    else:
        uptime_text = "未知"
    phone_number = module.get("phone_number") or "SIM 未提供"
    operator_name = operator.get("name") or "未知"
    radio = operator.get("radio")
    operator_text = f"{operator_name} · {radio}" if radio else operator_name
    dbm = signal.get("dbm")
    bars = signal.get("bars")
    signal_text = (
        f"{dbm} dBm · {bars}/5"
        if isinstance(dbm, int) and isinstance(bars, int)
        else "未知"
    )
    if call is None:
        call_text = "空闲"
    else:
        call_text = "%s · %s · %s" % (
            call.get("state", "unknown"),
            call.get("cellular_number") or "号码未知",
            call.get("frontend", "unknown"),
        )
    audio_text = "已连接" if audio.get("mode") else "未连接"
    web_text = "已启用" if status.get("web_enabled", True) else "已关闭"
    return (
        "[网关状态]\n"
        f"服务: {status.get('service', 'DJI2Telegram')} {status.get('version', '')}\n"
        f"运行时间: {uptime_text}\n"
        f"模块: {state_label(status.get('module_state'))}\n"
        f"本机号码: {phone_number}\n"
        f"运营商: {operator_text}\n"
        f"信号: {signal_text}\n"
        f"Telegram User: {state_label(status.get('telegram_state'))}\n"
        f"Telegram Bot: {state_label(status.get('telegram_bot_state'))}\n"
        f"Web/API: {web_text}\n"
        f"当前通话: {call_text}\n"
        f"音频: {audio_text}\n"
        f"来电入口: {status.get('incoming_call_frontend', 'unknown')}"
    )


class TelegramCommandRouter:
    """Single-user commands plus an explicit, confirm-before-send SMS draft."""

    SMS_CALLBACK_PATTERN = re.compile(
        r"^qdc507\.sms\.(send|cancel)\.([A-Za-z0-9_-]{16,32})$"
    )

    def __init__(
        self,
        user_id: int,
        status: Callable[[], Any],
        start_call: Callable[[str, int], Any],
        send_sms: Callable[[str, str], Any],
        hangup: Callable[[Optional[str]], Any],
        sms_draft_ttl_seconds: float = 600.0,
        *,
        user_login_ttl_seconds: float = 600.0,
        begin_user_login: Optional[Callable[[str], Any]] = None,
        submit_user_code: Optional[Callable[[str], Any]] = None,
        submit_user_password: Optional[Callable[[str], Any]] = None,
        cancel_user_login: Optional[Callable[[], Any]] = None,
        user_login_state: Optional[Callable[[], Any]] = None,
        restart_service: Optional[Callable[[], Any]] = None,
    ):
        if sms_draft_ttl_seconds <= 0:
            raise ValueError("SMS draft lifetime must be positive")
        if user_login_ttl_seconds <= 0:
            raise ValueError("User login lifetime must be positive")
        self.user_id = int(user_id)
        self.allowed_ids = (self.user_id,)
        self.status = status
        self.start_call = start_call
        self.send_sms = send_sms
        self.hangup = hangup
        self.begin_user_login = begin_user_login
        self.submit_user_code = submit_user_code
        self.submit_user_password = submit_user_password
        self.cancel_user_login = cancel_user_login
        self.user_login_state = user_login_state
        self.restart_service = restart_service
        self.sms_draft_ttl_seconds = sms_draft_ttl_seconds
        self.user_login_ttl_seconds = user_login_ttl_seconds
        self._sms_draft: Optional[SMSDraft] = None
        self._user_login: Optional[UserLoginConversation] = None
        self._sms_lock = asyncio.Lock()
        self._user_login_lock = asyncio.Lock()

    def is_sensitive_input(self, text: str) -> bool:
        stripped = text.strip()
        command = stripped.lower().split(" ", 1)[0].split("@", 1)[0]
        if command in {"/userlogin", "/usercode", "/userpassword"}:
            return True
        return bool(stripped and self._user_login is not None)

    async def dispatch(self, sender_id: int, text: str) -> Optional[TelegramResponse]:
        authorize_sender(sender_id, self.allowed_ids)
        stripped = text.strip()
        if not stripped:
            raise CommandError("短信内容不能为空")
        if not stripped.startswith("/"):
            if self._user_login is not None:
                return await self._advance_user_login(text)
            return await self._update_sms_draft(text)

        command = parse_command(text)
        if command.name == "help":
            if command.args:
                raise CommandError("/start 和 /help 不接受参数")
            text = (
                "[QDC507 网关]\n"
                "/status 查看状态\n"
                "/call <号码> 拨打电话\n"
                "/sendsms <号码> 创建短信草稿\n"
                "/hangup 挂断当前通话"
            )
            if self.begin_user_login is not None:
                text += "\n/userlogin 交互式重新登录 Telegram User"
            if self.restart_service is not None:
                text += "\n/restart 重启网关服务"
            return TelegramResponse(text)
        if command.name == "status":
            if command.args:
                raise CommandError("/status takes no arguments")
            result = await self._call(self.status)
            return TelegramResponse(format_human_status(result))
        if command.name == "call":
            if len(command.args) != 1:
                raise CommandError("用法: /call <电话号码>")
            number = validate_call_number(command.args[0])
            await self._call(self.start_call, number, self.user_id)
            return TelegramResponse(
                f"[拨打电话]\n号码: {number}\n"
                "正在呼叫你的 Telegram；接通后才会通过模块拨号。"
            )
        if command.name == "sendsms":
            if len(command.args) != 1:
                raise CommandError("用法: /sendsms <电话号码>")
            if self._user_login is not None:
                raise CommandError("请先完成 User 登录，或使用 /cancel 取消")
            return await self._begin_sms_draft(validate_call_number(command.args[0]))
        if command.name == "confirm":
            if command.args:
                raise CommandError("/confirm 不接受参数")
            return await self._confirm_sms(None)
        if command.name == "cancel":
            if command.args:
                raise CommandError("/cancel 不接受参数")
            if self._user_login is not None:
                return await self._cancel_user_login()
            return await self._cancel_sms(None)
        if command.name == "hangup":
            if len(command.args) > 1:
                raise CommandError("/hangup accepts at most one call id")
            await self._call(self.hangup, command.args[0] if command.args else None)
            return TelegramResponse("[通话已挂断]")
        if command.name == "userlogin":
            if self.begin_user_login is None:
                raise CommandError("Telegram User 在线重登未启用")
            if len(command.args) > 1:
                raise CommandError("用法: /userlogin")
            return await self._begin_user_login(
                command.args[0] if command.args else None
            )
        if command.name == "usercode":
            if self.submit_user_code is None:
                raise CommandError("Telegram User 在线重登未启用")
            if len(command.args) != 1:
                raise CommandError("用法: /usercode <验证码>")
            async with self._user_login_lock:
                self._user_login = UserLoginConversation(
                    "code",
                    time.monotonic() + self.user_login_ttl_seconds,
                )
                return await self._submit_user_code_locked(command.args[0])
        if command.name == "userpassword":
            if self.submit_user_password is None:
                raise CommandError("Telegram User 在线重登未启用")
            if len(command.args) != 1:
                raise CommandError("用法: /userpassword <两步验证密码>")
            async with self._user_login_lock:
                self._user_login = UserLoginConversation(
                    "password",
                    time.monotonic() + self.user_login_ttl_seconds,
                )
                return await self._submit_user_password_locked(command.args[0])
        if command.name == "restart":
            if self.restart_service is None:
                raise CommandError("Bot 重启服务未启用")
            if command.args:
                raise CommandError("/restart 不接受参数")
            detail = await self._maintenance_call(self.restart_service)
            return TelegramResponse(f"[重启服务]\n{detail}")
        raise CommandError("unsupported gateway command")

    async def _begin_user_login(self, phone: Optional[str]) -> TelegramResponse:
        async with self._sms_lock:
            draft = self._sms_draft
            if draft is not None and draft.expires_at > time.monotonic():
                raise CommandError("请先完成短信草稿，或使用 /cancel 取消")
            if draft is not None:
                self._sms_draft = None
        async with self._user_login_lock:
            if self._user_login is not None and self.cancel_user_login is not None:
                await self._maintenance_call(self.cancel_user_login)
            self._user_login = UserLoginConversation(
                "phone",
                time.monotonic() + self.user_login_ttl_seconds,
            )
            if phone is None:
                return TelegramResponse(
                    "[Telegram User 登录]\n请直接回复 User 账号手机号，例如 +8613800138000。\n"
                    "使用 /cancel 可取消登录。"
                )
            return await self._submit_user_phone_locked(phone)

    async def _advance_user_login(self, value: str) -> TelegramResponse:
        async with self._user_login_lock:
            conversation = self._user_login
            if conversation is None:
                raise CommandError("没有待处理的 User 登录，请使用 /userlogin")
            if conversation.expires_at <= time.monotonic():
                self._user_login = None
                if self.cancel_user_login is not None:
                    await self._maintenance_call(self.cancel_user_login)
                raise CommandError("User 登录流程已过期，请重新使用 /userlogin")
            if conversation.stage == "phone":
                return await self._submit_user_phone_locked(value)
            if conversation.stage == "code":
                return await self._submit_user_code_locked(value)
            if conversation.stage == "password":
                return await self._submit_user_password_locked(value)
            self._user_login = None
            raise CommandError("User 登录流程状态无效，请重新使用 /userlogin")

    async def _submit_user_phone_locked(self, phone: str) -> TelegramResponse:
        assert self.begin_user_login is not None
        normalized = validate_call_number(phone.strip())
        detail = await self._maintenance_call(self.begin_user_login, normalized)
        self._user_login = UserLoginConversation(
            "code",
            time.monotonic() + self.user_login_ttl_seconds,
        )
        return TelegramResponse(
            f"[Telegram User 登录]\n{detail}\n请直接回复验证码，或使用 /cancel 取消。"
        )

    async def _submit_user_code_locked(self, code: str) -> TelegramResponse:
        assert self.submit_user_code is not None
        detail = await self._maintenance_call(self.submit_user_code, code)
        state = None
        if self.user_login_state is not None:
            state = await self._call(self.user_login_state)
        password_required = (
            state == "login_password_required"
            or (state is None and "两步验证" in str(detail))
        )
        if password_required:
            self._user_login = UserLoginConversation(
                "password",
                time.monotonic() + self.user_login_ttl_seconds,
            )
            return TelegramResponse(
                f"[Telegram User 登录]\n{detail}\n请直接回复两步验证密码，或使用 /cancel 取消。"
            )
        self._user_login = None
        return TelegramResponse(f"[Telegram User 登录]\n{detail}")

    async def _submit_user_password_locked(self, password: str) -> TelegramResponse:
        assert self.submit_user_password is not None
        detail = await self._maintenance_call(self.submit_user_password, password)
        self._user_login = None
        return TelegramResponse(f"[Telegram User 登录]\n{detail}")

    async def _cancel_user_login(self) -> TelegramResponse:
        async with self._user_login_lock:
            self._user_login = None
            detail = "User 登录已取消。"
            if self.cancel_user_login is not None:
                detail = str(await self._maintenance_call(self.cancel_user_login))
            return TelegramResponse(f"[Telegram User 登录]\n{detail}")

    async def dispatch_callback(
        self,
        sender_id: int,
        callback_data: str,
    ) -> Optional[TelegramResponse]:
        authorize_sender(sender_id, self.allowed_ids)
        match = self.SMS_CALLBACK_PATTERN.fullmatch(callback_data or "")
        if match is None:
            return None
        action, token = match.groups()
        if action == "send":
            return await self._confirm_sms(token)
        return await self._cancel_sms(token)

    async def _confirm_sms(self, token: Optional[str]) -> TelegramResponse:
        async with self._sms_lock:
            draft = self._require_sms_draft(token)
            if draft.sending:
                raise CommandError("短信正在发送，请勿重复确认")
            if not draft.content:
                raise CommandError("短信内容为空")
            draft.sending = True
            number = draft.number
            content = draft.content
        try:
            await self._call(self.send_sms, number, content)
        except Exception:
            async with self._sms_lock:
                if self._sms_draft is draft:
                    draft.sending = False
            raise
        async with self._sms_lock:
            if self._sms_draft is draft:
                self._sms_draft = None
        return TelegramResponse(
            f"[短信已发送]\n收件人: {number}\n内容: {content}"
        )

    async def _cancel_sms(self, token: Optional[str]) -> TelegramResponse:
        async with self._sms_lock:
            draft = self._require_sms_draft(token)
            if draft.sending:
                raise CommandError("短信正在发送，无法取消")
            self._sms_draft = None
        return TelegramResponse(
            f"[已取消发送短信]\n收件人: {draft.number}\n内容: {draft.content or ''}"
        )

    async def _begin_sms_draft(self, number: str) -> TelegramResponse:
        async with self._sms_lock:
            self._sms_draft = SMSDraft(
                number=number,
                expires_at=time.monotonic() + self.sms_draft_ttl_seconds,
            )
        return TelegramResponse(
            f"[发送短信]\n收件人: {number}\n请回复要发送的短信内容。"
        )

    async def _update_sms_draft(self, text: str) -> TelegramResponse:
        content = text.strip()
        if not content:
            raise CommandError("短信内容不能为空")
        if len(content) > 4096:
            raise CommandError("短信内容不能超过 4096 个字符")
        async with self._sms_lock:
            draft = self._require_sms_draft()
            if draft.sending:
                raise CommandError("短信正在发送")
            draft.content = content
            draft.token = secrets.token_urlsafe(12)
            draft.expires_at = time.monotonic() + self.sms_draft_ttl_seconds
            token = draft.token
            number = draft.number
        return TelegramResponse(
            f"[发送短信确认]\n收件人: {number}\n内容: {content}\n\n"
            "点击下方按钮确认或取消；如需修改，请直接回复新的短信内容。",
            ((
                TelegramButton("发送", f"qdc507.sms.send.{token}"),
                TelegramButton("取消", f"qdc507.sms.cancel.{token}"),
            ),),
        )

    def _require_sms_draft(self, token: Optional[str] = None) -> SMSDraft:
        draft = self._sms_draft
        if draft is None:
            raise CommandError("没有待处理的短信，请先使用 /sendsms <电话号码>")
        if draft.expires_at < time.monotonic():
            self._sms_draft = None
            raise CommandError("短信草稿已过期，请重新使用 /sendsms")
        if token is not None and draft.token != token:
            raise CommandError("此确认消息已失效，请使用最新按钮")
        return draft

    @staticmethod
    async def _call(callback: Callable[..., Any], *args: Any) -> Any:
        result = callback(*args)
        return await result if inspect.isawaitable(result) else result

    @classmethod
    async def _maintenance_call(cls, callback: Callable[..., Any], *args: Any) -> Any:
        try:
            return await cls._call(callback, *args)
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
