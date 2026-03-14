"""HTTP API server for the bridge."""

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from openclaw_tg_bridge.client import BridgeClient, BridgeError
from openclaw_tg_bridge.config import (
    PolicyStore,
    load_config,
    parse_request_overrides,
    resolve_effective_policy,
    resolve_session_path,
)

logger = logging.getLogger(__name__)

# Global bridge client (set in lifespan)
_bridge: BridgeClient | None = None
_config: dict | None = None
_policy_store: PolicyStore | None = None


def get_bridge() -> BridgeClient:
    if _bridge is None:
        raise RuntimeError("Bridge not initialized")
    return _bridge


def get_config() -> dict:
    if _config is None:
        raise RuntimeError("Config not loaded")
    return _config


def get_policy_store() -> PolicyStore:
    if _policy_store is None:
        raise RuntimeError("Policy store not loaded")
    return _policy_store


async def _create_client() -> BridgeClient:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    cfg = get_config()
    api_id = cfg["api_id"]
    api_hash = cfg["api_hash"]
    if not api_id or not api_hash:
        raise ValueError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")

    session_string = cfg.get("session_string")
    if session_string:
        session = StringSession(session_string)
    else:
        path = resolve_session_path(cfg.get("session_path"))
        if not path:
            raise ValueError("TELEGRAM_SESSION_PATH or TELEGRAM_SESSION_STRING is required")
        session = str(path.with_suffix(""))  # Telethon adds .session

    proxy = cfg.get("proxy")
    if proxy and proxy[0] == "mtproxy":
        from telethon import connection
        # proxy = ("mtproxy", host, port, secret)
        client = TelegramClient(
            session,
            api_id,
            api_hash,
            connection=connection.ConnectionTcpMTProxyRandomizedIntermediate,
            proxy=(proxy[1], proxy[2], proxy[3]),
        )
    else:
        client = TelegramClient(
            session,
            api_id,
            api_hash,
            proxy=proxy if proxy else None,
        )
    client.flood_sleep_threshold = 0
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ValueError("Session not authorized. Create session locally with: python -m openclaw_tg_bridge.auth")

    return BridgeClient(
        client,
        reply_delay_sec=cfg["reply_delay_sec"],
        reply_delay_max_sec=cfg.get("reply_delay_max_sec"),
        allow_chat_ids=cfg["allow_chat_ids"] or None,
        deny_chat_ids=cfg["deny_chat_ids"] or None,
        write_allow_chat_ids=cfg["write_allow_chat_ids"] or None,
        write_deny_chat_ids=cfg["write_deny_chat_ids"] or None,
        rpc_timeout_sec=cfg["rpc_timeout_sec"],
        flood_wait_max_sleep_sec=cfg["flood_wait_max_sleep_sec"],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bridge, _config, _policy_store
    _config = load_config()
    _policy_store = PolicyStore(_config.get("policy_path"))
    try:
        _bridge = await _create_client()
        logger.info("Bridge client connected")
        yield
    finally:
        if _bridge:
            await _bridge.disconnect()
            _bridge = None
        _config = None
        _policy_store = None


app = FastAPI(title="OpenClaw Telegram Bridge", lifespan=lifespan)


def _check_auth(request: Request, api_token: str | None) -> None:
    if not api_token:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth[7:].strip()
    if not hmac.compare_digest(token, api_token):
        raise HTTPException(status_code=401, detail="Invalid token")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    cfg = get_config()
    token = cfg.get("api_token")
    if token:
        try:
            _check_auth(request, token)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    return await call_next(request)


class SendMessageBody(BaseModel):
    peer: str | int = Field(..., description="Username (@name), chat id, or 'me'")
    text: str = Field(..., min_length=1, max_length=4096)
    reply_to: int | None = None


def resolve_request_policy(request: Request) -> dict:
    overrides = parse_request_overrides(request.headers)
    return resolve_effective_policy(get_config(), get_policy_store(), overrides)


@app.post("/send_message")
async def send_message(request: Request, body: SendMessageBody):
    bridge = get_bridge()
    try:
        overrides = resolve_request_policy(request)
        result = await bridge.send_message(
            body.peer,
            body.text,
            reply_to=body.reply_to,
            policy_overrides=overrides,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers) from exc
    except Exception:
        logger.exception("send_message failed")
        raise HTTPException(status_code=502, detail="Request failed")


@app.get("/me")
async def me():
    bridge = get_bridge()
    try:
        data = await bridge.get_me()
        return data
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers) from exc
    except Exception:
        logger.exception("get_me failed")
        raise HTTPException(status_code=502, detail="Request failed")


@app.get("/dialogs")
async def dialogs(request: Request, limit: int = 20):
    bridge = get_bridge()
    try:
        overrides = resolve_request_policy(request)
        data = await bridge.get_dialogs(
            limit=min(max(1, limit), 50),
            policy_overrides=overrides,
        )
        return {"dialogs": data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers) from exc
    except Exception:
        logger.exception("get_dialogs failed")
        raise HTTPException(status_code=502, detail="Request failed")


@app.get("/messages")
async def messages(request: Request, peer: str | int, limit: int = 20, min_id: int | None = None):
    bridge = get_bridge()
    try:
        overrides = resolve_request_policy(request)
        data = await bridge.get_messages(
            peer,
            limit=min(max(1, limit), 50),
            min_id=min_id,
            policy_overrides=overrides,
        )
        return {"messages": data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers) from exc
    except Exception:
        logger.exception("get_messages failed")
        raise HTTPException(status_code=502, detail="Request failed")


@app.get("/health")
async def health():
    """Liveness: 200 if process is up. Does not require auth."""
    bridge = get_bridge()
    try:
        if await bridge.ensure_connected():
            return {"status": "ok"}
        return JSONResponse(status_code=503, content={"status": "disconnected"})
    except Exception:
        return JSONResponse(status_code=503, content={"status": "error"})
