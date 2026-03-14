"""Interactive auth CLI: create session locally (phone + code + optional 2FA)."""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from openclaw_tg_bridge.config import resolve_session_path


def _prompt(prompt: str, default: str | None = None, secret: bool = False) -> str:
    if default is not None:
        prompt = f"{prompt} [{default}] "
    else:
        prompt = f"{prompt} "
    if secret:
        return (getpass.getpass(prompt) or default or "").strip()
    return (input(prompt) or default or "").strip()


def resolve_local_session_file(session_path: str | None) -> Path:
    resolved = resolve_session_path(session_path or "./openclaw_user")
    if resolved is None:
        raise ValueError("Session path is required")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def write_session_string(path: str | None, session_string: str) -> None:
    if not path:
        return
    output_path = Path(path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(f"{session_string}\n", encoding="utf-8")


def main(
    *,
    session_path: str | None = None,
    print_session_string: bool = False,
    session_string_out: str | None = None,
) -> None:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    print("OpenClaw Telegram Bridge — create session locally (do not run on VPS).")
    print("Get api_id and api_hash from https://my.telegram.org (API Development tools).\n")

    api_id_s = _prompt("API ID")
    if not api_id_s:
        print("API ID is required.", file=sys.stderr)
        sys.exit(1)
    try:
        api_id = int(api_id_s)
    except ValueError:
        print("API ID must be a number.", file=sys.stderr)
        sys.exit(1)

    api_hash = _prompt("API Hash", secret=True)
    if not api_hash:
        print("API Hash is required.", file=sys.stderr)
        sys.exit(1)

    if session_path is None:
        session_path = _prompt("Session file path (e.g. ./openclaw_user)", default="./openclaw_user")
    session_file = resolve_local_session_file(session_path)
    session_name = str(session_file.with_suffix(""))  # Telethon adds .session

    async def run() -> None:
        client = TelegramClient(session_name, api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            phone = _prompt("Phone number (international format, e.g. +79001234567)")
            if not phone:
                print("Phone is required.", file=sys.stderr)
                await client.disconnect()
                sys.exit(1)
            sent = await client.send_code_request(phone)
            code = _prompt("Code from Telegram")
            if not code:
                print("Code is required.", file=sys.stderr)
                await client.disconnect()
                sys.exit(1)
            try:
                await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            except SessionPasswordNeededError:
                password = _prompt("2FA password", secret=True)
                await client.sign_in(password=password)

        me = await client.get_me()
        session_string = StringSession.save(client.session)
        await client.disconnect()

        print("\nSession created successfully.")
        print(f"Session file: {session_file}")
        print(
            f"User: {getattr(me, 'first_name', '')} {getattr(me, 'last_name', '')} "
            f"(@{getattr(me, 'username', '')})"
        )

        if print_session_string:
            print("\nSession string:")
            print(session_string)

        if session_string_out:
            write_session_string(session_string_out, session_string)
            print(f"\nSession string written to: {Path(session_string_out).expanduser().resolve()}")

        print("\nNext steps:")
        print("1. Either copy the .session file(s) to your VPS or set TELEGRAM_SESSION_STRING on the VPS.")
        print("2. Set on VPS: TELEGRAM_API_ID and TELEGRAM_API_HASH.")
        print("3. Start the bridge service on VPS with TELEGRAM_SESSION_PATH or TELEGRAM_SESSION_STRING.")

    asyncio.run(run())


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create a Telegram session locally for OpenClaw")
    parser.add_argument("--session-path", default=None, help="Session file path (default: ./openclaw_user)")
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
    main(
        session_path=args.session_path,
        print_session_string=args.print_session_string,
        session_string_out=args.session_string_out,
    )


if __name__ == "__main__":
    cli()
