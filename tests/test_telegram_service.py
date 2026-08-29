import asyncio

from qdc507_gateway.events import EventBus
from qdc507_gateway.telegram.service import KurigramTelegramService


def test_kurigram_service_starts_routes_and_forwards_sms(tmp_path):
    async def run():
        sent = []
        (tmp_path / "telegram.session").touch()

        class Identity:
            def __init__(self, user_id, is_bot):
                self.id = user_id
                self.is_bot = is_bot

        class Client:
            def __init__(self, name, identity):
                self.name = name
                self.identity = identity
                self.started = False
                self.handlers = []

            async def start(self):
                self.started = True

            async def stop(self):
                self.started = False

            async def get_me(self):
                return self.identity

            def add_handler(self, handler):
                self.handlers.append(handler)
                return handler

            async def send_message(self, chat_id, text):
                sent.append((self.name, chat_id, text))
                return "message"

            async def resolve_peer(self, peer):
                return peer

        class Bridge:
            async def start(self):
                pass

            async def stop(self):
                pass

        user_client = Client("user", Identity(100, False))
        bot_client = Client("bot", Identity(200, True))

        def client_factory(*_args, **kwargs):
            return bot_client if kwargs.get("bot_token") else user_client

        service = KurigramTelegramService(
            session_path=tmp_path / "telegram.session",
            bot_session_path=tmp_path / "telegram-bot.session",
            api_id=1,
            api_hash="hash",
            bot_token="123:token",
            user_id=42,
            events=EventBus(),
            status=lambda: {"ok": True},
            start_call=lambda number, user_id: None,
            send_sms=lambda number, text: None,
            hangup=lambda call_id: None,
            client_factory=client_factory,
            bridge_factory=lambda _: Bridge(),
            send_at=lambda command: {"command": command, "ok": True, "terminal": "OK"},
            restart_module=lambda: {"operation": "cfun", "reenumerated": True},
        )
        assert await service.start()
        assert service.state == "connected"
        assert service.bot_state == "connected"
        assert service.command_router.allowed_ids == (42,)
        assert service.command_router.send_at is not None
        assert service.command_router.restart_module is not None
        await service.forward_sms({"id": "sms-1", "sender": "+1", "body": "hello"})
        assert sent == [("bot", 42, "[接收短信]\n发件人: +1\n内容: hello")]
        await service.notify_incoming_cellular_call("+12045550100")
        assert sent[-1] == (
            "bot",
            42,
            "[蜂窝来电]\n电话号码: +12045550100\n"
            "正在呼叫你的 Telegram；接通后才会接听模块来电。",
        )
        assert user_client.handlers == []
        assert len(bot_client.handlers) == 2
        await service.stop()
        assert service.state == "stopped"
        assert service.bot_state == "stopped"

    asyncio.run(run())


def test_kurigram_service_requests_login_without_blocking_gateway_startup(tmp_path):
    async def run():
        events = []

        async def persist(event):
            events.append(event)

        service = KurigramTelegramService(
            session_path=tmp_path / "telegram.session",
            bot_session_path=tmp_path / "telegram-bot.session",
            api_id=1,
            api_hash="hash",
            bot_token=None,
            user_id=42,
            events=EventBus(persist=persist),
            status=lambda: {},
            start_call=lambda number, user_id: None,
            send_sms=lambda number, text: None,
            hangup=lambda call_id: None,
            client_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("client must not start before interactive login")
            ),
        )
        assert not await service.start()
        assert service.state == "login_required"
        assert service.last_error == "Telegram session is not initialized"
        assert events[-1].type == "telegram.login_required"

    asyncio.run(run())


def test_kurigram_service_is_disabled_without_credentials(tmp_path):
    async def run():
        service = KurigramTelegramService(
            session_path=tmp_path / "telegram.session",
            bot_session_path=tmp_path / "telegram-bot.session",
            api_id=None,
            api_hash=None,
            bot_token=None,
            user_id=None,
            events=EventBus(),
            status=lambda: {},
            start_call=lambda number, user_id: None,
            send_sms=lambda number, text: None,
            hangup=lambda call_id: None,
        )
        assert not await service.start()
        assert service.state == "disabled"

    asyncio.run(run())


