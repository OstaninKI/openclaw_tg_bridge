"""HTTP API server for the bridge."""

import asyncio
import hmac
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from openclaw_tg_bridge.client import BridgeClient, BridgeError, _normalize_peer, _serialize_message
from openclaw_tg_bridge.config import (
    PolicyStore,
    load_config,
    parse_request_overrides,
    resolve_effective_policy,
    resolve_session_path,
)
from openclaw_tg_bridge.state import DmCursorStore, SourceInventoryStore

logger = logging.getLogger(__name__)

# Global bridge client (set in lifespan)
_bridge: BridgeClient | None = None
_config: dict | None = None
_policy_store: PolicyStore | None = None
_sources_store: SourceInventoryStore | None = None
_dm_cursor_store: DmCursorStore | None = None
_dm_broker: "DmInboxBroker | None" = None
_resolved_peer_cache: "ResolvedPeerCache | None" = None


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


def get_sources_store() -> SourceInventoryStore:
    if _sources_store is None:
        raise RuntimeError("Sources store not loaded")
    return _sources_store


def get_dm_cursor_store() -> DmCursorStore:
    if _dm_cursor_store is None:
        raise RuntimeError("DM cursor store not loaded")
    return _dm_cursor_store


def get_dm_broker() -> "DmInboxBroker":
    if _dm_broker is None:
        raise RuntimeError("DM broker not loaded")
    return _dm_broker


def get_resolved_peer_cache() -> "ResolvedPeerCache":
    if _resolved_peer_cache is None:
        raise RuntimeError("Resolved peer cache not loaded")
    return _resolved_peer_cache


@dataclass(frozen=True)
class InboundDmEvent:
    sender_key: str
    message_id: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class AllowedDmSender:
    peer_ref: str
    cursor_key: str
    match_keys: frozenset[str]


class ResolvedPeerCache:
    def __init__(self, ttl_sec: float = 300.0) -> None:
        self._ttl_sec = max(1.0, ttl_sec)
        self._entries: dict[str, tuple[float, dict[str, str | None]]] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, bridge: BridgeClient, peer: str | int) -> dict[str, str | None]:
        normalized = _normalize_peer(peer)
        if normalized and normalized not in {"*", "me"} and normalized.lstrip("-").isdigit():
            return {
                "peer": normalized,
                "id": normalized,
                "username": None,
            }

        cache_key = normalized or str(peer).strip()
        now = time.monotonic()
        async with self._lock:
            cached = self._entries.get(cache_key)
            if cached and cached[0] > now:
                return dict(cached[1])
            identifiers = await bridge.resolve_peer_identifiers(peer)
            self._entries[cache_key] = (now + self._ttl_sec, dict(identifiers))
            return dict(identifiers)


