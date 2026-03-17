"""Telethon client wrapper with profile-aware access policies and error mapping."""

import asyncio
import inspect
import logging
import random
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import urlparse

from openclaw_tg_bridge.state import dialog_to_inventory_entry

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from telethon import TelegramClient
else:
    TelegramClient = Any

MAX_MESSAGE_LENGTH = 4096
MAX_SEND_CHUNKS = 20
OBSERVED_PEER_CACHE_SIZE = 512
MAX_CONTACT_VCARD_LENGTH = 512
MAX_TRANSCRIPTION_POLL_ATTEMPTS = 3
TRANSCRIPTION_POLL_INTERVAL_SEC = 1.5


class BridgeError(Exception):
    """Domain error that can be translated to an HTTP response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int = 502,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.headers = headers or {}


class BridgeValidationError(BridgeError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail, status_code=400)


class BridgeForbiddenError(BridgeError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail, status_code=403)


class BridgeUnavailableError(BridgeError):
    def __init__(self, detail: str = "Telegram bridge is temporarily unavailable.") -> None:
        super().__init__(detail, status_code=503)


class BridgeTimeoutError(BridgeError):
    def __init__(self, detail: str = "Telegram API did not respond in time.") -> None:
        super().__init__(detail, status_code=504)


class BridgeRateLimitError(BridgeError):
    def __init__(self, retry_after: int, detail: str | None = None) -> None:
        retry_after = max(1, int(retry_after))
        super().__init__(
            detail or f"Telegram rate limit hit. Retry after {retry_after}s.",
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
        self.retry_after = retry_after


@dataclass(frozen=True)
class BridgeScope:
    allow_all: bool
    allow: frozenset[str]
    deny: frozenset[str]

    def as_allow_input(self) -> list[str]:
        if self.allow_all:
            return ["*"]
        return sorted(self.allow)

    def as_deny_input(self) -> list[str]:
        return sorted(self.deny)


@dataclass(frozen=True)
class BridgePolicy:
    read_scope: BridgeScope
    write_scope: BridgeScope
    reply_delay_sec: float
    reply_delay_max_sec: float | None


def _normalize_peer(peer: str | int) -> str:
    """Normalize peer for allow/deny check: username (no @, lowercase) or canonical id."""
    value = str(peer).strip()
    if not value:
        return ""
    if value == "*":
        return "*"
    if value.lower() == "me":
        return "me"
    if value.startswith("@"):
        value = value[1:]
    if value.lstrip("-").isdigit():
        try:
            return str(int(value))
        except ValueError:
            return value
    return value.lower()


def _normalize_peer_list(peers: Iterable[str] | None) -> frozenset[str]:
    return frozenset(
        normalized
        for peer in (peers or [])
        if (normalized := _normalize_peer(peer))
    )


def _entity_display_name(entity: Any | None) -> str | None:
    if entity is None:
        return None
    title = getattr(entity, "title", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    first_name = getattr(entity, "first_name", None) or ""
    last_name = getattr(entity, "last_name", None) or ""
    full_name = f"{first_name} {last_name}".strip()
    if full_name:
        return full_name
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return None


def _display_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    text_attr = getattr(value, "text", None)
    if isinstance(text_attr, str) and text_attr.strip():
        return text_attr.strip()
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _telethon_functions() -> Any:
    from telethon import functions

    return functions


def _telethon_types() -> Any:
    from telethon import types

    return types


def _isoformat(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _datetime_unix(value: Any) -> int | None:
    if isinstance(value, datetime):
        return int(value.timestamp())
    return None


def _message_topic_id(message: Any) -> int | None:
    for attr in ("reply_to_top_id", "topic_id", "top_msg_id"):
        value = getattr(message, attr, None)
        if isinstance(value, int):
            return value
    reply_to = getattr(message, "reply_to", None)
    if reply_to is None:
        return None
    for attr in ("reply_to_top_id", "top_msg_id"):
        value = getattr(reply_to, attr, None)
        if isinstance(value, int):
            return value
    return None


def _extract_peer_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if value is None:
        return None
    for attr in ("user_id", "chat_id", "channel_id", "id"):
        candidate = getattr(value, attr, None)
        if isinstance(candidate, int):
            return candidate
    return None


def _message_sender_name(message: Any, *, sender: Any | None = None) -> str | None:
    sender = sender or getattr(message, "sender", None)
    if sender is not None:
        sender_name = _entity_display_name(sender)
        if sender_name:
            return sender_name
    post_author = getattr(message, "post_author", None)
    if isinstance(post_author, str) and post_author.strip():
        return post_author.strip()
    return None


def _split_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks of at most max_len chars, preferring logical break points."""
    if len(text) <= max_len:
        return [text]
    # Ordered preference for break points
    break_seqs = ["\n\n", "\n", ". ", "! ", "? ", " "]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        split_at = -1
        for seq in break_seqs:
            pos = remaining.rfind(seq, 0, max_len)
            if pos > 0:
                split_at = pos + len(seq)
                break
        if split_at <= 0:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _reaction_label(reaction: Any) -> str | None:
    if reaction is None:
        return None
    emoticon = getattr(reaction, "emoticon", None)
    if isinstance(emoticon, str) and emoticon.strip():
        return emoticon
    document_id = getattr(reaction, "document_id", None)
    if isinstance(document_id, int):
        return f"custom:{document_id}"
    return None


def _serialize_message_entities(message: Any) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    text = getattr(message, "text", None) or getattr(message, "message", None) or ""
    for entity in getattr(message, "entities", None) or []:
        item = {
            "type": type(entity).__name__,
            "offset": getattr(entity, "offset", None),
            "length": getattr(entity, "length", None),
            "text": "",
        }
        offset = item["offset"]
        length = item["length"]
        if isinstance(offset, int) and isinstance(length, int) and length > 0:
            item["text"] = text[offset : offset + length]
        if getattr(entity, "url", None):
            item["url"] = getattr(entity, "url", None)
        user_id = _extract_peer_id(getattr(entity, "user_id", None))
        if user_id is not None:
            item["user_id"] = user_id
        entities.append(item)
    return entities


def _is_voice_note(message: Any) -> bool:
    """Return True if the message contains a Telegram voice note."""
    try:
        types = _telethon_types()
        media = getattr(message, "media", None)
        document = getattr(media, "document", None)
        if document is None:
            return False
        for attr in getattr(document, "attributes", []):
            if isinstance(attr, types.DocumentAttributeAudio) and getattr(attr, "voice", False):
                return True
    except Exception:
        pass
    return False


def _is_video_note(message: Any) -> bool:
    """Return True if the message contains a Telegram video circle (round video)."""
    try:
        types = _telethon_types()
        media = getattr(message, "media", None)
        document = getattr(media, "document", None)
        if document is None:
            return False
        for attr in getattr(document, "attributes", []):
            if isinstance(attr, types.DocumentAttributeVideo) and getattr(attr, "round_message", False):
                return True
    except Exception:
        pass
    return False


def _extract_media_summary(message: Any, *, premium: bool | None = None) -> dict[str, Any]:
    media = getattr(message, "media", None)
    file = getattr(message, "file", None)
    summary: dict[str, Any] = {
        "has_media": bool(media),
        "media_type": type(media).__name__ if media is not None else None,
        "mime_type": getattr(file, "mime_type", None),
        "file_size": getattr(file, "size", None),
        "file_name": getattr(file, "name", None),
    }
    if premium is not None and (_is_voice_note(message) or _is_video_note(message)):
        summary["can_transcribe"] = bool(premium)
    return summary


def _extract_geo_summary(message: Any) -> dict[str, Any]:
    media = getattr(message, "media", None)
    geo = getattr(media, "geo", None) or getattr(message, "geo", None)
    point = getattr(media, "geo", None)
    result: dict[str, Any] = {
        "latitude": getattr(geo, "lat", None),
        "longitude": getattr(geo, "long", None),
        "venue_title": getattr(media, "title", None),
        "venue_address": getattr(media, "address", None),
        "venue_provider": getattr(media, "provider", None),
        "venue_id": getattr(media, "venue_id", None),
    }
    if point is None and getattr(message, "geo", None) is None:
        result["latitude"] = None
        result["longitude"] = None
    return result


def _extract_contact_summary(message: Any) -> dict[str, Any]:
    media = getattr(message, "media", None)
    raw_vcard = getattr(media, "vcard", None)
    contact_vcard = raw_vcard[:MAX_CONTACT_VCARD_LENGTH] if isinstance(raw_vcard, str) else raw_vcard
    return {
        "contact_phone": getattr(media, "phone_number", None),
        "contact_first_name": getattr(media, "first_name", None),
        "contact_last_name": getattr(media, "last_name", None),
        "contact_user_id": _extract_peer_id(getattr(media, "user_id", None)),
        "contact_vcard": contact_vcard,
    }


def _resolve_chat_type(entity: Any | None) -> str:
    if entity is None:
        return "unknown"
    if getattr(entity, "forum", False):
        return "forum"
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False):
        return "supergroup"
    if getattr(entity, "title", None) is not None:
        return "group"
    if getattr(entity, "first_name", None) is not None or getattr(entity, "last_name", None) is not None:
        return "direct"
    return "unknown"


def _is_channel_like(entity: Any | None) -> bool:
    return bool(getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False))


def _is_basic_group(entity: Any | None) -> bool:
    return not _is_channel_like(entity) and getattr(entity, "title", None) is not None


def _is_basic_group_admin_participant(participant: Any) -> bool:
    participant_type = type(participant).__name__.lower()
    return "admin" in participant_type or "creator" in participant_type


