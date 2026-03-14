"""JSON-backed state stores for discovered sources and polling checkpoints."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable


def _now_ts() -> float:
    return time.time()


def _normalize_username(username: str | None) -> str | None:
    if not username:
        return None
    username = username.strip()
    if not username:
        return None
    return username[1:].lower() if username.startswith("@") else username.lower()


def _entity_title(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if isinstance(title, str) and title.strip():
        return title
    first_name = getattr(entity, "first_name", None) or ""
    last_name = getattr(entity, "last_name", None) or ""
    full_name = f"{first_name} {last_name}".strip()
    if full_name:
        return full_name
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return ""


def classify_dialog(dialog: Any) -> str:
    """Classify a Telethon dialog/entity into a coarse source type."""
    entity = getattr(dialog, "entity", None)

    if getattr(dialog, "is_user", False):
        return "user"
    if getattr(dialog, "is_group", False):
        return "group"
    if getattr(dialog, "is_channel", False):
        if getattr(entity, "forum", False):
            return "forum"
        if getattr(entity, "megagroup", False):
            return "supergroup"
        if getattr(entity, "broadcast", False):
            return "channel"
        return "channel"

    if entity is not None:
        if getattr(entity, "forum", False):
            return "forum"
        if getattr(entity, "megagroup", False):
            return "supergroup"
        if getattr(entity, "broadcast", False):
            return "channel"
        if getattr(entity, "title", None) is not None:
            return "group"
        if getattr(entity, "first_name", None) is not None or getattr(entity, "last_name", None) is not None:
            return "user"

    return "unknown"


def dialog_to_inventory_entry(dialog: Any) -> dict[str, Any]:
    """Convert a Telethon dialog to a serializable inventory entry."""
    entity = getattr(dialog, "entity", None)
    chat_id = getattr(dialog, "id", None) or getattr(entity, "id", None)
    username = getattr(entity, "username", None)
    dialog_type = classify_dialog(dialog)
    return {
        "peer_id": chat_id,
        "peer_key": str(chat_id) if chat_id is not None else (_normalize_username(username) or ""),
        "username": _normalize_username(username),
        "title": _entity_title(entity),
        "type": dialog_type,
        "is_forum": bool(getattr(entity, "forum", False)),
        "raw_username": username,
        "updated_at": _now_ts(),
    }


class JsonStateStore:
    def __init__(self, path: str | Path | None) -> None:
        self._path = Path(path).expanduser().resolve() if path else None
        self._cache: dict[str, Any] | None = None
        self._mtime_ns: int | None = None
        self._lock = threading.RLock()

    @property
    def path(self) -> Path | None:
        return self._path

    def _empty(self) -> dict[str, Any]:
        return {}

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self._path is None:
                if self._cache is None:
                    self._cache = self._empty()
                self._mtime_ns = None
                return self._cache

            if not self._path.exists():
                self._cache = self._empty()
                self._mtime_ns = None
                return self._cache

            stat = self._path.stat()
            if self._cache is not None and self._mtime_ns == stat.st_mtime_ns:
                return self._cache

            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"State file {self._path} contains invalid JSON: {exc.msg}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"State file {self._path} must contain a JSON object")
            self._cache = data
            self._mtime_ns = stat.st_mtime_ns
            return data

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            if self._path is None:
                self._cache = data
                self._mtime_ns = None
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self._cache = data
            self._mtime_ns = self._path.stat().st_mtime_ns


class SourceInventoryStore(JsonStateStore):
    """Tracks dialogs that may be used as read-only sources."""

    def _empty(self) -> dict[str, Any]:
        return {"meta": {"last_synced_at": None}, "dialogs": []}

    def last_synced_at(self) -> float | None:
        data = self.load()
        meta = data.get("meta", {})
        if not isinstance(meta, dict):
            return None
        value = meta.get("last_synced_at")
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def needs_refresh(self, refresh_sec: float) -> bool:
        last_synced_at = self.last_synced_at()
        if last_synced_at is None:
            return True
        return (_now_ts() - last_synced_at) >= max(0.0, refresh_sec)

    def replace_dialogs(self, dialogs: Iterable[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            entries = [dialog for dialog in dialogs if dialog.get("peer_key")]
            data = {
                "meta": {
                    "last_synced_at": _now_ts(),
                    "dialog_count": len(entries),
                },
                "dialogs": entries,
            }
            self.save(data)
            return data

    def list_dialogs(
        self,
        *,
        include_types: Iterable[str] | None = None,
        exclude_peers: Iterable[str | int] | None = None,
        exclude_usernames: Iterable[str] | None = None,
        only_sourceable: bool = True,
    ) -> list[dict[str, Any]]:
        data = self.load()
        dialogs = data.get("dialogs", [])
        if not isinstance(dialogs, list):
            return []

        allowed_types = {item.strip().lower() for item in include_types or [] if str(item).strip()}
        excluded_peer_keys = {str(peer).strip() for peer in exclude_peers or [] if str(peer).strip()}
        excluded_usernames = {
            username
            for raw in (exclude_usernames or [])
            if (username := _normalize_username(str(raw)))
        }

        result: list[dict[str, Any]] = []
        for entry in dialogs:
            if not isinstance(entry, dict):
                continue
            peer_key = str(entry.get("peer_key", "")).strip()
            username = _normalize_username(entry.get("username"))
            dialog_type = str(entry.get("type", "unknown")).lower()
            sourceable = True

            if allowed_types and dialog_type not in allowed_types:
                sourceable = False
            if peer_key in excluded_peer_keys:
                sourceable = False
            if username and username in excluded_usernames:
                sourceable = False
            if dialog_type == "user":
                sourceable = False

            enriched = dict(entry)
            enriched["sourceable"] = sourceable
            if only_sourceable and not sourceable:
                continue
            result.append(enriched)

        result.sort(key=lambda item: (str(item.get("title", "")).lower(), str(item.get("peer_key", ""))))
        return result

    def allowed_identifiers(
        self,
        *,
        include_types: Iterable[str] | None = None,
        exclude_peers: Iterable[str | int] | None = None,
        exclude_usernames: Iterable[str] | None = None,
    ) -> list[str]:
        dialogs = self.list_dialogs(
            include_types=include_types,
            exclude_peers=exclude_peers,
            exclude_usernames=exclude_usernames,
            only_sourceable=True,
        )
        identifiers: list[str] = []
        seen: set[str] = set()
        for dialog in dialogs:
            for value in (dialog.get("peer_key"), dialog.get("username")):
                normalized = str(value or "").strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                identifiers.append(normalized)
        return identifiers


class DmCursorStore(JsonStateStore):
    """Tracks the highest acknowledged inbound DM message id per consumer and sender."""

    def _empty(self) -> dict[str, Any]:
        return {"consumers": {}}

    def get_cursor(self, consumer_id: str, sender_key: str) -> int:
        data = self.load()
        consumers = data.get("consumers", {})
        if not isinstance(consumers, dict):
            return 0
        consumer = consumers.get(consumer_id, {})
        if not isinstance(consumer, dict):
            return 0
        value = consumer.get(sender_key, 0)
        return int(value) if isinstance(value, (int, float)) else 0

    def get_consumer_cursors(self, consumer_id: str) -> dict[str, int]:
        data = self.load()
        consumers = data.get("consumers", {})
        if not isinstance(consumers, dict):
            return {}
        consumer = consumers.get(consumer_id, {})
        if not isinstance(consumer, dict):
            return {}
        return {
            str(sender_key): int(value)
            for sender_key, value in consumer.items()
            if isinstance(value, (int, float))
        }

    def ack(self, consumer_id: str, sender_key: str, message_id: int) -> int:
        with self._lock:
            if message_id <= 0:
                return self.get_cursor(consumer_id, sender_key)

            data = self.load()
            consumers = data.setdefault("consumers", {})
            if not isinstance(consumers, dict):
                consumers = {}
                data["consumers"] = consumers
            consumer = consumers.setdefault(consumer_id, {})
            if not isinstance(consumer, dict):
                consumer = {}
                consumers[consumer_id] = consumer

            current = consumer.get(sender_key, 0)
            current_id = int(current) if isinstance(current, (int, float)) else 0
            if message_id > current_id:
                consumer[sender_key] = int(message_id)
                self.save(data)
                return int(message_id)
            return current_id