class DmInboxBroker:
    def __init__(self, max_events: int = 500) -> None:
        self._events: deque[InboundDmEvent] = deque(maxlen=max_events)
        self._condition = asyncio.Condition()

    async def push(self, payload: dict[str, Any]) -> None:
        sender_id = payload.get("sender_id")
        message_id = payload.get("id")
        sender_key = _normalize_peer(sender_id)
        if not sender_key or not isinstance(message_id, int):
            return
        async with self._condition:
            self._events.append(InboundDmEvent(sender_key=sender_key, message_id=message_id, payload=payload))
            self._condition.notify_all()

    async def wait_for_new_events(self, timeout_sec: float) -> None:
        timeout_sec = max(0.0, timeout_sec)
        if timeout_sec <= 0:
            return
        try:
            async with asyncio.timeout(timeout_sec):
                async with self._condition:
                    await self._condition.wait()
        except TimeoutError:
            return

    def list_pending(
        self,
        *,
        allowed_senders: list[AllowedDmSender],
        cursor_map: dict[str, int],
        limit: int,
    ) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        allowed_matches = {
            match_key
            for sender in allowed_senders
            for match_key in sender.match_keys
            if match_key
        }
        for event in self._events:
            payload_match_keys = {
                _normalize_peer(event.payload.get("sender_id")),
                _normalize_peer(event.payload.get("sender_username")),
            }
            if not payload_match_keys & allowed_matches:
                continue
            if event.message_id <= cursor_map.get(event.sender_key, 0):
                continue
            pending.append(dict(event.payload))
            if len(pending) >= limit:
                break
        pending.sort(key=lambda item: (int(item.get("date_unix", 0)), int(item.get("id", 0))))
        return pending

    def prune_acked(self, cursor_map: dict[str, int]) -> None:
        self._events = deque(
            [
                event
                for event in self._events
                if event.message_id > cursor_map.get(event.sender_key, 0)
            ],
            maxlen=self._events.maxlen,
        )


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
    global _bridge, _config, _policy_store, _sources_store, _dm_cursor_store, _dm_broker, _resolved_peer_cache
    from telethon import events

    _config = load_config()
    _policy_store = PolicyStore(_config.get("policy_path"))
    _sources_store = SourceInventoryStore(_config.get("sources_inventory_path"))
    _dm_cursor_store = DmCursorStore(_config.get("inbox_state_path"))
    _dm_broker = DmInboxBroker()
    _resolved_peer_cache = ResolvedPeerCache()
    try:
        _bridge = await _create_client()

        async def _on_new_dm(event: Any) -> None:
            if not getattr(event, "is_private", False):
                return
            message = getattr(event, "message", None)
            if message is None or getattr(message, "out", False):
                return
            sender = getattr(message, "sender", None)
            if sender is None:
                for resolver_name in ("get_sender",):
                    resolver = getattr(message, resolver_name, None) or getattr(event, resolver_name, None)
                    if not callable(resolver):
                        continue
                    try:
                        resolved = resolver()
                        sender = await resolved if hasattr(resolved, "__await__") else resolved
                    except Exception:
                        logger.debug("Unable to resolve sender for inbound DM", exc_info=True)
                        sender = None
                    if sender is not None:
                        break
            payload = _serialize_message(message, entity=sender or getattr(event, "chat", None))
            payload["chat_type"] = "direct"
            if payload.get("chat_title") is None:
                payload["chat_title"] = payload.get("sender_name")
            await get_dm_broker().push(payload)

        _bridge.client.add_event_handler(_on_new_dm, events.NewMessage(incoming=True))
        logger.info("Bridge client connected")
        yield
    finally:
        if _bridge:
            await _bridge.disconnect()
            _bridge = None
        _config = None
        _policy_store = None
        _sources_store = None
        _dm_cursor_store = None
        _dm_broker = None
        _resolved_peer_cache = None


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


class SyncSourcesBody(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=2000)


class AckDmInboxBody(BaseModel):
    sender_id: str | int = Field(..., description="Sender id or normalized peer key")
    sender_username: str | None = None
    message_id: int = Field(..., ge=1)