def _build_sender_lookup(result: Any) -> dict[str, Any]:
    lookup: dict[str, Any] = {}
    for collection_name in ("users", "chats"):
        collection = getattr(result, collection_name, None) or []
        for item in collection:
            normalized = _normalize_peer(getattr(item, "id", None))
            if normalized:
                lookup[normalized] = item
    return lookup


def _resolve_message_sender(message: Any, sender_lookup: dict[str, Any] | None = None) -> Any | None:
    sender = getattr(message, "sender", None)
    if sender is not None or not sender_lookup:
        return sender
    sender_id = getattr(message, "sender_id", None)
    if not isinstance(sender_id, int):
        sender_id = _extract_peer_id(getattr(message, "from_id", None))
    normalized = _normalize_peer(sender_id)
    if not normalized:
        return None
    return sender_lookup.get(normalized)


def _serialize_message(
    message: Any,
    *,
    entity: Any | None = None,
    sender_lookup: dict[str, Any] | None = None,
    topic_id_override: int | None = None,
    premium: bool | None = None,
) -> dict[str, Any]:
    sender = _resolve_message_sender(message, sender_lookup)
    chat_entity = entity or getattr(message, "chat", None) or getattr(message, "sender", None)
    date_value = getattr(message, "date", None)
    sender_id = getattr(message, "sender_id", None)
    if not isinstance(sender_id, int):
        sender_id = _extract_peer_id(getattr(message, "from_id", None))
    topic_id = topic_id_override if isinstance(topic_id_override, int) and topic_id_override > 0 else _message_topic_id(message)
    payload = {
        "id": getattr(message, "id", None),
        "text": getattr(message, "text", None) or "",
        "date": _isoformat(date_value),
        "date_unix": _datetime_unix(date_value) or 0,
        "out": getattr(message, "out", None),
        "sender_id": sender_id,
        "sender_name": _message_sender_name(message, sender=sender),
        "sender_username": getattr(sender, "username", None) or getattr(getattr(message, "sender", None), "username", None),
        "chat_id": getattr(chat_entity, "id", None),
        "chat_title": _entity_display_name(chat_entity),
        "chat_username": getattr(chat_entity, "username", None),
        "chat_type": _resolve_chat_type(chat_entity),
        "topic_id": topic_id,
        "reply_to_message_id": getattr(message, "reply_to_msg_id", None),
        "grouped_id": getattr(message, "grouped_id", None),
    }
    payload.update(_extract_media_summary(message, premium=premium))
    payload.update(_extract_geo_summary(message))
    payload.update(_extract_contact_summary(message))
    entities = _serialize_message_entities(message)
    if entities:
        payload["entities"] = entities
    return payload


