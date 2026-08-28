import asyncio
import ipaddress
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

from qdc507_gateway import __version__
from qdc507_gateway.events import EventBus
from qdc507_gateway.models import GatewayEvent
from qdc507_gateway.security import (
    AuthFailureLimiter,
    dangerous_at_command,
    forbidden_generic_at_command,
)
from qdc507_gateway.storage.database import Database
from qdc507_gateway.web.calls import (
    AUDIO_SUBPROTOCOL,
    extract_audio_ticket,
    public_call_error,
)


async def sse_event_stream(events: EventBus, keepalive_seconds: float = 25.0):
    """Stream events without cancelling the subscription on each keepalive.

    ``asyncio.wait_for(anext(...))`` cancels the pending ``anext`` when it
    times out, which closes an async generator and makes the next iteration
    raise ``StopAsyncIteration``. Keep one pending read alive instead.
    """
    iterator = events.subscribe()
    pending: asyncio.Task | None = None
    try:
        yield ": DJI2Telegram stream connected\n\n"
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait((pending,), timeout=keepalive_seconds)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            yield "event: %s\ndata: %s\n\n" % (
                event.type,
                json.dumps(event.to_dict(), ensure_ascii=False, default=str),
            )
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await iterator.aclose()


def create_app(database: Database, events: EventBus, state: Optional[Dict[str, Any]] = None, lifespan=None):
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket
        from fastapi.responses import RedirectResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError("FastAPI is required to create the REST application") from exc

    app = FastAPI(title="DJI2Telegram", version=__version__, lifespan=lifespan)
    state = state if state is not None else {}
    auth_limiter = state.get("auth_limiter")
    if not isinstance(auth_limiter, AuthFailureLimiter):
        auth_limiter = AuthFailureLimiter()
        state["auth_limiter"] = auth_limiter

    def request_identity(request: Request) -> str:
        peer = request.client.host if request.client is not None else "unknown"
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        forwarded = request.headers.get("x-forwarded-for")
        if peer_address.is_loopback and forwarded:
            candidate = forwarded.split(",", 1)[0].strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
        return str(peer_address)

    @app.middleware("http")
    async def disable_web_asset_cache(request, call_next):
        response = await call_next(request)
        if request.url.path == "/web" or request.url.path.startswith("/web/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    async def require_token(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ):
        identity = request_identity(request)
        decision = auth_limiter.check(identity)
        if decision.blocked:
            raise HTTPException(
                status_code=429,
                detail="too many authentication failures",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
        if not authorization or not authorization.startswith("Bearer "):
            decision = auth_limiter.record_failure(identity)
            if decision.newly_blocked:
                await events.publish(GatewayEvent("security.auth_blocked", {
                    "client": identity,
                    "retry_after_seconds": decision.retry_after_seconds,
                }))
            if decision.blocked:
                raise HTTPException(
                    status_code=429,
                    detail="too many authentication failures",
                    headers={"Retry-After": str(decision.retry_after_seconds)},
                )
            raise HTTPException(status_code=401, detail="Bearer token required")
        from qdc507_gateway.security import verify_token
        token = authorization[7:].strip()
        row = database.token()
        if row is not None and verify_token(token, row["token_hash"]):
            auth_limiter.record_success(identity)
            return "api"
        decision = auth_limiter.record_failure(identity)
        if decision.newly_blocked:
            await events.publish(GatewayEvent("security.auth_blocked", {
                "client": identity,
                "retry_after_seconds": decision.retry_after_seconds,
            }))
        if decision.blocked:
            raise HTTPException(
                status_code=429,
                detail="too many authentication failures",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
        raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/api/v1/status")
    async def status(_: str = Depends(require_token)):
        current = state.get("get_status", state.get("status", {
            "service": "DJI2Telegram", "module_state": "disconnected",
        }))
        if callable(current):
            current = current()
            if hasattr(current, "__await__"):
                current = await current
        return current

    @app.get("/api/v1/module")
    async def module(refresh: bool = False, _: str = Depends(require_token)):
        if refresh:
            refresher = state.get("refresh_module_status")
            if refresher is not None:
                try:
                    result = refresher()
                    if hasattr(result, "__await__"):
                        result = await result
                    return result
                except RuntimeError as exc:
                    raise HTTPException(status_code=503, detail=str(exc)) from exc
        return state.get("module", {"connected": False})

    @app.get("/api/v1/sms")
    async def sms(limit: int = 50, unread: Optional[bool] = None, _: str = Depends(require_token)):
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return {"items": [dict(row) for row in database.list_sms(limit, unread)]}

    @app.post("/api/v1/sms/send")
    async def send_sms(payload: Dict[str, Any], _: str = Depends(require_token)):
        destination = payload.get("to")
        text = payload.get("text")
        if not isinstance(destination, str) or not destination.strip():
            raise HTTPException(status_code=422, detail="to and text are required")
        if not isinstance(text, str) or not text:
            raise HTTPException(status_code=422, detail="to and text are required")
        if len(text) > 4096:
            raise HTTPException(status_code=422, detail="text is too long")
        sender = state.get("send_sms")
        if sender is None:
            raise HTTPException(status_code=503, detail="modem SMS service is unavailable")
        try:
            result = sender(destination, text)
            if hasattr(result, "__await__"):
                result = await result
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"queued": True, "result": result}

    @app.get("/api/v1/calls/current")
    async def current_call(_: str = Depends(require_token)):
        current = state.get("current_call")
        if callable(current):
            current = current()
            if hasattr(current, "__await__"):
                current = await current
        return None if current is None else asdict(current)

    @app.get("/api/v1/calls")
    async def calls(limit: int = 50, _: str = Depends(require_token)):
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return {"items": [dict(row) for row in database.list_calls(limit)]}

    @app.get("/api/v1/audio")
    async def audio_status(_: str = Depends(require_token)):
        current = state.get("get_audio", {})
        if callable(current):
            current = current()
            if hasattr(current, "__await__"):
                current = await current
        return current

    @app.post("/api/v1/calls/start")
    async def start_call(payload: Dict[str, Any], _: str = Depends(require_token)):
        number = payload.get("number")
        if not isinstance(number, str):
            raise HTTPException(status_code=422, detail="number is required")
        normalized = number.strip().replace(" ", "").replace("-", "")
        if not re.fullmatch(r"\+?[0-9]{1,20}", normalized):
            raise HTTPException(status_code=422, detail="invalid phone number")
        if payload.get("frontend", "web") != "web":
            raise HTTPException(status_code=422, detail="API calls use the web frontend")
        handler = state.get("start_web_call")
        if handler is None:
            raise HTTPException(status_code=503, detail="web call service is unavailable")
        try:
            result = handler(normalized)
            if hasattr(result, "__await__"):
                result = await result
            return asdict(result)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/calls/{call_id}/answer")
    async def answer_call(call_id: str, _: str = Depends(require_token)):
        handler = state.get("answer_web_call")
        if handler is None:
            raise HTTPException(status_code=503, detail="web call service is unavailable")
        try:
            result = handler(call_id)
            if hasattr(result, "__await__"):
                result = await result
            return asdict(result)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/calls/{call_id}/audio-ticket")
    async def audio_ticket(call_id: str, _: str = Depends(require_token)):
        handler = state.get("issue_audio_ticket")
        if handler is None:
            raise HTTPException(status_code=503, detail="web audio service is unavailable")
        try:
            result = handler(call_id)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/audio/diagnostic/start")
    async def start_audio_diagnostic(_: str = Depends(require_token)):
        handler = state.get("start_audio_diagnostic")
        if handler is None:
            raise HTTPException(status_code=503, detail="audio diagnostic service is unavailable")
        try:
            result = handler()
            if hasattr(result, "__await__"):
                result = await result
            return result
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/audio/diagnostic/{session_id}/ticket")
    async def audio_diagnostic_ticket(
        session_id: str,
        _: str = Depends(require_token),
    ):
        handler = state.get("issue_audio_diagnostic_ticket")
        if handler is None:
            raise HTTPException(status_code=503, detail="audio diagnostic service is unavailable")
        try:
            result = handler(session_id)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/calls/{call_id}/hangup")
    async def hangup(call_id: str, _: str = Depends(require_token)):
        handler = state.get("hangup")
        if handler is None:
            raise HTTPException(status_code=503, detail="call service is unavailable")
        try:
            result = handler(call_id)
            if hasattr(result, "__await__"):
                result = await result
            return {"call_id": call_id, "result": result}
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/module/reconnect")
    async def reconnect(_: str = Depends(require_token)):
        handler = state.get("reconnect")
        if handler is None:
            raise HTTPException(status_code=503, detail="module service is unavailable")
        try:
            result = handler()
            if hasattr(result, "__await__"):
                result = await result
            return {"accepted": True, "result": result}
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/v1/module/at")
    async def at(payload: Dict[str, Any], _: str = Depends(require_token)):
        command_value = payload.get("command")
        if not isinstance(command_value, str):
            raise HTTPException(status_code=422, detail="command must be a string")
        command = command_value.strip()
        if not command:
            raise HTTPException(status_code=422, detail="command is required")
        if len(command) > 1024 or any(ord(character) < 0x20 for character in command):
            raise HTTPException(status_code=422, detail="command is invalid")
        try:
            command.encode("ascii")
        except UnicodeEncodeError as exc:
            raise HTTPException(status_code=422, detail="command must be ASCII") from exc
        timeout_ms = payload.get("timeout_ms", 3000)
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 100 <= timeout_ms <= 60000:
            raise HTTPException(status_code=422, detail="timeout_ms must be between 100 and 60000")
        if forbidden_generic_at_command(command):
            raise HTTPException(status_code=403, detail="QADBKEY requires the dedicated authorization endpoint")
        if dangerous_at_command(command) and payload.get("confirm_persistent") is not True:
            raise HTTPException(status_code=409, detail="explicit persistent-command confirmation required")
        handler = state.get("at")
        if handler is None:
            raise HTTPException(status_code=503, detail="AT service is unavailable")
        try:
            result = handler(command, timeout_ms)
            if hasattr(result, "__await__"):
                result = await result
            return result
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/v1/module/adb/authorize")
    async def authorize(payload: Dict[str, Any], _: str = Depends(require_token)):
        if payload.get("confirm") is not True:
            raise HTTPException(status_code=409, detail="explicit authorization confirmation required")
        handler = state.get("authorize_adb")
        if handler is None:
            raise HTTPException(status_code=503, detail="ADB authorization service is unavailable")
        try:
            result = handler()
            if hasattr(result, "__await__"):
                result = await result
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"authorized": bool(result)}

    @app.get("/api/v1/events")
    async def event_stream(_: str = Depends(require_token)):
        return StreamingResponse(
            sse_event_stream(events),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.websocket("/api/v1/calls/{call_id}/audio")
    async def web_audio(websocket: WebSocket, call_id: str):
        ticket = extract_audio_ticket(websocket.headers.get("sec-websocket-protocol"))
        consume = state.get("consume_audio_ticket")
        runner = state.get("run_audio_websocket")
        accepted = False
        if ticket is not None and consume is not None:
            accepted = consume(call_id, ticket)
            if hasattr(accepted, "__await__"):
                accepted = await accepted
        if not accepted or runner is None:
            await websocket.close(code=4401, reason="invalid or expired audio ticket")
            return
        await websocket.accept(subprotocol=AUDIO_SUBPROTOCOL)
        try:
            result = runner(websocket, call_id)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            detail = public_call_error(exc)
            await events.publish(GatewayEvent("audio.websocket_error", {
                "call_id": call_id,
                "error": type(exc).__name__,
                "detail": detail,
            }))
            try:
                await websocket.send_json({
                    "type": "error",
                    "detail": f"web audio session failed: {detail}",
                })
            except Exception:
                pass
            try:
                await websocket.close(code=1011, reason="audio session failed")
            except Exception:
                pass

    @app.websocket("/api/v1/audio/diagnostic/{session_id}")
    async def diagnostic_audio(websocket: WebSocket, session_id: str):
        ticket = extract_audio_ticket(websocket.headers.get("sec-websocket-protocol"))
        consume = state.get("consume_audio_diagnostic_ticket")
        runner = state.get("run_audio_diagnostic_websocket")
        accepted = False
        if ticket is not None and consume is not None:
            accepted = consume(session_id, ticket)
            if hasattr(accepted, "__await__"):
                accepted = await accepted
        if not accepted or runner is None:
            await websocket.close(code=4401, reason="invalid or expired audio ticket")
            return
        await websocket.accept(subprotocol=AUDIO_SUBPROTOCOL)
        try:
            result = runner(websocket, session_id)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            await events.publish(GatewayEvent("audio.diagnostic_error", {
                "session_id": session_id,
                "error": type(exc).__name__,
            }))
            try:
                await websocket.send_json({
                    "type": "error",
                    "detail": "audio diagnostic session failed",
                })
            except Exception:
                pass
            try:
                await websocket.close(code=1011, reason="audio diagnostic failed")
            except Exception:
                pass

    static_dir = Path(__file__).resolve().parents[1] / "web" / "static"
    if static_dir.is_dir():
        @app.get("/", include_in_schema=False)
        async def web_root():
            return RedirectResponse(url="/web/")

        app.mount("/web", StaticFiles(directory=static_dir, html=True), name="web")

    return app
