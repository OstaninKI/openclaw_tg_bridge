"""Configuration from environment, policy files, and request overrides."""

import json
import os
from pathlib import Path
from typing import Any, Mapping

HEADER_POLICY_PROFILE = "x-openclaw-policy-profile"
HEADER_REPLY_DELAY_SEC = "x-openclaw-reply-delay-sec"
HEADER_REPLY_DELAY_MAX_SEC = "x-openclaw-reply-delay-max-sec"
HEADER_ALLOW_FROM = "x-openclaw-allow-from"
HEADER_DENY_FROM = "x-openclaw-deny-from"
HEADER_WRITE_TO = "x-openclaw-write-to"
HEADER_DENY_WRITE_TO = "x-openclaw-deny-write-to"


def _get_int(key: str, default: int | None = None) -> int | None:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(key: str, default: float | None = None) -> float | None:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _list_from_env(key: str) -> list[str]:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _get_header(headers: Mapping[str, str], key: str) -> str | None:
    for header_name, value in headers.items():
        if header_name.lower() == key:
            return value
    return None


def _parse_float(raw: str, key: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {key}: {raw!r}") from exc


def _parse_list_header(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _deep_merge_dicts(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict[str, Any]:
    """Load bridge config from environment."""
    api_id = _get_int("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    session_path = os.environ.get("TELEGRAM_SESSION_PATH", "").strip()
    session_string = os.environ.get("TELEGRAM_SESSION_STRING", "").strip()
    listen = os.environ.get("TELEGRAM_BRIDGE_LISTEN", "127.0.0.1:8765").strip()
    reply_delay_sec = _get_float("TELEGRAM_REPLY_DELAY_SEC") or 2.0
    reply_delay_max_sec = _get_float("TELEGRAM_REPLY_DELAY_MAX_SEC")
    allow_chat_ids = _list_from_env("TELEGRAM_ALLOW_CHAT_IDS")
    deny_chat_ids = _list_from_env("TELEGRAM_DENY_CHAT_IDS")
    write_allow_chat_ids = _list_from_env("TELEGRAM_WRITE_ALLOW_CHAT_IDS")
    write_deny_chat_ids = _list_from_env("TELEGRAM_WRITE_DENY_CHAT_IDS")
    api_token = os.environ.get("TELEGRAM_BRIDGE_API_TOKEN", "").strip()
    rpc_timeout_sec = _get_float("TELEGRAM_RPC_TIMEOUT_SEC") or 30.0
    flood_wait_max_sleep_sec = _get_float("TELEGRAM_FLOOD_WAIT_MAX_SLEEP_SEC") or 3.0
    policy_path = os.environ.get("TELEGRAM_POLICY_PATH", "").strip()
    policy_default_profile = os.environ.get("TELEGRAM_POLICY_DEFAULT_PROFILE", "").strip()
    default_state_dir = (
        Path(policy_path).expanduser().resolve().parent
        if policy_path
        else (Path.home() / ".openclaw" / "telethon")
    )
    sources_inventory_path = os.environ.get("TELEGRAM_SOURCES_INVENTORY_PATH", "").strip()
    inbox_state_path = os.environ.get("TELEGRAM_INBOX_STATE_PATH", "").strip()
    sources_refresh_sec = _get_float("TELEGRAM_SOURCES_REFRESH_SEC") or 300.0
    sources_dialog_limit = _get_int("TELEGRAM_SOURCES_DIALOG_LIMIT") or 500

    proxy = None
    proxy_type = os.environ.get("TELEGRAM_PROXY_TYPE", "").strip().lower()
    if proxy_type == "socks5":
        host = os.environ.get("TELEGRAM_PROXY_HOST", "").strip()
        port = _get_int("TELEGRAM_PROXY_PORT")
        if host and port:
            proxy = ("socks5", host, port)
    elif proxy_type == "mtproxy":
        host = os.environ.get("TELEGRAM_PROXY_HOST", "").strip()
        port = _get_int("TELEGRAM_PROXY_PORT")
        secret = os.environ.get("TELEGRAM_PROXY_SECRET", "00000000000000000000000000000000").strip()
        if host and port:
            proxy = ("mtproxy", host, port, secret)

    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "session_path": session_path or None,
        "session_string": session_string or None,
        "listen": listen,
        "reply_delay_sec": max(0.0, reply_delay_sec),
        "reply_delay_max_sec": reply_delay_max_sec if reply_delay_max_sec is not None else None,
        "allow_chat_ids": allow_chat_ids,
        "deny_chat_ids": deny_chat_ids,
        "write_allow_chat_ids": write_allow_chat_ids,
        "write_deny_chat_ids": write_deny_chat_ids,
        "api_token": api_token or None,
        "rpc_timeout_sec": max(1.0, rpc_timeout_sec),
        "flood_wait_max_sleep_sec": max(0.0, flood_wait_max_sleep_sec),
        "proxy": proxy,
        "policy_path": policy_path or None,
        "policy_default_profile": policy_default_profile or None,
        "sources_inventory_path": sources_inventory_path or str(default_state_dir / "sources_inventory.json"),
        "inbox_state_path": inbox_state_path or str(default_state_dir / "dm_inbox_state.json"),
        "sources_refresh_sec": max(0.0, sources_refresh_sec),
        "sources_dialog_limit": min(max(50, sources_dialog_limit), 2000),
    }


def resolve_session_path(session_path: str | None) -> Path | None:
    """Resolve session file path; expand user and make absolute."""
    if not session_path:
        return None
    path = Path(session_path).expanduser().resolve()
    if path.suffix != ".session":
        path = path.with_suffix(path.suffix + ".session" if not path.suffix else ".session")
    return path


class PolicyStore:
    """Reloadable JSON policy store for profile-based access rules."""

    def __init__(self, policy_path: str | None) -> None:
        self._path = Path(policy_path).expanduser().resolve() if policy_path else None
        self._cache: dict[str, Any] = {}
        self._mtime_ns: int | None = None

    def _load(self) -> dict[str, Any]:
        if self._path is None or not self._path.exists():
            self._cache = {}
            self._mtime_ns = None
            return self._cache

        stat = self._path.stat()
        if self._mtime_ns == stat.st_mtime_ns:
            return self._cache

        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Policy file must contain a JSON object")
        self._cache = data
        self._mtime_ns = stat.st_mtime_ns
        return self._cache

    def resolve(self, profile_id: str | None) -> dict[str, Any]:
        data = self._load()
        defaults = data.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ValueError("Policy file field 'defaults' must be an object")
        if not profile_id:
            return defaults

        profiles = data.get("profiles", {})
        if not isinstance(profiles, dict):
            raise ValueError("Policy file field 'profiles' must be an object")
        if profile_id not in profiles:
            raise ValueError(f"Unknown policy profile: {profile_id}")

        profile = profiles[profile_id]
        if not isinstance(profile, dict):
            raise ValueError(f"Policy profile {profile_id!r} must be an object")
        return _deep_merge_dicts(defaults, profile)


def parse_request_overrides(headers: Mapping[str, str]) -> dict[str, Any]:
    """Parse optional backend policy overrides from HTTP headers."""
    policy_profile = _get_header(headers, HEADER_POLICY_PROFILE)
    reply_delay_raw = _get_header(headers, HEADER_REPLY_DELAY_SEC)
    reply_delay_max_raw = _get_header(headers, HEADER_REPLY_DELAY_MAX_SEC)
    allow_raw = _get_header(headers, HEADER_ALLOW_FROM)
    deny_raw = _get_header(headers, HEADER_DENY_FROM)
    write_allow_raw = _get_header(headers, HEADER_WRITE_TO)
    write_deny_raw = _get_header(headers, HEADER_DENY_WRITE_TO)

    overrides: dict[str, Any] = {}
    if policy_profile is not None:
        overrides["policy_profile"] = policy_profile.strip() or None
    if reply_delay_raw is not None:
        overrides["reply_delay_sec"] = max(0.0, _parse_float(reply_delay_raw, "reply delay"))
    if reply_delay_max_raw is not None:
        overrides["reply_delay_max_sec"] = max(0.0, _parse_float(reply_delay_max_raw, "reply delay max"))
    if allow_raw is not None:
        overrides["read_allow_chat_ids"] = _parse_list_header(allow_raw)
    if deny_raw is not None:
        overrides["read_deny_chat_ids"] = _parse_list_header(deny_raw)
    if write_allow_raw is not None:
        overrides["write_allow_chat_ids"] = _parse_list_header(write_allow_raw)
    if write_deny_raw is not None:
        overrides["write_deny_chat_ids"] = _parse_list_header(write_deny_raw)
    return overrides


def resolve_effective_policy(
    config: dict[str, Any],
    policy_store: PolicyStore,
    request_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge env config, policy file, and request overrides into one effective policy."""
    request_overrides = dict(request_overrides or {})
    profile_id = request_overrides.get("policy_profile")
    if profile_id is None:
        profile_id = config.get("policy_default_profile")

    merged: dict[str, Any] = {
        "policy_profile": profile_id,
        "reply_delay_sec": config["reply_delay_sec"],
        "reply_delay_max_sec": config["reply_delay_max_sec"],
        "read_allow_chat_ids": list(config["allow_chat_ids"]) if config["allow_chat_ids"] else None,
        "read_deny_chat_ids": list(config["deny_chat_ids"]) if config["deny_chat_ids"] else None,
        "write_allow_chat_ids": list(config["write_allow_chat_ids"]),
        "write_deny_chat_ids": list(config["write_deny_chat_ids"]) if config["write_deny_chat_ids"] else None,
        "sources_auto_discover": False,
        "sources_include_types": None,
        "sources_exclude_peers": [],
        "sources_exclude_usernames": [],
    }

    policy_data = policy_store.resolve(profile_id)
    if policy_data:
        read_policy = policy_data.get("read", {})
        write_policy = policy_data.get("write", {})
        sources_policy = policy_data.get("sources", {})
        if (
            not isinstance(read_policy, dict)
            or not isinstance(write_policy, dict)
            or not isinstance(sources_policy, dict)
        ):
            raise ValueError("Policy file read/write/sources sections must be objects")

        if "replyDelaySec" in policy_data:
            merged["reply_delay_sec"] = max(0.0, float(policy_data["replyDelaySec"]))
        if "replyDelayMaxSec" in policy_data and policy_data["replyDelayMaxSec"] is not None:
            merged["reply_delay_max_sec"] = max(0.0, float(policy_data["replyDelayMaxSec"]))

        if "allow" in read_policy:
            merged["read_allow_chat_ids"] = list(read_policy["allow"] or [])
        if "deny" in read_policy:
            merged["read_deny_chat_ids"] = list(read_policy["deny"] or [])
        if "allow" in write_policy:
            merged["write_allow_chat_ids"] = list(write_policy["allow"] or [])
        if "deny" in write_policy:
            merged["write_deny_chat_ids"] = list(write_policy["deny"] or [])
        if "autoDiscover" in sources_policy:
            merged["sources_auto_discover"] = bool(sources_policy["autoDiscover"])
        if "includeTypes" in sources_policy:
            merged["sources_include_types"] = list(sources_policy["includeTypes"] or [])
        if "excludePeers" in sources_policy:
            merged["sources_exclude_peers"] = list(sources_policy["excludePeers"] or [])
        if "excludeUsernames" in sources_policy:
            merged["sources_exclude_usernames"] = list(sources_policy["excludeUsernames"] or [])

    for key in (
        "reply_delay_sec",
        "reply_delay_max_sec",
        "read_allow_chat_ids",
        "read_deny_chat_ids",
        "write_allow_chat_ids",
        "write_deny_chat_ids",
    ):
        if key in request_overrides:
            merged[key] = request_overrides[key]

    return merged