def _serialize_messages(
    messages: Iterable[Any],
    *,
    entity: Any | None = None,
    sender_lookup: dict[str, Any] | None = None,
    skip_outbound: bool = False,
    topic_id_override: int | None = None,
    premium: bool | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in reversed(list(messages)):
        if message is None or getattr(message, "id", None) is None:
            continue
        if skip_outbound and getattr(message, "out", False):
            continue
        out.append(
            _serialize_message(
                message,
                entity=entity,
                sender_lookup=sender_lookup,
                topic_id_override=topic_id_override,
                premium=premium,
            )
        )
    return out


def _serialize_topic(topic: Any) -> dict[str, Any] | None:
    title = getattr(topic, "title", None)
    if not isinstance(title, str) or not title.strip():
        return None
    topic_id = getattr(topic, "top_message", None)
    if not isinstance(topic_id, int):
        topic_id = getattr(topic, "id", None)
    if not isinstance(topic_id, int):
        return None
    return {
        "id": getattr(topic, "id", None),
        "topic_id": topic_id,
        "title": title.strip(),
        "icon_color": getattr(topic, "icon_color", None),
        "icon_emoji_id": getattr(topic, "icon_emoji_id", None),
        "closed": bool(getattr(topic, "closed", False)),
        "hidden": bool(getattr(topic, "hidden", False)),
        "pinned": bool(getattr(topic, "pinned", False)),
        "unread_count": getattr(topic, "unread_count", None),
        "unread_mentions_count": getattr(topic, "unread_mentions_count", None),
        "unread_reactions_count": getattr(topic, "unread_reactions_count", None),
        "from_id": _extract_peer_id(getattr(topic, "from_id", None)),
        "date": _isoformat(getattr(topic, "date", None)),
    }


def build_scope(
    allow_peers: list[str] | None,
    deny_peers: list[str] | None,
    *,
    default_allow_all: bool,
) -> BridgeScope:
    allow = _normalize_peer_list(allow_peers)
    deny = _normalize_peer_list(deny_peers)

    if allow_peers is None:
        allow_all = default_allow_all
        allow = frozenset()
    else:
        allow_all = "*" in allow
        allow = frozenset(item for item in allow if item != "*")

    return BridgeScope(
        allow_all=allow_all,
        allow=allow,
        deny=deny,
    )


def build_policy(
    *,
    read_allow_chat_ids: list[str] | None,
    read_deny_chat_ids: list[str] | None,
    write_allow_chat_ids: list[str] | None,
    write_deny_chat_ids: list[str] | None,
    reply_delay_sec: float,
    reply_delay_max_sec: float | None,
) -> BridgePolicy:
    delay = max(0.0, reply_delay_sec)
    delay_max = reply_delay_max_sec
    if delay_max is not None and delay_max <= delay:
        delay_max = None
    return BridgePolicy(
        read_scope=build_scope(read_allow_chat_ids, read_deny_chat_ids, default_allow_all=True),
        write_scope=build_scope(write_allow_chat_ids, write_deny_chat_ids, default_allow_all=False),
        reply_delay_sec=delay,
        reply_delay_max_sec=delay_max,
    )


def override_policy(base: BridgePolicy, overrides: dict[str, object] | None = None) -> BridgePolicy:
    if not overrides:
        return base
    return build_policy(
        read_allow_chat_ids=(
            overrides["read_allow_chat_ids"] if "read_allow_chat_ids" in overrides else base.read_scope.as_allow_input()
        ),
        read_deny_chat_ids=(
            overrides["read_deny_chat_ids"] if "read_deny_chat_ids" in overrides else base.read_scope.as_deny_input()
        ),
        write_allow_chat_ids=(
            overrides["write_allow_chat_ids"] if "write_allow_chat_ids" in overrides else base.write_scope.as_allow_input()
        ),
        write_deny_chat_ids=(
            overrides["write_deny_chat_ids"] if "write_deny_chat_ids" in overrides else base.write_scope.as_deny_input()
        ),
        reply_delay_sec=float(overrides.get("reply_delay_sec", base.reply_delay_sec)),
        reply_delay_max_sec=(
            float(overrides["reply_delay_max_sec"])
            if "reply_delay_max_sec" in overrides and overrides["reply_delay_max_sec"] is not None
            else base.reply_delay_max_sec
        ),
    )


def _scope_matches(candidate_keys: Iterable[str], scope: BridgeScope) -> bool:
    keys = {key for key in candidate_keys if key}
    if "*" in scope.deny or keys & scope.deny:
        return False
    if scope.allow_all:
        return True
    return bool(keys & scope.allow)


def _build_candidate_keys(
    *,
    peer: str | int | None = None,
    entity: Any | None = None,
    extra_keys: Iterable[str | int] | None = None,
) -> set[str]:
    keys: set[str] = set()
    if peer is not None:
        normalized = _normalize_peer(peer)
        if normalized:
            keys.add(normalized)
    if entity is not None:
        entity_id = getattr(entity, "id", None)
        if entity_id is not None:
            keys.add(_normalize_peer(entity_id))
        username = getattr(entity, "username", None)
        if username:
            keys.add(_normalize_peer(username))
    if extra_keys is not None:
        for item in extra_keys:
            normalized = _normalize_peer(item)
            if normalized:
                keys.add(normalized)
    return keys


def _can_write_self(scope: BridgeScope) -> bool:
    return scope.allow_all or "me" in scope.allow


def _require_self_write(scope: BridgeScope, *, detail: str) -> None:
    if not _can_write_self(scope):
        raise BridgeForbiddenError(detail)


def _extract_flood_wait_seconds(exc: BaseException) -> int | None:
    seconds = getattr(exc, "seconds", None)
    if isinstance(seconds, int):
        return seconds
    if isinstance(seconds, float):
        return int(seconds)
    if type(exc).__name__ == "FloodWaitError":
        return int(seconds or 0) or None
    return None


def _map_telegram_error(exc: BaseException, *, action: str) -> BridgeError:
    name = type(exc).__name__
    if isinstance(exc, BridgeError):
        return exc

    if name == "FilterIdInvalidError":
        return BridgeValidationError("Invalid Telegram dialog folder id. Use a custom folder id between 2 and 255.")

    seconds = _extract_flood_wait_seconds(exc)
    if seconds is not None:
        return BridgeRateLimitError(seconds)

    invalid_peer_errors = {
        "PeerIdInvalidError",
        "UsernameInvalidError",
        "UsernameNotOccupiedError",
        "InviteHashInvalidError",
        "ChatIdInvalidError",
        "ChannelInvalidError",
    }
    forbidden_errors = {
        "ChatWriteForbiddenError",
        "ChatAdminRequiredError",
        "UserIsBlockedError",
        "ForbiddenError",
        "UserBannedInChannelError",
    }
    auth_errors = {
        "AuthKeyError",
        "AuthKeyDuplicatedError",
        "AuthKeyUnregisteredError",
        "SessionRevokedError",
        "UnauthorizedError",
    }
    rate_limit_errors = {"PeerFloodError", "UserRestrictedError"}

    if name in invalid_peer_errors:
        return BridgeValidationError(f"Invalid Telegram peer for {action}.")
    if isinstance(exc, ValueError):
        message = str(exc).lower()
        peer_related_markers = (
            "cannot find any entity",
            "cannot cast",
            "username",
            "peer",
            "chat id",
            "channel",
            "user id",
        )
        if any(marker in message for marker in peer_related_markers):
            return BridgeValidationError(f"Invalid Telegram peer for {action}.")
    if name in forbidden_errors:
        return BridgeForbiddenError(f"Telegram denied permission to {action}.")
    if name in auth_errors:
        return BridgeUnavailableError("Telegram session is no longer valid. Re-create the session locally.")
    if name in rate_limit_errors:
        return BridgeRateLimitError(60, detail=f"Telegram temporarily limited {action}. Try again later.")
    return BridgeError(f"Telegram request failed while trying to {action}.")


class BridgeClient:
    """Wraps Telethon client with reply delay, read/write scopes, and send lock."""

    def __init__(
        self,
        client: TelegramClient,
        *,
        reply_delay_sec: float = 2.0,
        reply_delay_max_sec: float | None = None,
        allow_chat_ids: list[str] | None = None,
        deny_chat_ids: list[str] | None = None,
        write_allow_chat_ids: list[str] | None = None,
        write_deny_chat_ids: list[str] | None = None,
        rpc_timeout_sec: float = 30.0,
        flood_wait_max_sleep_sec: float = 3.0,
    ) -> None:
        self._client = client
        self._policy = build_policy(
            read_allow_chat_ids=allow_chat_ids,
            read_deny_chat_ids=deny_chat_ids,
            write_allow_chat_ids=write_allow_chat_ids,
            write_deny_chat_ids=write_deny_chat_ids,
            reply_delay_sec=reply_delay_sec,
            reply_delay_max_sec=reply_delay_max_sec,
        )
        self._send_lock = asyncio.Lock()
        self._rpc_timeout_sec = max(1.0, rpc_timeout_sec)
        self._flood_wait_max_sleep_sec = max(0.0, flood_wait_max_sleep_sec)
        self._observed_peer_entities: OrderedDict[str, Any] = OrderedDict()
        self._is_premium: bool | None = None

    @property
    def client(self) -> TelegramClient:
        return self._client

    def _resolve_policy(self, overrides: dict[str, object] | None = None) -> BridgePolicy:
        return override_policy(self._policy, overrides)

    def _get_delay(self, policy: BridgePolicy) -> float:
        delay_max = policy.reply_delay_max_sec
        if delay_max is not None and delay_max > policy.reply_delay_sec:
            return random.uniform(policy.reply_delay_sec, delay_max)
        return policy.reply_delay_sec

    async def _is_connected(self) -> bool:
        connected = self._client.is_connected()
        if inspect.isawaitable(connected):
            connected = await connected
        return bool(connected)

    async def ensure_connected(self) -> bool:
        if not await self._is_connected():
            try:
                await self._client.connect()
            except Exception as exc:
                logger.warning("Reconnect failed: %s", type(exc).__name__)
                return False
        return await self._is_connected()

    async def refresh_premium(self) -> bool:
        """Fetch and cache the account's Premium status. Returns the cached value."""
        try:
            me = await self._client.get_me()
            self._is_premium = bool(getattr(me, "premium", False))
        except Exception as exc:
            logger.warning("Failed to fetch premium status: %s", type(exc).__name__)
            self._is_premium = False
        return bool(self._is_premium)

    async def _call_telegram(
        self,
        func: Any,
        *args: Any,
        action: str,
        allow_flood_retry: bool = False,
        **kwargs: Any,
    ) -> Any:
        for attempt in range(2):
            try:
                async with asyncio.timeout(self._rpc_timeout_sec):
                    return await func(*args, **kwargs)
            except TimeoutError as exc:
                raise BridgeTimeoutError() from exc
            except Exception as exc:
                seconds = _extract_flood_wait_seconds(exc)
                if seconds is not None:
                    if allow_flood_retry and attempt == 0 and seconds <= self._flood_wait_max_sleep_sec:
                        logger.info("FloodWait: sleeping %s seconds before retry", seconds)
                        await asyncio.sleep(seconds)
                        continue
                    raise BridgeRateLimitError(seconds) from exc
                raise _map_telegram_error(exc, action=action) from exc
        raise BridgeError(f"Telegram request failed while trying to {action}.")

    def observe_peer_entity(
        self,
        entity: Any | None,
        *,
        peer: str | int | None = None,
        extra_keys: Iterable[str | int] | None = None,
    ) -> None:
        if entity is None:
            return
        keys = _build_candidate_keys(peer=peer, entity=entity, extra_keys=extra_keys)
        if not keys:
            return
        for key in keys:
            self._observed_peer_entities[key] = entity
            self._observed_peer_entities.move_to_end(key)
        while len(self._observed_peer_entities) > OBSERVED_PEER_CACHE_SIZE:
            self._observed_peer_entities.popitem(last=False)

    def _get_observed_peer_entity(self, peer: str | int) -> Any | None:
        normalized = _normalize_peer(peer)
        if not normalized:
            return None
        entity = self._observed_peer_entities.get(normalized)
        if entity is not None:
            self._observed_peer_entities.move_to_end(normalized)
        return entity

    async def _resolve_entity(self, peer: str | int, *, action: str) -> Any:
        if not await self.ensure_connected():
            raise BridgeUnavailableError("Telegram bridge is not connected.")
        observed = self._get_observed_peer_entity(peer)
        if observed is not None:
            return observed
        entity = await self._call_telegram(self._client.get_entity, peer, action=action)
        self.observe_peer_entity(entity, peer=peer)
        return entity

    def _check_scope(
        self,
        *,
        scope: BridgeScope,
        candidate_keys: set[str],
        peer: str | int,
        action: str,
    ) -> None:
        if not _scope_matches(candidate_keys, scope):
            raise BridgeForbiddenError(f"{action.capitalize()} is not allowed for peer: {peer}")

    async def _resolve_scoped_entity(
        self,
        peer: str | int,
        *,
        action: str,
        scope: BridgeScope,
    ) -> tuple[Any, set[str]]:
        entity = await self._resolve_entity(peer, action=action)
        candidate_keys = _build_candidate_keys(peer=peer, entity=entity)
        self._check_scope(scope=scope, candidate_keys=candidate_keys, peer=peer, action=action)
        return entity, candidate_keys

    async def _delay_before_write(self, policy: BridgePolicy) -> None:
        await asyncio.sleep(self._get_delay(policy))
        if not await self.ensure_connected():
            raise BridgeUnavailableError("Telegram bridge is not connected.")

    def _require_channel_like(self, entity: Any, *, action: str) -> None:
        if not _is_channel_like(entity):
            raise BridgeValidationError(f"{action.capitalize()} is supported only for channels and supergroups.")

    async def send_message(
        self,
        peer: str | int,
        text: str,
        reply_to: int | None = None,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        text = (text or "").strip()
        if not text:
            raise BridgeValidationError("Message text is empty")

        chunks = _split_text(text, MAX_MESSAGE_LENGTH)
        if len(chunks) > MAX_SEND_CHUNKS:
            raise BridgeValidationError(
                f"Message splits into {len(chunks)} chunks, which exceeds the limit of {MAX_SEND_CHUNKS}. "
                "Reduce the message length."
            )
        async with self._send_lock:
            entity, _ = await self._resolve_scoped_entity(
                peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)

            ids: list[Any] = []
            for chunk in chunks:
                result = await self._call_telegram(
                    self._client.send_message,
                    entity,
                    chunk,
                    reply_to=reply_to if not ids else None,
                    action="send a message",
                    allow_flood_retry=True,
                )
                ids.append(getattr(result, "id", None))
            if len(ids) == 1:
                return {"ok": True, "message_id": ids[0]}
            return {"ok": True, "message_id": ids[0], "message_ids": ids}

    async def mark_read(
        self,
        peer: str | int,
        *,
        max_message_id: int | None = None,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="marking messages as read",
            scope=policy.read_scope,
        )
        self._check_scope(
            scope=policy.write_scope,
            candidate_keys=_build_candidate_keys(peer=peer, entity=entity),
            peer=peer,
            action="interacting",
        )
        kwargs: dict[str, Any] = {}
        if isinstance(max_message_id, int) and max_message_id > 0:
            kwargs["max_id"] = max_message_id
        await self._call_telegram(
            self._client.send_read_acknowledge,
            entity,
            action="mark messages as read",
            **kwargs,
        )
        return {"ok": True}

    async def send_typing(
        self,
        peer: str | int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="writing",
            scope=policy.write_scope,
        )
        functions = _telethon_functions()
        types = _telethon_types()
        input_peer = await self._call_telegram(
            self._client.get_input_entity,
            entity,
            action="resolve typing peer",
        )
        await self._call_telegram(
            self._client.__call__,
            functions.messages.SetTypingRequest(
                peer=input_peer,
                action=types.SendMessageTypingAction(),
            ),
            action="send typing status",
        )
        return {"ok": True}

    async def send_file(
        self,
        peer: str | int,
        file_path: str,
        *,
        caption: str | None = None,
        reply_to: int | None = None,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing backend-host files is not allowed for this profile.")
        file_path = str(file_path or "").strip()
        if not file_path:
            raise BridgeValidationError("file_path is required.")
        if not Path(file_path).exists():
            raise BridgeValidationError("File does not exist on the backend host.")
        async with self._send_lock:
            entity, _ = await self._resolve_scoped_entity(
                peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)
            result = await self._call_telegram(
                self._client.send_file,
                entity,
                file_path,
                caption=caption,
                reply_to=reply_to,
                action="send a file",
                allow_flood_retry=True,
            )
            return {"ok": True, "message_id": getattr(result, "id", None)}

    async def send_voice(
        self,
        peer: str | int,
        file_path: str,
        *,
        caption: str | None = None,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing backend-host files is not allowed for this profile.")
        file_path = str(file_path or "").strip()
        if not file_path:
            raise BridgeValidationError("file_path is required.")
        if not Path(file_path).exists():
            raise BridgeValidationError("File does not exist on the backend host.")
        async with self._send_lock:
            entity, _ = await self._resolve_scoped_entity(
                peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)
            result = await self._call_telegram(
                self._client.send_file,
                entity,
                file_path,
                caption=caption,
                voice_note=True,
                action="send a voice note",
                allow_flood_retry=True,
            )
            return {"ok": True, "message_id": getattr(result, "id", None)}

    async def send_sticker(
        self,
        peer: str | int,
        file_path: str,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing backend-host files is not allowed for this profile.")
        file_path = str(file_path or "").strip()
        if not file_path:
            raise BridgeValidationError("file_path is required.")
        if not Path(file_path).exists():
            raise BridgeValidationError("File does not exist on the backend host.")
        async with self._send_lock:
            entity, _ = await self._resolve_scoped_entity(
                peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)
            result = await self._call_telegram(
                self._client.send_file,
                entity,
                file_path,
                force_document=False,
                action="send a sticker",
                allow_flood_retry=True,
            )
            return {"ok": True, "message_id": getattr(result, "id", None)}

    async def send_location(
        self,
        peer: str | int,
        *,
        latitude: float,
        longitude: float,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        if not (-90.0 <= latitude <= 90.0):
            raise BridgeValidationError("latitude must be between -90 and 90.")
        if not (-180.0 <= longitude <= 180.0):
            raise BridgeValidationError("longitude must be between -180 and 180.")
        policy = self._resolve_policy(policy_overrides)
        types = _telethon_types()
        functions = _telethon_functions()
        async with self._send_lock:
            entity, _ = await self._resolve_scoped_entity(
                peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)
            request = functions.messages.SendMediaRequest(
                peer=entity,
                media=types.InputMediaGeoPoint(
                    geo_point=types.InputGeoPoint(
                        lat=latitude,
                        long=longitude,
                        accuracy_radius=None,
                    )
                ),
                message="",
                random_id=random.getrandbits(63),
            )
            result = await self._call_telegram(
                self._client.__call__,
                request,
                action="send a location",
                allow_flood_retry=True,
            )
            updates = getattr(result, "updates", None) or []
            message_id = None
            for update in updates:
                value = getattr(update, "id", None) or getattr(getattr(update, "message", None), "id", None)
                if isinstance(value, int):
                    message_id = value
                    break
            return {"ok": True, "message_id": message_id}

    async def edit_message(
        self,
        peer: str | int,
        message_id: int,
        text: str,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        text = (text or "").strip()
        if message_id < 1:
            raise BridgeValidationError("message_id must be >= 1.")
        if not text:
            raise BridgeValidationError("Message text is empty")
        async with self._send_lock:
            entity, _ = await self._resolve_scoped_entity(
                peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)
            await self._call_telegram(
                self._client.edit_message,
                entity,
                message_id,
                text,
                action="edit a message",
                allow_flood_retry=True,
            )
            return {"ok": True, "message_id": message_id}

    async def delete_message(
        self,
        peer: str | int,
        message_id: int,
        *,
        revoke: bool = True,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        if message_id < 1:
            raise BridgeValidationError("message_id must be >= 1.")
        async with self._send_lock:
            entity, _ = await self._resolve_scoped_entity(
                peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)
            await self._call_telegram(
                self._client.delete_messages,
                entity,
                message_id,
                revoke=revoke,
                action="delete a message",
            )
            return {"ok": True, "message_id": message_id}

    async def forward_message(
        self,
        from_peer: str | int,
        to_peer: str | int,
        message_id: int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        if message_id < 1:
            raise BridgeValidationError("message_id must be >= 1.")
        from_entity, _ = await self._resolve_scoped_entity(
            from_peer,
            action="reading",
            scope=policy.read_scope,
        )
        async with self._send_lock:
            to_entity, _ = await self._resolve_scoped_entity(
                to_peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)
            result = await self._call_telegram(
                self._client.forward_messages,
                to_entity,
                message_id,
                from_entity,
                action="forward a message",
                allow_flood_retry=True,
            )
            if isinstance(result, list) and result:
                forwarded = result[0]
            else:
                forwarded = result
            return {"ok": True, "message_id": getattr(forwarded, "id", None)}

    async def get_me(self) -> dict[str, Any]:
        if not await self.ensure_connected():
            raise BridgeUnavailableError("Telegram bridge is not connected.")
        me = await self._call_telegram(self._client.get_me, action="fetch account details")
        if not me:
            return {}
        return {
            "id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
            "last_name": getattr(me, "last_name", None),
            "premium": bool(getattr(me, "premium", False)),
        }

    async def transcribe_voice(
        self,
        peer: str | int,
        message_id: int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        if message_id < 1:
            raise BridgeValidationError("message_id must be >= 1.")
        if self._is_premium is False:
            return {"ok": False, "error": "transcription_unavailable"}
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        functions = _telethon_functions()
        request = functions.messages.TranscribeAudioRequest(peer=entity, msg_id=message_id)

        async def _do_transcribe() -> Any:
            try:
                async with asyncio.timeout(self._rpc_timeout_sec):
                    return await self._client(request)
            except TimeoutError as exc:
                raise BridgeTimeoutError() from exc
            except Exception as exc:
                exc_name = type(exc).__name__
                if exc_name == "PremiumAccountRequiredError" or "PREMIUM_ACCOUNT_REQUIRED" in str(exc):
                    self._is_premium = False
                    return None  # sentinel for fallback
                raise _map_telegram_error(exc, action="transcribe voice") from exc

        result = await _do_transcribe()
        if result is None:
            return {"ok": False, "error": "transcription_unavailable"}
        for _ in range(MAX_TRANSCRIPTION_POLL_ATTEMPTS):
            if not getattr(result, "pending", False):
                break
            await asyncio.sleep(TRANSCRIPTION_POLL_INTERVAL_SEC)
            polled = await _do_transcribe()
            if polled is None:
                break
            result = polled
        text = getattr(result, "text", None) or ""
        return {"ok": True, "text": text}

    async def get_message(
        self,
        peer: str | int,
        message_id: int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        if message_id < 1:
            raise BridgeValidationError("message_id must be >= 1.")
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        message = await self._call_telegram(
            self._client.get_messages,
            entity,
            ids=message_id,
            action="read one message",
        )
        if message is None:
            raise BridgeValidationError("Message not found.")
        return _serialize_message(message, entity=entity, premium=self._is_premium)

    async def download_media(
        self,
        peer: str | int,
        message_id: int,
        *,
        output_path: str | None = None,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Downloading media to the backend host is not allowed for this profile.")
        if message_id < 1:
            raise BridgeValidationError("message_id must be >= 1.")
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        tg_message = await self._call_telegram(
            self._client.get_messages,
            entity,
            ids=message_id,
            action="download media",
        )
        if tg_message is None:
            raise BridgeValidationError("Message not found.")
        if getattr(tg_message, "media", None) is None:
            raise BridgeValidationError("Message does not contain downloadable media.")
        file_path = await self._call_telegram(
            self._client.download_media,
            tg_message,
            file=output_path,
            action="download media",
        )
        return {
            "ok": True,
            "path": file_path,
            "message": _serialize_message(tg_message, entity=entity, premium=self._is_premium),
        }

    async def download_media_for_inbox(
        self,
        peer: str | int,
        message_id: int,
        *,
        output_path: str,
        policy_overrides: dict[str, object] | None = None,
    ) -> str | None:
        """Download one inbound DM attachment for internal channel processing.

        Unlike download_media(), this path is used by /dm/inbox/poll and only enforces read scope.
        """
        policy = self._resolve_policy(policy_overrides)
        if message_id < 1:
            raise BridgeValidationError("message_id must be >= 1.")
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        tg_message = await self._call_telegram(
            self._client.get_messages,
            entity,
            ids=message_id,
            action="download inbound media",
        )
        if tg_message is None or getattr(tg_message, "media", None) is None:
            return None
        return await self._call_telegram(
            self._client.download_media,
            tg_message,
            file=output_path,
            action="download inbound media",
        )

    async def search_messages(
        self,
        peer: str | int,
        query: str,
        *,
        limit: int = 20,
        from_user: str | int | None = None,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        query = (query or "").strip()
        if not query:
            raise BridgeValidationError("query is required.")
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        from_entity = None
        if from_user is not None:
            from_entity = await self._resolve_entity(from_user, action="resolve search sender")
        messages = await self._call_telegram(
            self._client.get_messages,
            entity,
            limit=min(max(1, limit), 50),
            search=query,
            from_user=from_entity,
            action="search messages",
        )
        return _serialize_messages(messages, entity=entity, premium=self._is_premium)

    async def get_media_info(
        self,
        peer: str | int,
        message_id: int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        message = await self.get_message(peer, message_id, policy_overrides=policy_overrides)
        return {
            "id": message.get("id"),
            "has_media": message.get("has_media"),
            "media_type": message.get("media_type"),
            "mime_type": message.get("mime_type"),
            "file_name": message.get("file_name"),
            "file_size": message.get("file_size"),
            "latitude": message.get("latitude"),
            "longitude": message.get("longitude"),
            "venue_title": message.get("venue_title"),
            "venue_address": message.get("venue_address"),
        }

    async def list_contacts(
        self,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for listing contacts.")
        functions = _telethon_functions()
        result = await self._call_telegram(
            self._client.__call__,
            functions.contacts.GetContactsRequest(hash=0),
            action="list contacts",
        )
        contacts: list[dict[str, Any]] = []
        for user in getattr(result, "users", []) or []:
            keys = _build_candidate_keys(entity=user)
            if not _scope_matches(keys, policy.read_scope):
                continue
            contacts.append(
                {
                    "id": getattr(user, "id", None),
                    "username": getattr(user, "username", None),
                    "title": _entity_display_name(user),
                    "phone": getattr(user, "phone", None),
                }
            )
        return contacts

    async def search_contacts(
        self,
        query: str,
        *,
        limit: int = 10,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for searching contacts.")
        query = (query or "").strip()
        if not query:
            raise BridgeValidationError("query is required.")
        functions = _telethon_functions()
        result = await self._call_telegram(
            self._client.__call__,
            functions.contacts.SearchRequest(q=query, limit=min(max(1, limit), 50)),
            action="search contacts",
        )
        contacts: list[dict[str, Any]] = []
        for user in getattr(result, "users", []) or []:
            keys = _build_candidate_keys(entity=user)
            if not _scope_matches(keys, policy.read_scope):
                continue
            contacts.append(
                {
                    "id": getattr(user, "id", None),
                    "username": getattr(user, "username", None),
                    "title": _entity_display_name(user),
                    "phone": getattr(user, "phone", None),
                }
            )
        return contacts

    async def add_contact(
        self,
        phone: str,
        first_name: str,
        last_name: str | None = None,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for creating contacts.")
        phone = phone.strip()
        first_name = first_name.strip()
        if not phone or not first_name:
            raise BridgeValidationError("phone and first_name are required.")
        functions = _telethon_functions()
        types = _telethon_types()
        result = await self._call_telegram(
            self._client.__call__,
            functions.contacts.ImportContactsRequest(
                contacts=[
                    types.InputPhoneContact(
                        client_id=random.getrandbits(63),
                        phone=phone,
                        first_name=first_name,
                        last_name=(last_name or "").strip(),
                    )
                ]
            ),
            action="add a contact",
        )
        user = (getattr(result, "users", None) or [None])[0]
        return {
            "ok": True,
            "contact": {
                "id": getattr(user, "id", None),
                "username": getattr(user, "username", None),
                "title": _entity_display_name(user),
                "phone": getattr(user, "phone", None) if user is not None else phone,
            },
        }

    async def delete_contact(
        self,
        user_peer: str | int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for deleting contacts.")
        entity, _ = await self._resolve_scoped_entity(
            user_peer,
            action="writing",
            scope=policy.write_scope,
        )
        functions = _telethon_functions()
        await self._call_telegram(
            self._client.__call__,
            functions.contacts.DeleteContactsRequest(id=[entity]),
            action="delete a contact",
        )
        return {"ok": True}

    async def block_user(
        self,
        user_peer: str | int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for blocking users.")
        entity, _ = await self._resolve_scoped_entity(
            user_peer,
            action="writing",
            scope=policy.write_scope,
        )
        functions = _telethon_functions()
        await self._call_telegram(
            self._client.__call__,
            functions.contacts.BlockRequest(id=entity),
            action="block a user",
        )
        return {"ok": True}

    async def unblock_user(
        self,
        user_peer: str | int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for unblocking users.")
        entity, _ = await self._resolve_scoped_entity(
            user_peer,
            action="writing",
            scope=policy.write_scope,
        )
        functions = _telethon_functions()
        await self._call_telegram(
            self._client.__call__,
            functions.contacts.UnblockRequest(id=entity),
            action="unblock a user",
        )
        return {"ok": True}

    async def get_blocked_users(
        self,
        *,
        limit: int = 100,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for listing blocked users.")
        functions = _telethon_functions()
        result = await self._call_telegram(
            self._client.__call__,
            functions.contacts.GetBlockedRequest(offset=0, limit=min(max(1, limit), 200)),
            action="list blocked users",
        )
        users = getattr(result, "users", []) or []
        blocked: list[dict[str, Any]] = []
        for user in users:
            keys = _build_candidate_keys(entity=user)
            if not _scope_matches(keys, policy.read_scope):
                continue
            blocked.append(
                {
                    "id": getattr(user, "id", None),
                    "username": getattr(user, "username", None),
                    "title": _entity_display_name(user),
                }
            )
        return blocked

    async def create_group(
        self,
        title: str,
        users: list[str | int],
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for creating groups.")
        title = title.strip()
        if not title:
            raise BridgeValidationError("title is required.")
        user_entities: list[Any] = []
        for user in users:
            entity, _ = await self._resolve_scoped_entity(
                user,
                action="writing",
                scope=policy.write_scope,
            )
            user_entities.append(entity)
        functions = _telethon_functions()
        result = await self._call_telegram(
            self._client.__call__,
            functions.messages.CreateChatRequest(title=title, users=user_entities),
            action="create a group",
        )
        chats = getattr(result, "chats", None) or []
        chat = chats[0] if chats else None
        return {"ok": True, "chat_id": getattr(chat, "id", None), "title": getattr(chat, "title", title)}

    async def create_channel(
        self,
        title: str,
        *,
        about: str | None = None,
        megagroup: bool = False,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for creating channels.")
        title = title.strip()
        if not title:
            raise BridgeValidationError("title is required.")
        is_megagroup = bool(megagroup)
        functions = _telethon_functions()
        result = await self._call_telegram(
            self._client.__call__,
            functions.channels.CreateChannelRequest(
                title=title,
                about=(about or "").strip(),
                broadcast=not is_megagroup,
                megagroup=is_megagroup,
            ),
            action="create a channel",
        )
        chats = getattr(result, "chats", None) or []
        chat = chats[0] if chats else None
        return {"ok": True, "chat_id": getattr(chat, "id", None), "title": getattr(chat, "title", title)}

    async def invite_to_group(
        self,
        peer: str | int,
        users: list[str | int],
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for inviting users.")
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="writing",
            scope=policy.write_scope,
        )
        user_entities: list[Any] = []
        for user in users:
            user_entity, _ = await self._resolve_scoped_entity(
                user,
                action="writing",
                scope=policy.write_scope,
            )
            user_entities.append(user_entity)
        functions = _telethon_functions()
        if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
            await self._call_telegram(
                self._client.__call__,
                functions.channels.InviteToChannelRequest(channel=entity, users=user_entities),
                action="invite users to a channel",
            )
        else:
            for user in user_entities:
                await self._call_telegram(
                    self._client.__call__,
                    functions.messages.AddChatUserRequest(
                        chat_id=getattr(entity, "id", None),
                        user_id=user,
                        fwd_limit=50,
                    ),
                    action="invite users to a group",
                )
        return {"ok": True, "invited_count": len(user_entities)}

    async def get_invite_link(
        self,
        peer: str | int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for exporting invite links.")
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        functions = _telethon_functions()
        result = await self._call_telegram(
            self._client.__call__,
            functions.messages.ExportChatInviteRequest(peer=entity),
            action="get invite link",
        )
        return {"link": getattr(result, "link", None)}

    async def join_chat_by_link(
        self,
        link: str,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for joining chats.")
        link = link.strip()
        if not link:
            raise BridgeValidationError("link is required.")
        functions = _telethon_functions()
        raw = link
        if raw.startswith("@"):
            target_type = "public"
            target_value = raw[1:]
        elif raw.startswith("+"):
            target_type = "invite"
            target_value = raw[1:]
        else:
            parsed = urlparse(raw if "://" in raw else f"https://{raw}")
            host = (parsed.netloc or "").strip().lower()
            path_parts = [part for part in (parsed.path or "").split("/") if part]
            known_hosts = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
            target_type = "public"
            target_value = ""
            if host in known_hosts and path_parts:
                first = path_parts[0]
                if first.lower() == "joinchat" and len(path_parts) >= 2:
                    target_type = "invite"
                    target_value = path_parts[1]
                elif first.startswith("+"):
                    target_type = "invite"
                    target_value = first[1:]
                elif first.lower() == "s" and len(path_parts) >= 2:
                    target_value = path_parts[1]
                else:
                    target_value = first
            elif path_parts:
                target_value = path_parts[-1]
            else:
                target_value = (parsed.netloc or raw).strip()
            if target_value.startswith("+"):
                target_type = "invite"
                target_value = target_value[1:]
        target_value = target_value.split("?", 1)[0].split("#", 1)[0].strip().lstrip("@")
        if not target_value or any(ch.isspace() for ch in target_value):
            raise BridgeValidationError("link is invalid.")

        if target_type == "invite":
            result = await self._call_telegram(
                self._client.__call__,
                functions.messages.ImportChatInviteRequest(hash=target_value),
                action="join a chat by invite link",
            )
            chats = getattr(result, "chats", None) or []
            chat = chats[0] if chats else None
            return {"ok": True, "chat_id": getattr(chat, "id", None), "title": getattr(chat, "title", None), "join_type": "invite"}

        entity = await self._resolve_entity(target_value, action="resolve public chat")
        if not _is_channel_like(entity):
            raise BridgeValidationError("Public link must resolve to a channel or supergroup.")
        if getattr(entity, "left", None) is False:
            return {
                "ok": True,
                "chat_id": getattr(entity, "id", None),
                "title": _entity_display_name(entity),
                "join_type": "public",
                "already_joined": True,
            }
        result = await self._call_telegram(
            self._client.__call__,
            functions.channels.JoinChannelRequest(channel=entity),
            action="join a public chat",
        )
        chats = getattr(result, "chats", None) or []
        chat = chats[0] if chats else entity
        return {
            "ok": True,
            "chat_id": getattr(chat, "id", None),
            "title": getattr(chat, "title", None) or _entity_display_name(entity),
            "join_type": "public",
        }

    async def list_dialog_folders(
        self,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for listing dialog folders.")
        functions = _telethon_functions()
        raw_filters = await self._call_telegram(
            self._client.__call__,
            functions.messages.GetDialogFiltersRequest(),
            action="list dialog folders",
        )
        raw_items = getattr(raw_filters, "filters", None)
        if raw_items is None:
            raw_items = raw_filters
        try:
            filters = list(raw_items or [])
        except TypeError:
            filters = []

        def _serialize_peer_ids(peers: Any) -> list[int]:
            ids: list[int] = []
            seen: set[int] = set()
            for peer in peers or []:
                peer_id = _extract_peer_id(peer)
                if peer_id is None or peer_id in seen:
                    continue
                seen.add(peer_id)
                ids.append(peer_id)
            return ids

        out: list[dict[str, Any]] = []
        for item in filters:
            if type(item).__name__ != "DialogFilter":
                continue
            folder_id = getattr(item, "id", None)
            if not isinstance(folder_id, int):
                continue
            out.append(
                {
                    "id": folder_id,
                    "title": _display_text(getattr(item, "title", None)),
                    "emoticon": getattr(item, "emoticon", None),
                    "contacts": bool(getattr(item, "contacts", False)),
                    "non_contacts": bool(getattr(item, "non_contacts", False)),
                    "groups": bool(getattr(item, "groups", False)),
                    "broadcasts": bool(getattr(item, "broadcasts", False)),
                    "bots": bool(getattr(item, "bots", False)),
                    "exclude_muted": bool(getattr(item, "exclude_muted", False)),
                    "exclude_read": bool(getattr(item, "exclude_read", False)),
                    "exclude_archived": bool(getattr(item, "exclude_archived", False)),
                    "pinned_peers": _serialize_peer_ids(getattr(item, "pinned_peers", None)),
                    "include_peers": _serialize_peer_ids(getattr(item, "include_peers", None)),
                    "exclude_peers": _serialize_peer_ids(getattr(item, "exclude_peers", None)),
                }
            )
        out.sort(key=lambda folder: int(folder.get("id", 0)))
        return out

    async def upsert_dialog_folder(
        self,
        folder_id: int,
        title: str,
        *,
        emoticon: str | None = None,
        contacts: bool = False,
        non_contacts: bool = False,
        groups: bool = False,
        broadcasts: bool = False,
        bots: bool = False,
        exclude_muted: bool = False,
        exclude_read: bool = False,
        exclude_archived: bool = False,
        pinned_peers: list[str | int] | None = None,
        include_peers: list[str | int] | None = None,
        exclude_peers: list[str | int] | None = None,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for managing dialog folders.")
        if folder_id < 2 or folder_id > 255:
            raise BridgeValidationError("folder_id must be between 2 and 255.")
        title = (title or "").strip()
        if not title:
            raise BridgeValidationError("title is required.")
        emoticon_value = (emoticon or "").strip() or None
        functions = _telethon_functions()
        types = _telethon_types()

        async def _resolve_input_peers(items: list[str | int] | None) -> list[Any]:
            resolved: list[Any] = []
            for peer in items or []:
                entity = await self._resolve_entity(peer, action="resolve dialog folder peer")
                input_peer = await self._call_telegram(
                    self._client.get_input_entity,
                    entity,
                    action="resolve dialog folder peer",
                )
                resolved.append(input_peer)
            return resolved

        dialog_filter = types.DialogFilter(
            id=folder_id,
            title=types.TextWithEntities(text=title, entities=[]),
            emoticon=emoticon_value,
            contacts=bool(contacts),
            non_contacts=bool(non_contacts),
            groups=bool(groups),
            broadcasts=bool(broadcasts),
            bots=bool(bots),
            exclude_muted=bool(exclude_muted),
            exclude_read=bool(exclude_read),
            exclude_archived=bool(exclude_archived),
            pinned_peers=await _resolve_input_peers(pinned_peers),
            include_peers=await _resolve_input_peers(include_peers),
            exclude_peers=await _resolve_input_peers(exclude_peers),
        )
        await self._call_telegram(
            self._client.__call__,
            functions.messages.UpdateDialogFilterRequest(
                id=folder_id,
                filter=dialog_filter,
            ),
            action="update a dialog folder",
        )
        return {"ok": True, "folder_id": folder_id}

    async def delete_dialog_folder(
        self,
        folder_id: int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for deleting dialog folders.")
        if folder_id < 2 or folder_id > 255:
            raise BridgeValidationError("folder_id must be between 2 and 255.")
        functions = _telethon_functions()
        await self._call_telegram(
            self._client.__call__,
            functions.messages.UpdateDialogFilterRequest(
                id=folder_id,
                filter=None,
            ),
            action="delete a dialog folder",
        )
        return {"ok": True, "folder_id": folder_id}

    async def resolve_username(self, username: str) -> dict[str, Any]:
        entity = await self._resolve_entity(username, action="resolve username")
        return {
            "id": getattr(entity, "id", None),
            "username": getattr(entity, "username", None),
            "title": _entity_display_name(entity),
            "type": _resolve_chat_type(entity),
        }

    async def get_user_status(self, peer: str | int) -> dict[str, Any]:
        entity = await self._resolve_entity(peer, action="read user status")
        status = getattr(entity, "status", None)
        payload = {
            "id": getattr(entity, "id", None),
            "username": getattr(entity, "username", None),
            "status_type": type(status).__name__ if status is not None else None,
            "was_online": _isoformat(getattr(status, "was_online", None)),
            "expires": _isoformat(getattr(status, "expires", None)),
        }
        return payload

    async def get_participants(
        self,
        peer: str | int,
        *,
        limit: int = 100,
        offset: int = 0,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        participants = await self._call_telegram(
            self._client.get_participants,
            entity,
            limit=min(max(1, limit), 200),
            offset=max(0, offset),
            action="list participants",
        )
        result: list[dict[str, Any]] = []
        for participant in participants:
            result.append(
                {
                    "id": getattr(participant, "id", None),
                    "username": getattr(participant, "username", None),
                    "title": _entity_display_name(participant),
                    "bot": bool(getattr(participant, "bot", False)),
                }
            )
        return result

    async def get_admins(
        self,
        peer: str | int,
        *,
        limit: int = 100,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        limit = min(max(1, limit), 200)
        if _is_channel_like(entity):
            types = _telethon_types()
            admins = await self._call_telegram(
                self._client.get_participants,
                entity,
                limit=limit,
                filter=types.ChannelParticipantsAdmins(),
                action="list admins",
            )
            result: list[dict[str, Any]] = []
            for admin in admins:
                result.append(
                    {
                        "id": getattr(admin, "id", None),
                        "username": getattr(admin, "username", None),
                        "title": _entity_display_name(admin),
                        "bot": bool(getattr(admin, "bot", False)),
                    }
                )
            return result
        if _is_basic_group(entity):
            functions = _telethon_functions()
            full = await self._call_telegram(
                self._client.__call__,
                functions.messages.GetFullChatRequest(chat_id=getattr(entity, "id", None)),
                action="list admins",
            )
            full_chat = getattr(full, "full_chat", None)
            participants = getattr(getattr(full_chat, "participants", None), "participants", None) or []
            users_by_id = {
                getattr(user, "id", None): user
                for user in (getattr(full, "users", None) or [])
                if getattr(user, "id", None) is not None
            }
            result = []
            for participant in participants:
                if not _is_basic_group_admin_participant(participant):
                    continue
                user = users_by_id.get(getattr(participant, "user_id", None))
                if user is None:
                    continue
                result.append(
                    {
                        "id": getattr(user, "id", None),
                        "username": getattr(user, "username", None),
                        "title": _entity_display_name(user),
                        "bot": bool(getattr(user, "bot", False)),
                    }
                )
                if len(result) >= limit:
                    break
            return result
        raise BridgeValidationError("Admin listing is supported only for groups, supergroups, and channels.")

    async def get_banned_users(
        self,
        peer: str | int,
        *,
        limit: int = 100,
        offset: int = 0,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        self._require_channel_like(entity, action="listing banned users")
        types = _telethon_types()
        try:
            kicked_filter = types.ChannelParticipantsKicked(q="")
        except TypeError:
            kicked_filter = types.ChannelParticipantsKicked()
        users = await self._call_telegram(
            self._client.get_participants,
            entity,
            limit=min(max(1, limit), 200),
            offset=max(0, offset),
            filter=kicked_filter,
            action="list banned users",
        )
        return [
            {
                "id": getattr(user, "id", None),
                "username": getattr(user, "username", None),
                "title": _entity_display_name(user),
            }
            for user in users
        ]

    async def promote_admin(
        self,
        peer: str | int,
        user_peer: str | int,
        *,
        title: str | None = None,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="writing",
            scope=policy.write_scope,
        )
        user, _ = await self._resolve_scoped_entity(
            user_peer,
            action="writing",
            scope=policy.write_scope,
        )
        functions = _telethon_functions()
        if _is_basic_group(entity):
            await self._call_telegram(
                self._client.__call__,
                functions.messages.EditChatAdminRequest(
                    chat_id=getattr(entity, "id", None),
                    user_id=user,
                    is_admin=True,
                ),
                action="promote an admin",
            )
            return {"ok": True}
        if not _is_channel_like(entity):
            raise BridgeValidationError("Admin promotion is supported only for groups, supergroups, and channels.")
        types = _telethon_types()
        rights = types.ChatAdminRights(
            change_info=True,
            post_messages=True,
            edit_messages=True,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            add_admins=False,
            anonymous=False,
            manage_call=True,
            other=True,
        )
        await self._call_telegram(
            self._client.__call__,
            functions.channels.EditAdminRequest(
                channel=entity,
                user_id=user,
                admin_rights=rights,
                rank=(title or "").strip() or "Admin",
            ),
            action="promote an admin",
        )
        return {"ok": True}

    async def demote_admin(
        self,
        peer: str | int,
        user_peer: str | int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="writing",
            scope=policy.write_scope,
        )
        user, _ = await self._resolve_scoped_entity(
            user_peer,
            action="writing",
            scope=policy.write_scope,
        )
        functions = _telethon_functions()
        if _is_basic_group(entity):
            await self._call_telegram(
                self._client.__call__,
                functions.messages.EditChatAdminRequest(
                    chat_id=getattr(entity, "id", None),
                    user_id=user,
                    is_admin=False,
                ),
                action="demote an admin",
            )
            return {"ok": True}
        if not _is_channel_like(entity):
            raise BridgeValidationError("Admin demotion is supported only for groups, supergroups, and channels.")
        types = _telethon_types()
        rights = types.ChatAdminRights(
            change_info=False,
            post_messages=False,
            edit_messages=False,
            delete_messages=False,
            ban_users=False,
            invite_users=False,
            pin_messages=False,
            add_admins=False,
            anonymous=False,
            manage_call=False,
            other=False,
        )
        await self._call_telegram(
            self._client.__call__,
            functions.channels.EditAdminRequest(
                channel=entity,
                user_id=user,
                admin_rights=rights,
                rank="",
            ),
            action="demote an admin",
        )
        return {"ok": True}

    async def ban_user(
        self,
        peer: str | int,
        user_peer: str | int,
        *,
        until_date: int | None = None,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="writing",
            scope=policy.write_scope,
        )
        user, _ = await self._resolve_scoped_entity(
            user_peer,
            action="writing",
            scope=policy.write_scope,
        )
        self._require_channel_like(entity, action="banning users")
        functions = _telethon_functions()
        types = _telethon_types()
        rights = types.ChatBannedRights(
            until_date=until_date,
            view_messages=True,
            send_messages=True,
            send_media=True,
            send_stickers=True,
            send_gifs=True,
            send_games=True,
            send_inline=True,
            embed_links=True,
            send_polls=True,
            change_info=True,
            invite_users=True,
            pin_messages=True,
        )
        await self._call_telegram(
            self._client.__call__,
            functions.channels.EditBannedRequest(
                channel=entity,
                participant=user,
                banned_rights=rights,
            ),
            action="ban a user",
        )
        return {"ok": True}

    async def unban_user(
        self,
        peer: str | int,
        user_peer: str | int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="writing",
            scope=policy.write_scope,
        )
        user, _ = await self._resolve_scoped_entity(
            user_peer,
            action="writing",
            scope=policy.write_scope,
        )
        self._require_channel_like(entity, action="unbanning users")
        functions = _telethon_functions()
        types = _telethon_types()
        rights = types.ChatBannedRights(
            until_date=None,
            view_messages=False,
            send_messages=False,
            send_media=False,
            send_stickers=False,
            send_gifs=False,
            send_games=False,
            send_inline=False,
            embed_links=False,
            send_polls=False,
            change_info=False,
            invite_users=False,
            pin_messages=False,
        )
        await self._call_telegram(
            self._client.__call__,
            functions.channels.EditBannedRequest(
                channel=entity,
                participant=user,
                banned_rights=rights,
            ),
            action="unban a user",
        )
        return {"ok": True}

    async def get_chat(
        self,
        peer: str | int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        data = {
            "id": getattr(entity, "id", None),
            "username": getattr(entity, "username", None),
            "title": _entity_display_name(entity),
            "type": _resolve_chat_type(entity),
        }
        functions = _telethon_functions()
        try:
            if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
                full = await self._call_telegram(
                    self._client.__call__,
                    functions.channels.GetFullChannelRequest(channel=entity),
                    action="read chat details",
                )
                full_chat = getattr(full, "full_chat", None)
                data["about"] = getattr(full_chat, "about", None)
                data["participants_count"] = getattr(full_chat, "participants_count", None)
            elif getattr(entity, "title", None) is not None:
                full = await self._call_telegram(
                    self._client.__call__,
                    functions.messages.GetFullChatRequest(chat_id=getattr(entity, "id", None)),
                    action="read chat details",
                )
                full_chat = getattr(full, "full_chat", None)
                data["participants_count"] = getattr(full_chat, "participants_count", None)
        except BridgeError:
            raise
        except Exception:
            logger.debug("Unable to fetch full chat info", exc_info=True)
        return data

    async def get_history(
        self,
        peer: str | int,
        *,
        limit: int = 100,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        return await self.get_messages(
            peer,
            limit=min(max(1, limit), 100),
            policy_overrides=policy_overrides,
        )

    async def search_public_chats(
        self,
        query: str,
        *,
        limit: int = 20,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        query = (query or "").strip()
        if not query:
            raise BridgeValidationError("query is required.")
        functions = _telethon_functions()
        result = await self._call_telegram(
            self._client.__call__,
            functions.contacts.SearchRequest(q=query, limit=min(max(1, limit), 50)),
            action="search public chats",
        )
        found: list[dict[str, Any]] = []
        for collection_name in ("users", "chats"):
            for entity in getattr(result, collection_name, []) or []:
                keys = _build_candidate_keys(entity=entity)
                if not _scope_matches(keys, policy.read_scope):
                    continue
                found.append(
                    {
                        "id": getattr(entity, "id", None),
                        "username": getattr(entity, "username", None),
                        "title": _entity_display_name(entity),
                        "type": _resolve_chat_type(entity),
                    }
                )
        return found

    async def get_recent_actions(
        self,
        peer: str | int,
        *,
        limit: int = 20,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        self._require_channel_like(entity, action="reading recent admin actions")
        functions = _telethon_functions()
        result = await self._call_telegram(
            self._client.__call__,
            functions.channels.GetAdminLogRequest(
                channel=entity,
                q="",
                events_filter=None,
                admins=[],
                max_id=0,
                min_id=0,
                limit=min(max(1, limit), 50),
            ),
            action="read recent admin actions",
        )
        events = getattr(result, "events", []) or []
        return [
            {
                "id": getattr(event, "id", None),
                "date": _isoformat(getattr(event, "date", None)),
                "user_id": getattr(event, "user_id", None),
                "action": type(getattr(event, "action", None)).__name__,
            }
            for event in events
        ]

    async def get_pinned_messages(
        self,
        peer: str | int,
        *,
        limit: int = 20,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        types = _telethon_types()
        try:
            messages = await self._call_telegram(
                self._client.get_messages,
                entity,
                limit=min(max(1, limit), 50),
                filter=types.InputMessagesFilterPinned(),
                action="read pinned messages",
            )
        except Exception:
            all_messages = await self._call_telegram(
                self._client.get_messages,
                entity,
                limit=min(max(1, limit), 50),
                action="read pinned messages",
            )
            messages = [message for message in all_messages if getattr(message, "pinned", False)]
        return _serialize_messages(messages, entity=entity, premium=self._is_premium)

    async def send_reaction(
        self,
        peer: str | int,
        message_id: int,
        emoji: str,
        *,
        big: bool = False,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        if message_id < 1:
            raise BridgeValidationError("message_id must be >= 1.")
        emoji = (emoji or "").strip()
        if not emoji:
            raise BridgeValidationError("emoji is required.")
        async with self._send_lock:
            entity, _ = await self._resolve_scoped_entity(
                peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)
            types = _telethon_types()
            functions = _telethon_functions()
            input_peer = await self._call_telegram(
                self._client.get_input_entity,
                entity,
                action="resolve reaction peer",
            )
            await self._call_telegram(
                self._client.__call__,
                functions.messages.SendReactionRequest(
                    peer=input_peer,
                    msg_id=message_id,
                    big=big,
                    reaction=[types.ReactionEmoji(emoticon=emoji)],
                ),
                action="send a reaction",
                allow_flood_retry=True,
            )
            return {"ok": True}

    async def remove_reaction(
        self,
        peer: str | int,
        message_id: int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        if message_id < 1:
            raise BridgeValidationError("message_id must be >= 1.")
        async with self._send_lock:
            entity, _ = await self._resolve_scoped_entity(
                peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)
            functions = _telethon_functions()
            input_peer = await self._call_telegram(
                self._client.get_input_entity,
                entity,
                action="resolve reaction peer",
            )
            await self._call_telegram(
                self._client.__call__,
                functions.messages.SendReactionRequest(
                    peer=input_peer,
                    msg_id=message_id,
                    reaction=[],
                ),
                action="remove a reaction",
                allow_flood_retry=True,
            )
            return {"ok": True}

    async def get_message_reactions(
        self,
        peer: str | int,
        message_id: int,
        *,
        limit: int = 50,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        if message_id < 1:
            raise BridgeValidationError("message_id must be >= 1.")
        entity, _ = await self._resolve_scoped_entity(
            peer,
            action="reading",
            scope=policy.read_scope,
        )
        functions = _telethon_functions()
        input_peer = await self._call_telegram(
            self._client.get_input_entity,
            entity,
            action="resolve reactions peer",
        )
        result = await self._call_telegram(
            self._client.__call__,
            functions.messages.GetMessageReactionsListRequest(
                peer=input_peer,
                id=message_id,
                limit=min(max(1, limit), 100),
            ),
            action="read message reactions",
        )
        aggregated: dict[str, int] = {}
        for reaction in getattr(result, "reactions", []) or []:
            reaction_type = getattr(reaction, "reaction", None)
            label = _reaction_label(reaction_type) or "unknown"
            count = getattr(reaction, "count", 1)
            if not isinstance(count, int) or count < 1:
                count = 1
            aggregated[label] = aggregated.get(label, 0) + count
        return [
            {"emoji": emoji, "count": count}
            for emoji, count in sorted(aggregated.items(), key=lambda item: (-item[1], item[0]))
        ]

    async def leave_chat(
        self,
        peer: str | int,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_policy(policy_overrides)
        _require_self_write(policy.write_scope, detail="Writing is not allowed for leaving chats.")
        async with self._send_lock:
            entity, _ = await self._resolve_scoped_entity(
                peer,
                action="writing",
                scope=policy.write_scope,
            )
            await self._delay_before_write(policy)
            await self._call_telegram(
                self._client.delete_dialog,
                entity,
                action="leave a chat",
            )
            return {"ok": True}

    async def get_dialogs(
        self,
        limit: int = 20,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        if not await self.ensure_connected():
            raise BridgeUnavailableError("Telegram bridge is not connected.")
        policy = self._resolve_policy(policy_overrides)
        dialogs = await self._call_telegram(
            self._client.get_dialogs,
            limit=min(max(1, limit), 50),
            action="list dialogs",
        )
        out = []
        for dialog in dialogs:
            entity = dialog.entity
            candidate_keys = _build_candidate_keys(
                entity=entity,
                extra_keys=[getattr(dialog, "id", None)],
            )
            if not _scope_matches(candidate_keys, policy.read_scope):
                continue
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or ""
            chat_id = getattr(dialog, "id", None) or getattr(entity, "id", None)
            out.append(
                {
                    "id": chat_id,
                    "title": title,
                    "username": getattr(entity, "username", None),
                }
            )
        return out

    async def discover_source_dialogs(self, limit: int = 500) -> list[dict[str, Any]]:
        if not await self.ensure_connected():
            raise BridgeUnavailableError("Telegram bridge is not connected.")
        dialogs = await self._call_telegram(
            self._client.get_dialogs,
            limit=min(max(1, limit), 2000),
            action="list source dialogs",
        )
        out = []
        for dialog in dialogs:
            entry = dialog_to_inventory_entry(dialog)
            if entry.get("peer_key"):
                out.append(entry)
        return out

    async def resolve_peer_identifiers(self, peer: str | int) -> dict[str, str | None]:
        entity = await self._resolve_entity(peer, action="resolve peer")
        return {
            "peer": _normalize_peer(peer),
            "id": _normalize_peer(getattr(entity, "id", None)) or None,
            "username": _normalize_peer(getattr(entity, "username", None)) or None,
        }

    async def get_incoming_direct_messages(
        self,
        peer: str | int,
        *,
        min_id: int | None = None,
        limit: int = 20,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        entity = await self._resolve_entity(peer, action="read incoming direct messages")
        candidate_keys = _build_candidate_keys(peer=peer, entity=entity)
        self._check_scope(
            scope=policy.read_scope,
            candidate_keys=candidate_keys,
            peer=peer,
            action="reading",
        )

        kwargs: dict[str, Any] = {"limit": min(max(1, limit), 50)}
        if min_id is not None:
            kwargs["min_id"] = min_id
        messages = await self._call_telegram(
            self._client.get_messages,
            entity,
            action="read incoming direct messages",
            **kwargs,
        )
        return _serialize_messages(messages, entity=entity, skip_outbound=True, premium=self._is_premium)

    async def list_topics(
        self,
        peer: str | int,
        limit: int = 20,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        entity = await self._resolve_entity(peer, action="list forum topics")
        candidate_keys = _build_candidate_keys(peer=peer, entity=entity)
        self._check_scope(
            scope=policy.read_scope,
            candidate_keys=candidate_keys,
            peer=peer,
            action="reading",
        )
        if not getattr(entity, "forum", False):
            raise BridgeValidationError("Telegram peer does not support forum topics.")

        functions = _telethon_functions()
        request = functions.messages.GetForumTopicsRequest(
            peer=entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=min(max(1, limit), 100),
            q="",
        )
        result = await self._call_telegram(
            self._client.__call__,
            request,
            action="list forum topics",
        )
        topics: list[dict[str, Any]] = []
        for topic in getattr(result, "topics", []) or []:
            serialized = _serialize_topic(topic)
            if serialized is not None:
                topics.append(serialized)
        return topics

    async def get_messages(
        self,
        peer: str | int,
        limit: int = 20,
        min_id: int | None = None,
        topic_id: int | None = None,
        since_unix: int | None = None,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        entity = await self._resolve_entity(peer, action="read messages")
        candidate_keys = _build_candidate_keys(peer=peer, entity=entity)
        self._check_scope(
            scope=policy.read_scope,
            candidate_keys=candidate_keys,
            peer=peer,
            action="reading",
        )
        if since_unix is not None and since_unix < 1:
            raise BridgeValidationError("since_unix must be >= 1.")

        def _filter_batch_since(batch: list[Any]) -> tuple[list[Any], bool]:
            accepted: list[Any] = []
            hit_older_boundary = False
            for message in batch:
                if message is None or getattr(message, "id", None) is None:
                    continue
                if since_unix is not None:
                    message_unix = _datetime_unix(getattr(message, "date", None))
                    if message_unix is not None and message_unix < since_unix:
                        hit_older_boundary = True
                        break
                accepted.append(message)
            return accepted, hit_older_boundary

        if topic_id is not None:
            if topic_id < 1:
                raise BridgeValidationError("topic_id must be >= 1.")
            if not getattr(entity, "forum", False):
                raise BridgeValidationError("Telegram peer does not support forum topics.")
            functions = _telethon_functions()
            if since_unix is None:
                request = functions.messages.GetRepliesRequest(
                    peer=entity,
                    msg_id=topic_id,
                    offset_id=0,
                    offset_date=None,
                    add_offset=0,
                    limit=min(max(1, limit), 50),
                    max_id=0,
                    min_id=max(0, min_id or 0),
                    hash=0,
                )
                result = await self._call_telegram(
                    self._client.__call__,
                    request,
                    action="read topic messages",
                )
                return _serialize_messages(
                    getattr(result, "messages", []) or [],
                    entity=entity,
                    sender_lookup=_build_sender_lookup(result),
                    topic_id_override=topic_id,
                    premium=self._is_premium,
                )

            collected: list[Any] = []
            sender_lookup: dict[str, Any] = {}
            offset_id = 0
            max_messages = min(max(1, limit), 50)
            while len(collected) < max_messages:
                request = functions.messages.GetRepliesRequest(
                    peer=entity,
                    msg_id=topic_id,
                    offset_id=offset_id,
                    offset_date=None,
                    add_offset=0,
                    limit=max_messages - len(collected),
                    max_id=0,
                    min_id=max(0, min_id or 0),
                    hash=0,
                )
                result = await self._call_telegram(
                    self._client.__call__,
                    request,
                    action="read topic messages",
                )
                sender_lookup.update(_build_sender_lookup(result))
                batch = [message for message in (getattr(result, "messages", []) or []) if message is not None]
                if not batch:
                    break
                accepted, hit_older_boundary = _filter_batch_since(batch)
                collected.extend(accepted)
                oldest_id = min(
                    (
                        int(message_id)
                        for message in batch
                        if isinstance((message_id := getattr(message, "id", None)), int)
                    ),
                    default=0,
                )
                if hit_older_boundary or len(collected) >= max_messages or oldest_id <= 0 or oldest_id == offset_id:
                    break
                offset_id = oldest_id
            return _serialize_messages(
                collected,
                entity=entity,
                sender_lookup=sender_lookup,
                topic_id_override=topic_id,
                premium=self._is_premium,
            )

        max_messages = min(max(1, limit), 50)
        if since_unix is None:
            kwargs: dict[str, Any] = {"limit": max_messages}
            if min_id is not None:
                kwargs["min_id"] = min_id
            messages = await self._call_telegram(
                self._client.get_messages,
                entity,
                action="read messages",
                **kwargs,
            )
            return _serialize_messages(messages, entity=entity, premium=self._is_premium)

        collected: list[Any] = []
        offset_id = 0
        while len(collected) < max_messages:
            kwargs = {
                "limit": max_messages - len(collected),
            }
            if min_id is not None:
                kwargs["min_id"] = min_id
            if offset_id > 0:
                kwargs["offset_id"] = offset_id
            messages = await self._call_telegram(
                self._client.get_messages,
                entity,
                action="read messages",
                **kwargs,
            )
            batch = [message for message in list(messages) if message is not None]
            if not batch:
                break
            accepted, hit_older_boundary = _filter_batch_since(batch)
            collected.extend(accepted)
            oldest_id = min(
                (
                    int(message_id)
                    for message in batch
                    if isinstance((message_id := getattr(message, "id", None)), int)
                ),
                default=0,
            )
            if hit_older_boundary or len(collected) >= max_messages or oldest_id <= 0 or oldest_id == offset_id:
                break
            offset_id = oldest_id
        return _serialize_messages(collected, entity=entity, premium=self._is_premium)

    async def disconnect(self) -> None:
        if await self._is_connected():
            await self._client.disconnect()
