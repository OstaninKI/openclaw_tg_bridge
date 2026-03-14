"""Configuration from environment and request overrides."""

import os
from pathlib import Path
from typing import Mapping

HEADER_REPLY_DELAY_SEC = "x-openclaw-reply-delay-sec"
HEADER_REPLY_DELAY_MAX_SEC = "x-openclaw-reply-delay-max-sec"
HEADER_ALLOW_FROM = "x-openclaw-allow-from"
HEADER_DENY_FROM = "x-openclaw-deny-from"


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
    return [p.strip() for p in raw.split(",") if p.strip()]


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


def load_config() -> dict:
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
    api_token = os.environ.get("TELEGRAM_BRIDGE_API_TOKEN", "").strip()
    rpc_timeout_sec = _get_float("TELEGRAM_RPC_TIMEOUT_SEC") or 30.0
    flood_wait_max_sleep_sec = _get_float("TELEGRAM_FLOOD_WAIT_MAX_SLEEP_SEC") or 3.0

    # Proxy (optional): SOCKS5 or MTProxy via env
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
        "api_token": api_token or None,
        "rpc_timeout_sec": max(1.0, rpc_timeout_sec),
        "flood_wait_max_sleep_sec": max(0.0, flood_wait_max_sleep_sec),
        "proxy": proxy,
    }


def resolve_session_path(session_path: str | None) -> Path | None:
    """Resolve session file path; expand user and make absolute."""
    if not session_path:
        return None
    p = Path(session_path).expanduser().resolve()
    if p.suffix != ".session":
        p = p.with_suffix(p.suffix + ".session" if not p.suffix else ".session")
    return p


def parse_request_overrides(headers: Mapping[str, str]) -> dict:
    """Parse optional backend policy overrides from HTTP headers."""
    reply_delay_raw = _get_header(headers, HEADER_REPLY_DELAY_SEC)
    reply_delay_max_raw = _get_header(headers, HEADER_REPLY_DELAY_MAX_SEC)
    allow_raw = _get_header(headers, HEADER_ALLOW_FROM)
    deny_raw = _get_header(headers, HEADER_DENY_FROM)

    overrides: dict[str, object] = {}
    if reply_delay_raw is not None:
        overrides["reply_delay_sec"] = max(0.0, _parse_float(reply_delay_raw, "reply delay"))
    if reply_delay_max_raw is not None:
        overrides["reply_delay_max_sec"] = max(0.0, _parse_float(reply_delay_max_raw, "reply delay max"))
    if allow_raw is not None:
        overrides["allow_chat_ids"] = _parse_list_header(allow_raw)
    if deny_raw is not None:
        overrides["deny_chat_ids"] = _parse_list_header(deny_raw)
    return overrides