def test_bot_stays_available_when_user_session_needs_login(tmp_path):
    async def run():
        class Identity:
            id = 200
            is_bot = True

        class BotClient:
            def __init__(self):
                self.handlers = []

            async def start(self):
                return None

            async def stop(self):
                return None

            async def get_me(self):
                return Identity()

            def add_handler(self, handler):
                self.handlers.append(handler)
                return handler

        bot = BotClient()

        def factory(*_args, **kwargs):
            assert kwargs.get("bot_token") == "123:token"
            return bot

        service = KurigramTelegramService(
            session_path=tmp_path / "telegram.session",
            bot_session_path=tmp_path / "telegram-bot.session",
            api_id=1,
            api_hash="hash",
            bot_token="123:token",
            user_id=42,
            events=EventBus(),
            status=lambda: {},
            start_call=lambda *_: None,
            send_sms=lambda *_: None,
            hangup=lambda *_: None,
            client_factory=factory,
        )
        assert await service.start()
        assert service.state == "login_required"
        assert service.bot_state == "connected"
        assert len(bot.handlers) == 2
        assert service.command_router.begin_user_login is not None
        await service.stop()

    asyncio.run(run())


def test_bot_can_replace_and_restart_a_lost_user_session(tmp_path):
    async def run():
        session_path = tmp_path / "telegram.session"
        session_path.write_text("old-session", encoding="utf-8")

        class Identity:
            def __init__(self, user_id):
                self.id = user_id
                self.is_bot = False

        class LoginClient:
            def __init__(self, path):
                self.path = path
                self.connected = False

            async def connect(self):
                self.connected = True
                self.path.write_text("new-session", encoding="utf-8")
                return False

            async def disconnect(self):
                self.connected = False

            async def send_code(self, _phone):
                return type("SentCode", (), {"phone_code_hash": "challenge"})()

            async def sign_in(self, phone, phone_code_hash, code):
                assert phone == "+14312764514"
                assert phone_code_hash == "challenge"
                assert code == "12345"
                return Identity(100)

        class RuntimeClient:
            async def start(self):
                return None

            async def stop(self):
                return None

            async def get_me(self):
                return Identity(100)

        class Bridge:
            async def start(self):
                return None

            async def stop(self):
                return None

        def factory(name, _api_id, _api_hash, workdir, **_kwargs):
            if name.startswith(".telegram.relogin-"):
                return LoginClient(tmp_path / f"{name}.session")
            return RuntimeClient()

        service = KurigramTelegramService(
            session_path=session_path,
            bot_session_path=tmp_path / "bot.session",
            api_id=1,
            api_hash="hash",
            bot_token=None,
            user_id=42,
            events=EventBus(),
            status=lambda: {},
            start_call=lambda *_: None,
            send_sms=lambda *_: None,
            hangup=lambda *_: None,
            client_factory=factory,
            bridge_factory=lambda _: Bridge(),
        )
        assert "验证码已发送" in await service.begin_user_login("+14312764514")
        assert service.state == "login_code_required"
        assert "通话功能已恢复" in await service.submit_user_code("12345")
        assert service.state == "connected"
        assert service.account_user_id == 100
        assert session_path.read_text(encoding="utf-8") == "new-session"
        assert (tmp_path / "telegram.session.bak").read_text(encoding="utf-8") == "old-session"
        await service.stop()

    asyncio.run(run())


def test_daemon_does_not_open_an_interactive_prompt_for_unauthorized_session(tmp_path):
    async def run():
        (tmp_path / "telegram.session").touch()

        class UnauthorizedClient:
            started = False

            async def connect(self):
                return False

            async def disconnect(self):
                return None

            async def start(self):
                self.started = True

            async def stop(self):
                return None

        client = UnauthorizedClient()
        service = KurigramTelegramService(
            session_path=tmp_path / "telegram.session",
            bot_session_path=tmp_path / "bot.session",
            api_id=1,
            api_hash="hash",
            bot_token=None,
            user_id=42,
            events=EventBus(),
            status=lambda: {},
            start_call=lambda *_: None,
            send_sms=lambda *_: None,
            hangup=lambda *_: None,
            client_factory=lambda *_args, **_kwargs: client,
        )
        assert not await service.start()
        assert service.state == "login_required"
        assert service.last_error == "Telegram session is not authorized"
        assert client.started is False

    asyncio.run(run())