def _dedupe_peers(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = _normalize_peer(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(str(value))
    return deduped


async def _sync_sources_inventory(*, force: bool = False, limit: int | None = None) -> dict:
    store = get_sources_store()
    cfg = get_config()
    if not force and not store.needs_refresh(cfg["sources_refresh_sec"]):
        return store.load()

    bridge = get_bridge()
    dialogs = await bridge.discover_source_dialogs(limit=limit or cfg["sources_dialog_limit"])
    return store.replace_dialogs(dialogs)


def _source_entry_matches_policy(entry: dict, policy: dict) -> bool:
    deny_keys = {
        _normalize_peer(value)
        for value in (policy.get("read_deny_chat_ids") or [])
        if _normalize_peer(value)
    }
    for value in (entry.get("peer_id"), entry.get("username")):
        normalized = _normalize_peer(value)
        if normalized and normalized in deny_keys:
            return False
    return True


async def _apply_source_discovery(policy: dict) -> dict:
    policy = dict(policy)
    if not policy.get("sources_auto_discover"):
        return policy

    await _sync_sources_inventory(force=False)
    store = get_sources_store()
    identifiers = store.allowed_identifiers(
        include_types=policy.get("sources_include_types"),
        exclude_peers=policy.get("sources_exclude_peers"),
        exclude_usernames=policy.get("sources_exclude_usernames"),
    )
    if policy.get("read_allow_chat_ids") is None:
        policy["read_allow_chat_ids"] = identifiers
    else:
        policy["read_allow_chat_ids"] = _dedupe_peers(
            list(policy.get("read_allow_chat_ids") or []) + identifiers
        )
    return policy


async def resolve_request_policy(request: Request) -> dict:
    overrides = parse_request_overrides(request.headers)
    policy = resolve_effective_policy(get_config(), get_policy_store(), overrides)
    return await _apply_source_discovery(policy)


def _resolve_dm_consumer_id(policy: dict) -> str:
    profile = policy.get("policy_profile")
    return str(profile).strip() if profile else "default"


async def _resolve_allowed_dm_senders(bridge: BridgeClient, policy: dict) -> list[AllowedDmSender]:
    allow = policy.get("read_allow_chat_ids")
    if allow is None:
        raise BridgeError(
            "DM inbox requires an explicit read allowlist with sender ids/usernames.",
            status_code=400,
        )
    senders: list[AllowedDmSender] = []
    seen_cursor_keys: set[str] = set()
    for value in allow:
        normalized = _normalize_peer(value)
        if normalized in {"", "*", "me"}:
            continue
        identifiers = await get_resolved_peer_cache().resolve(bridge, value)
        cursor_key = identifiers.get("id") or identifiers.get("peer")
        if not cursor_key or cursor_key in seen_cursor_keys:
            continue
        match_keys = frozenset(
            item
            for item in (identifiers.get("id"), identifiers.get("username"), identifiers.get("peer"))
            if item
        )
        senders.append(
            AllowedDmSender(
                peer_ref=str(value),
                cursor_key=cursor_key,
                match_keys=match_keys,
            )
        )
        seen_cursor_keys.add(cursor_key)
    if not senders:
        raise BridgeError(
            "DM inbox requires at least one explicit allowed sender.",
            status_code=400,
        )
    return senders


async def _recover_dm_events(
    *,
    bridge: BridgeClient,
    policy: dict,
    allowed_senders: list[AllowedDmSender],
    cursor_map: dict[str, int],
    limit: int,
) -> list[dict[str, Any]]:
    recovered: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for sender in allowed_senders:
        min_id = cursor_map.get(sender.cursor_key, 0)
        messages = await bridge.get_incoming_direct_messages(
            sender.peer_ref,
            min_id=min_id,
            limit=limit,
            policy_overrides=policy,
        )
        for message in messages:
            sender_id = _normalize_peer(message.get("sender_id") or sender.cursor_key)
            message_id = int(message.get("id") or 0)
            if not sender_id or message_id <= 0:
                continue
            dedupe_key = (sender_id, message_id)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            recovered.append(message)
            if len(recovered) >= limit:
                recovered.sort(key=lambda item: (int(item.get("date_unix", 0)), int(item.get("id", 0))))
                return recovered
    recovered.sort(key=lambda item: (int(item.get("date_unix", 0)), int(item.get("id", 0))))
    return recovered


@app.post("/send_message")
async def send_message(request: Request, body: SendMessageBody):
    bridge = get_bridge()
    try:
        overrides = await resolve_request_policy(request)
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
        overrides = await resolve_request_policy(request)
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


@app.get("/topics")
async def topics(request: Request, peer: str | int, limit: int = 20):
    bridge = get_bridge()
    try:
        overrides = await resolve_request_policy(request)
        data = await bridge.list_topics(
            peer,
            limit=min(max(1, limit), 100),
            policy_overrides=overrides,
        )
        return {"topics": data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers) from exc
    except Exception:
        logger.exception("list_topics failed")
        raise HTTPException(status_code=502, detail="Request failed")


@app.get("/messages")
async def messages(
    request: Request,
    peer: str | int,
    limit: int = 20,
    min_id: int | None = None,
    topic_id: int | None = None,
    since_unix: int | None = None,
):
    bridge = get_bridge()
    try:
        overrides = await resolve_request_policy(request)
        data = await bridge.get_messages(
            peer,
            limit=min(max(1, limit), 50),
            min_id=min_id,
            topic_id=topic_id,
            since_unix=since_unix,
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


@app.get("/dm/inbox/poll")
async def poll_dm_inbox(request: Request, timeout_ms: int = 25000, limit: int = 10):
    bridge = get_bridge()
    try:
        policy = await resolve_request_policy(request)
        allowed_senders = await _resolve_allowed_dm_senders(bridge, policy)
        consumer_id = _resolve_dm_consumer_id(policy)
        cursor_store = get_dm_cursor_store()
        cursor_map = cursor_store.get_consumer_cursors(consumer_id)
        broker = get_dm_broker()

        events = broker.list_pending(
            allowed_senders=allowed_senders,
            cursor_map=cursor_map,
            limit=min(max(1, limit), 50),
        )
        if not events:
            events = await _recover_dm_events(
                bridge=bridge,
                policy=policy,
                allowed_senders=allowed_senders,
                cursor_map=cursor_map,
                limit=min(max(1, limit), 50),
            )
        if not events:
            await broker.wait_for_new_events(timeout_ms / 1000.0)
            cursor_map = cursor_store.get_consumer_cursors(consumer_id)
            events = broker.list_pending(
                allowed_senders=allowed_senders,
                cursor_map=cursor_map,
                limit=min(max(1, limit), 50),
            )

        return {"events": events, "consumer_id": consumer_id}
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("poll_dm_inbox failed")
        raise HTTPException(status_code=502, detail="Request failed")


@app.post("/dm/inbox/ack")
async def ack_dm_inbox(request: Request, body: AckDmInboxBody):
    bridge = get_bridge()
    try:
        policy = await resolve_request_policy(request)
        allowed_senders = await _resolve_allowed_dm_senders(bridge, policy)
        sender_key = _normalize_peer(body.sender_id)
        sender_username = _normalize_peer(body.sender_username)
        allowed = next(
            (
                sender
                for sender in allowed_senders
                if sender_key in sender.match_keys or (sender_username and sender_username in sender.match_keys)
            ),
            None,
        )
        if allowed is None:
            raise HTTPException(status_code=403, detail="Ack is not allowed for this sender.")

        consumer_id = _resolve_dm_consumer_id(policy)
        cursor_store = get_dm_cursor_store()
        acknowledged = cursor_store.ack(consumer_id, allowed.cursor_key, body.message_id)
        get_dm_broker().prune_acked(cursor_store.get_consumer_cursors(consumer_id))
        return {
            "ok": True,
            "consumer_id": consumer_id,
            "sender_id": allowed.cursor_key,
            "acked_message_id": acknowledged,
        }
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers) from exc
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("ack_dm_inbox failed")
        raise HTTPException(status_code=502, detail="Request failed")


@app.get("/sources")
async def list_sources(request: Request, refresh: bool = False):
    try:
        policy = await resolve_request_policy(request)
        data = await _sync_sources_inventory(force=refresh)
        dialogs = get_sources_store().list_dialogs(
            include_types=policy.get("sources_include_types"),
            exclude_peers=policy.get("sources_exclude_peers"),
            exclude_usernames=policy.get("sources_exclude_usernames"),
            only_sourceable=True,
        )
        sources = [dialog for dialog in dialogs if _source_entry_matches_policy(dialog, policy)]
        return {"sources": sources, "meta": data.get("meta", {})}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers) from exc
    except Exception:
        logger.exception("list_sources failed")
        raise HTTPException(status_code=502, detail="Request failed")


@app.post("/sources/sync")
async def sync_sources(request: Request, body: SyncSourcesBody):
    try:
        policy = await resolve_request_policy(request)
        data = await _sync_sources_inventory(force=True, limit=body.limit)
        dialogs = get_sources_store().list_dialogs(
            include_types=policy.get("sources_include_types"),
            exclude_peers=policy.get("sources_exclude_peers"),
            exclude_usernames=policy.get("sources_exclude_usernames"),
            only_sourceable=True,
        )
        sources = [dialog for dialog in dialogs if _source_entry_matches_policy(dialog, policy)]
        return {"sources": sources, "meta": data.get("meta", {})}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BridgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=exc.headers) from exc
    except Exception:
        logger.exception("sync_sources failed")
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
