"""Telethon client wrapper with backend-enforced policy and error mapping."""

import asyncio
import inspect
import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from telethon import TelegramClient
else:
    TelegramClient = Any

# Telegram message length limit
MAX_MESSAGE_LENGTH = 4096


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
class BridgePolicy:
    allow: frozenset[str] | None
    deny: frozenset[str]
    reply_delay_sec: float
    reply_delay_max_sec: float | None


def _normalize_peer(peer: str | int) -> str:
    """Normalize peer for allow/deny check: username (no @, lowercase) or canonical id."""
    s = str(peer).strip()
    if not s:
        return ""
    if s == "*":
        return "*"
    if s.lower() == "me":
        return "me"
    if s.startswith("@"):
        s = s[1:]
    if s.lstrip("-").isdigit():
        try:
            return str(int(s))
        except ValueError:
            return s
    return s.lower()


def _normalize_peer_list(peers: Iterable[str] | None) -> frozenset[str] | None:
    if peers is None:
        return None
    normalized = {_normalize_peer(peer) for peer in peers if _normalize_peer(peer)}
    if not normalized or "*" in normalized:
        return None
    return frozenset(normalized)


def build_policy(
    *,
    allow_chat_ids: list[str] | None,
    deny_chat_ids: list[str] | None,
    reply_delay_sec: float,
    reply_delay_max_sec: float | None,
) -> BridgePolicy:
    delay = max(0.0, reply_delay_sec)
    delay_max = reply_delay_max_sec
    if delay_max is not None and delay_max <= delay:
        delay_max = None
    return BridgePolicy(
        allow=_normalize_peer_list(allow_chat_ids),
        deny=_normalize_peer_list(deny_chat_ids) or frozenset(),
        reply_delay_sec=delay,
        reply_delay_max_sec=delay_max,
    )


def override_policy(base: BridgePolicy, overrides: dict[str, object] | None = None) -> BridgePolicy:
    if not overrides:
        return base
    return build_policy(
        allow_chat_ids=overrides["allow_chat_ids"] if "allow_chat_ids" in overrides else (
            sorted(base.allow) if base.allow is not None else []
        ),
        deny_chat_ids=overrides["deny_chat_ids"] if "deny_chat_ids" in overrides else list(base.deny),
        reply_delay_sec=float(overrides.get("reply_delay_sec", base.reply_delay_sec)),
        reply_delay_max_sec=(
            float(overrides["reply_delay_max_sec"])
            if "reply_delay_max_sec" in overrides and overrides["reply_delay_max_sec"] is not None
            else base.reply_delay_max_sec
        ),
    )


def _peer_matches(candidate_keys: Iterable[str], allow_list: frozenset[str] | None, deny_list: frozenset[str]) -> bool:
    keys = {key for key in candidate_keys if key}
    if keys & deny_list:
        return False
    if allow_list is None:
        return True
    return bool(keys & allow_list)


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
        "ValueError",
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
    if name in forbidden_errors:
        return BridgeForbiddenError(f"Telegram denied permission to {action}.")
    if name in auth_errors:
        return BridgeUnavailableError("Telegram session is no longer valid. Re-create the session locally.")
    if name in rate_limit_errors:
        return BridgeRateLimitError(60, detail=f"Telegram temporarily limited {action}. Try again later.")
    return BridgeError(f"Telegram request failed while trying to {action}.")


class BridgeClient:
    """Wraps Telethon client with reply delay, allow/deny, and send lock."""

    def __init__(
        self,
        client: TelegramClient,
        *,
        reply_delay_sec: float = 2.0,
        reply_delay_max_sec: float | None = None,
        allow_chat_ids: list[str] | None = None,
        deny_chat_ids: list[str] | None = None,
        rpc_timeout_sec: float = 30.0,
        flood_wait_max_sleep_sec: float = 3.0,
    ) -> None:
        self._client = client
        self._policy = build_policy(
            allow_chat_ids=allow_chat_ids,
            deny_chat_ids=deny_chat_ids,
            reply_delay_sec=reply_delay_sec,
            reply_delay_max_sec=reply_delay_max_sec,
        )
        self._send_lock = asyncio.Lock()
        self._rpc_timeout_sec = max(1.0, rpc_timeout_sec)
        self._flood_wait_max_sleep_sec = max(0.0, flood_wait_max_sleep_sec)

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
        """Reconnect if disconnected. Return True if connected."""
        if not await self._is_connected():
            try:
                await self._client.connect()
            except Exception as exc:
                logger.warning("Reconnect failed: %s", type(exc).__name__)
                return False
        return await self._is_connected()

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

    async def _resolve_entity(self, peer: str | int, *, action: str) -> Any:
        if not await self.ensure_connected():
            raise BridgeUnavailableError("Telegram bridge is not connected.")
        return await self._call_telegram(self._client.get_entity, peer, action=action)

    def _check_allowed(self, *, policy: BridgePolicy, candidate_keys: set[str], peer: str | int) -> None:
        if not _peer_matches(candidate_keys, policy.allow, policy.deny):
            raise BridgeForbiddenError(f"Peer not allowed: {peer}")

    async def send_message(
        self,
        peer: str | int,
        text: str,
        reply_to: int | None = None,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        """Send one message with enforced policy."""
        policy = self._resolve_policy(policy_overrides)
        text = (text or "").strip()
        if not text:
            raise BridgeValidationError("Message text is empty")
        if len(text) > MAX_MESSAGE_LENGTH:
            raise BridgeValidationError(f"Message too long (max {MAX_MESSAGE_LENGTH} characters)")

        async with self._send_lock:
            entity = await self._resolve_entity(peer, action="send a message")
            candidate_keys = _build_candidate_keys(peer=peer, entity=entity)
            self._check_allowed(policy=policy, candidate_keys=candidate_keys, peer=peer)

            delay = self._get_delay(policy)
            await asyncio.sleep(delay)
            if not await self.ensure_connected():
                raise BridgeUnavailableError("Telegram bridge is not connected.")

            result = await self._call_telegram(
                self._client.send_message,
                entity,
                text,
                reply_to=reply_to,
                action="send a message",
                allow_flood_retry=True,
            )
            return {"ok": True, "message_id": getattr(result, "id", None)}

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
        }

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
            if not _peer_matches(candidate_keys, policy.allow, policy.deny):
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

    async def get_messages(
        self,
        peer: str | int,
        limit: int = 20,
        *,
        policy_overrides: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        policy = self._resolve_policy(policy_overrides)
        entity = await self._resolve_entity(peer, action="read messages")
        candidate_keys = _build_candidate_keys(peer=peer, entity=entity)
        self._check_allowed(policy=policy, candidate_keys=candidate_keys, peer=peer)

        messages = await self._call_telegram(
            self._client.get_messages,
            entity,
            limit=min(max(1, limit), 50),
            action="read messages",
        )
        out = []
        for message in messages:
            if message is None:
                continue
            out.append(
                {
                    "id": getattr(message, "id", None),
                    "text": getattr(message, "text", None) or "",
                    "date": str(getattr(message, "date", "")),
                    "out": getattr(message, "out", None),
                }
            )
        return out

    async def disconnect(self) -> None:
        if await self._is_connected():
            await self._client.disconnect()
