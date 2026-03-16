"""QR-code-based Telegram authentication: state machine + optional HTTP server.

Two usage modes:

1. Standalone (first install, ``auth-qr`` CLI subcommand):
   - Starts a temporary FastAPI HTTP server on localhost.
   - Skill connects to it, fetches the QR PNG/ASCII, shows to user.
   - Skill polls ``GET /status``; when ``awaiting_password`` asks user for 2FA
     and posts to ``POST /password``.
   - Server exits when auth is done; .session file is saved.

2. Embedded (live re-auth inside the running bridge, ``server.py``):
   - ``QrAuthContext`` and ``run_qr_login_flow`` are imported directly.
   - The bridge server manages the task and exposes ``/auth/qr/*`` endpoints
     protected by the existing API token.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import io
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public state types
# ---------------------------------------------------------------------------

class QrState(str, Enum):
    AWAITING_SCAN = "awaiting_scan"
    AWAITING_PASSWORD = "awaiting_password"
    DONE = "done"
    ERROR = "error"


@dataclass
class QrAuthContext:
    """Mutable state shared between the QR login flow coroutine and the HTTP layer."""

    state: QrState = QrState.AWAITING_SCAN
    qr_url: str = ""
    # monotonic timestamp when current QR expires (approximate)
    qr_expires_at: float = 0.0
    qr_png_b64: str = ""
    qr_ascii: str = ""
    error: str | None = None

    # asyncio primitives – always instantiated inside a running event loop
    _password_queue: asyncio.Queue[str] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1)
    )
    _done_event: asyncio.Event = field(default_factory=asyncio.Event)


# ---------------------------------------------------------------------------
# QR generation helpers
# ---------------------------------------------------------------------------

def generate_qr_png_b64(url: str) -> str:
    """Return QR code PNG encoded as a base64 string.

    Requires ``qrcode[pil]`` (Pillow) to be installed.
    """
    import qrcode  # type: ignore[import-untyped]

    qr = qrcode.make(url)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    buf.seek(0)
    return base64.b64encode(buf.getvalue()).decode()


def generate_qr_ascii(url: str) -> str:
    """Return QR code as an inverted ASCII-art string (terminal-friendly)."""
    import qrcode  # type: ignore[import-untyped]

    qr = qrcode.QRCode()
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def _refresh_qr(ctx: QrAuthContext, qr_login: Any) -> None:
    """Update *ctx* with fresh QR data from a Telethon ``QRLogin`` object."""
    url: str | bytes = qr_login.url
    if isinstance(url, bytes):
        url = url.decode()
    ctx.qr_url = url
    # Use the server-authoritative expiry time from qr_login.expires (datetime UTC).
    try:
        remaining = (
            qr_login.expires - datetime.datetime.now(tz=datetime.timezone.utc)
        ).total_seconds()
        ctx.qr_expires_at = time.monotonic() + max(remaining, 0.0)
    except Exception:
        ctx.qr_expires_at = time.monotonic() + 30.0

    try:
        ctx.qr_png_b64 = generate_qr_png_b64(url)
    except Exception:
        logger.warning("Failed to generate QR PNG", exc_info=True)
        ctx.qr_png_b64 = ""

    try:
        ctx.qr_ascii = generate_qr_ascii(url)
    except Exception:
        logger.warning("Failed to generate QR ASCII", exc_info=True)
        ctx.qr_ascii = ""


# ---------------------------------------------------------------------------
# Core QR login state machine (shared between standalone and embedded modes)
# ---------------------------------------------------------------------------

async def run_qr_login_flow(client: Any, ctx: QrAuthContext) -> None:
    """Drive *ctx* through the QR login state machine.

    The caller must have called ``await client.connect()`` beforehand.
    On completion (success), ``ctx.state`` is ``DONE`` and ``ctx._done_event``
    is set.  On failure, ``ctx.state`` is ``ERROR`` and the exception is
    re-raised.
    """
    from telethon.errors import SessionPasswordNeededError  # type: ignore[import-untyped]

    try:
        qr_login = await client.qr_login()
        _refresh_qr(ctx, qr_login)
        ctx.state = QrState.AWAITING_SCAN

        while True:
            try:
                await qr_login.wait()
                # Successfully scanned, no 2FA required.
                ctx.state = QrState.DONE
                ctx._done_event.set()
                return
            except asyncio.TimeoutError:
                # QR expired – regenerate and keep waiting.
                await qr_login.recreate()
                _refresh_qr(ctx, qr_login)
            except SessionPasswordNeededError:
                # QR was scanned but account has 2FA.
                ctx.state = QrState.AWAITING_PASSWORD
                password = await ctx._password_queue.get()
                await client.sign_in(password=password)
                ctx.state = QrState.DONE
                ctx._done_event.set()
                return
    except Exception as exc:
        ctx.state = QrState.ERROR
        ctx.error = str(exc)
        ctx._done_event.set()
        raise


# ---------------------------------------------------------------------------
# Standalone mini HTTP server (for the ``auth-qr`` CLI subcommand)
# ---------------------------------------------------------------------------

async def _standalone_serve(ctx: QrAuthContext, host: str, port: int) -> None:
    """Run a minimal FastAPI server exposing QR state until auth completes."""
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
    import uvicorn

    mini_app = FastAPI(title="OpenClaw QR Auth", docs_url=None, redoc_url=None)

    @mini_app.get("/qr")
    async def get_qr():
        return {
            "state": ctx.state,
            "qr_url": ctx.qr_url,
            "qr_png_b64": ctx.qr_png_b64,
            "qr_ascii": ctx.qr_ascii,
            "expires_at": ctx.qr_expires_at,
        }

    @mini_app.get("/status")
    async def get_status():
        return {"state": ctx.state, "error": ctx.error}

    class _PasswordBody(BaseModel):
        password: str

    @mini_app.post("/password")
    async def post_password(body: _PasswordBody):
        if ctx.state != QrState.AWAITING_PASSWORD:
            raise HTTPException(
                status_code=409,
                detail=f"Not awaiting password; current state: {ctx.state}",
            )
        try:
            ctx._password_queue.put_nowait(body.password)
        except asyncio.QueueFull:
            raise HTTPException(status_code=409, detail="Password already submitted")
        return {"status": "accepted"}

    config = uvicorn.Config(mini_app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    async def _stop_when_done() -> None:
        await ctx._done_event.wait()
        server.should_exit = True

    await asyncio.gather(server.serve(), _stop_when_done())


# ---------------------------------------------------------------------------
# Entry point for the ``auth-qr`` CLI subcommand
# ---------------------------------------------------------------------------

def main_qr(
    *,
    session_path: str | None = None,
    print_session_string: bool = False,
    session_string_out: str | None = None,
    api_id_value: str | None = None,
    api_hash_value: str | None = None,
    listen: str = "127.0.0.1:8767",
) -> None:
    """Interactive QR-based session creation.  Must run locally, not on a VPS."""
    import getpass

    from telethon import TelegramClient  # type: ignore[import-untyped]
    from telethon.sessions import StringSession  # type: ignore[import-untyped]

    from openclaw_tg_bridge.auth import resolve_local_session_file, write_session_string

    print("OpenClaw Telegram Bridge — create session via QR code (do not run on VPS).")
    print("Get api_id and api_hash from https://my.telegram.org (API Development tools).\n")

    # -- API credentials --------------------------------------------------- #
    api_id_s = (api_id_value or os.environ.get("TELEGRAM_API_ID", "")).strip()
    if not api_id_s:
        api_id_s = input("API ID ").strip()
    if not api_id_s:
        print("API ID is required.", file=sys.stderr)
        sys.exit(1)
    try:
        api_id = int(api_id_s)
    except ValueError:
        print("API ID must be a number.", file=sys.stderr)
        sys.exit(1)

    api_hash = (api_hash_value or os.environ.get("TELEGRAM_API_HASH", "")).strip()
    if not api_hash:
        api_hash = getpass.getpass("API Hash ").strip()
    if not api_hash:
        print("API Hash is required.", file=sys.stderr)
        sys.exit(1)

    if session_path is None:
        session_path = (
            input("Session file path (e.g. ./openclaw_user) [./openclaw_user] ").strip()
            or "./openclaw_user"
        )

    session_file = resolve_local_session_file(session_path)
    session_name = str(session_file.with_suffix(""))  # Telethon appends .session

    # -- Parse listen address ---------------------------------------------- #
    if ":" in listen:
        host, port_s = listen.rsplit(":", 1)
        port = int(port_s)
    else:
        host, port = listen, 8767

    # ---------------------------------------------------------------------- #
    async def _run() -> None:
        client = TelegramClient(session_name, api_id, api_hash)
        await client.connect()

        if await client.is_user_authorized():
            me = await client.get_me()
            await client.disconnect()
            print(
                f"\nSession already authorized as "
                f"{getattr(me, 'first_name', '')} {getattr(me, 'last_name', '')}"
                f" (@{getattr(me, 'username', '')})."
            )
            return

        ctx = QrAuthContext()

        print(f"\nQR auth server started on http://{host}:{port}")
        print("  GET  /qr       — QR code (PNG base64 + ASCII art + expiry)")
        print("  GET  /status   — current auth state")
        print("  POST /password — submit 2FA password if prompted")
        print(
            "\nOpen your Telegram app → Settings → Devices → Link Desktop Device"
            " and scan the QR code shown by the skill."
        )
        print("The QR refreshes automatically on expiry (typically ~30 seconds) if not scanned.\n")

        async def _flow() -> None:
            try:
                await run_qr_login_flow(client, ctx)
            except Exception:
                pass  # state already set to ERROR

        await asyncio.gather(_flow(), _standalone_serve(ctx, host, port))

        if ctx.state != QrState.DONE:
            print(f"\nAuth failed: {ctx.error}", file=sys.stderr)
            await client.disconnect()
            sys.exit(1)

        me = await client.get_me()
        session_string = StringSession.save(client.session)
        await client.disconnect()

        print("\nSession created successfully.")
        print(f"Session file: {session_file}")
        print(
            f"User: {getattr(me, 'first_name', '')} {getattr(me, 'last_name', '')}"
            f" (@{getattr(me, 'username', '')})"
        )

        if print_session_string:
            print("\nSession string:")
            print(session_string)

        if session_string_out:
            write_session_string(session_string_out, session_string)
            print(
                f"\nSession string written to: {Path(session_string_out).expanduser().resolve()}"
            )

        print("\nNext steps:")
        print(
            "1. Either copy the .session file(s) to your VPS or set"
            " TELEGRAM_SESSION_STRING on the VPS."
        )
        print("2. Set on VPS: TELEGRAM_API_ID and TELEGRAM_API_HASH.")
        print(
            "3. Start the bridge service on VPS with TELEGRAM_SESSION_PATH or"
            " TELEGRAM_SESSION_STRING."
        )

    asyncio.run(_run())


def cli_qr(argv: list[str] | None = None) -> None:
    """Argument-parser entry point for the ``auth-qr`` subcommand."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Create a Telegram session via QR code for OpenClaw"
    )
    parser.add_argument(
        "--session-path", default=None, help="Session file path (default: ./openclaw_user)"
    )
    parser.add_argument(
        "--api-id", default=None, help="Telegram API ID (or use TELEGRAM_API_ID env var)"
    )
    parser.add_argument(
        "--api-hash", default=None, help="Telegram API hash (or use TELEGRAM_API_HASH env var)"
    )
    parser.add_argument(
        "--listen",
        default="127.0.0.1:8767",
        help="host:port for the local QR auth HTTP server (default: 127.0.0.1:8767)",
    )
    parser.add_argument(
        "--print-session-string",
        action="store_true",
        help="Print the generated StringSession to stdout",
    )
    parser.add_argument(
        "--session-string-out",
        default=None,
        help="Write the generated StringSession to a file",
    )
    args = parser.parse_args(argv)
    main_qr(
        session_path=args.session_path,
        print_session_string=args.print_session_string,
        session_string_out=args.session_string_out,
        api_id_value=args.api_id,
        api_hash_value=args.api_hash,
        listen=args.listen,
    )


if __name__ == "__main__":
    cli_qr()
