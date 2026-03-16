"""Entry point: auth CLI or run server."""

import argparse
import logging
import sys

from openclaw_tg_bridge.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw Telegram Bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    auth_p = sub.add_parser("auth", help="Create session locally via phone + code (interactive)")
    auth_p.add_argument("--session-path", default=None, help="Session file path")
    auth_p.add_argument("--api-id", default=None, help="Telegram API ID (or use TELEGRAM_API_ID)")
    auth_p.add_argument("--api-hash", default=None, help="Telegram API hash (or use TELEGRAM_API_HASH)")
    auth_p.add_argument("--phone", default=None, help="Telegram phone number (or use TELEGRAM_PHONE)")
    auth_p.add_argument(
        "--print-session-string",
        action="store_true",
        help="Print the generated StringSession to stdout",
    )
    auth_p.add_argument(
        "--session-string-out",
        default=None,
        help="Write the generated StringSession to a file",
    )

    auth_qr_p = sub.add_parser(
        "auth-qr",
        help="Create session locally via QR code (interactive, local only)",
    )
    auth_qr_p.add_argument("--session-path", default=None, help="Session file path")
    auth_qr_p.add_argument("--api-id", default=None, help="Telegram API ID (or use TELEGRAM_API_ID)")
    auth_qr_p.add_argument("--api-hash", default=None, help="Telegram API hash (or use TELEGRAM_API_HASH)")
    auth_qr_p.add_argument(
        "--listen",
        default="127.0.0.1:8767",
        help="host:port for the local QR auth HTTP server (default: 127.0.0.1:8767)",
    )
    auth_qr_p.add_argument(
        "--print-session-string",
        action="store_true",
        help="Print the generated StringSession to stdout",
    )
    auth_qr_p.add_argument(
        "--session-string-out",
        default=None,
        help="Write the generated StringSession to a file",
    )

    run_p = sub.add_parser("run", help="Run HTTP bridge server")
    run_p.add_argument("--host", default=None, help="Override listen host")
    run_p.add_argument("--port", type=int, default=None, help="Override listen port")
    args = parser.parse_args()

    if args.command == "auth":
        from openclaw_tg_bridge.auth import main as auth_main

        auth_main(
            session_path=args.session_path,
            print_session_string=args.print_session_string,
            session_string_out=args.session_string_out,
            api_id_value=args.api_id,
            api_hash_value=args.api_hash,
            phone_value=args.phone,
        )
        return

    if args.command == "auth-qr":
        from openclaw_tg_bridge.auth_qr import main_qr

        main_qr(
            session_path=args.session_path,
            print_session_string=args.print_session_string,
            session_string_out=args.session_string_out,
            api_id_value=args.api_id,
            api_hash_value=args.api_hash,
            listen=args.listen,
        )
        return

    if args.command == "run":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        cfg = load_config()
        listen = cfg["listen"]
        if ":" in listen:
            host, port_s = listen.rsplit(":", 1)
            port = int(port_s)
        else:
            host, port = listen, 8765
        if args.host is not None:
            host = args.host
        if args.port is not None:
            port = args.port
        import uvicorn
        from openclaw_tg_bridge.server import app
        uvicorn.run(app, host=host, port=port, log_level="info")
        return

    sys.exit(1)


if __name__ == "__main__":
    main()
