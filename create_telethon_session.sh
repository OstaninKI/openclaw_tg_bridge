#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
BACKEND_DIR="$ROOT_DIR/backend"
DEFAULT_SESSION_PATH="$ROOT_DIR/openclaw_user.session"

usage() {
  echo "Usage: sh create_telethon_session.sh [session_path] [extra auth args...]" >&2
  echo "" >&2
  echo "Phone+code auth (default):" >&2
  echo "  sh create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session --print-session-string" >&2
  echo "" >&2
  echo "QR code auth:" >&2
  echo "  sh create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session --use-qr [--listen 127.0.0.1:8767]" >&2
  echo "" >&2
  echo "With --use-qr the script starts a local HTTP server on 127.0.0.1:8767 (configurable via --listen)." >&2
  echo "The setup skill (or any HTTP client) polls GET /qr to fetch the QR code and GET /status to check progress." >&2
  echo "If 2FA is required, POST /password {\"password\":\"...\"} to submit it." >&2
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

SESSION_PATH=$DEFAULT_SESSION_PATH
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
  SESSION_PATH=$1
  shift
fi

case "$SESSION_PATH" in
  *.session) SESSION_BASE=${SESSION_PATH%".session"} ;;
  *) SESSION_BASE=$SESSION_PATH ;;
esac

# Detect --use-qr flag and strip it from the remaining args.
USE_QR=0
AUTH_SUBCMD="auth"
FILTERED=""
for _arg in "$@"; do
  if [ "$_arg" = "--use-qr" ]; then
    USE_QR=1
  else
    FILTERED="$FILTERED $(printf '%s' "$_arg")"
  fi
done
if [ "$USE_QR" -eq 1 ]; then
  AUTH_SUBCMD="auth-qr"
fi

VENV_DIR="$BACKEND_DIR/.venv"
CREATED_VENV=0
TEMP_DIR=""

cleanup() {
  if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
    rm -rf "$TEMP_DIR"
  fi
}

trap cleanup EXIT INT TERM

if [ ! -x "$VENV_DIR/bin/python" ]; then
  TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/openclaw-tg-auth.XXXXXX")
  VENV_DIR="$TEMP_DIR/.venv"
  CREATED_VENV=1
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV_DIR/bin/python" -m pip install -e "$BACKEND_DIR"
fi

echo "Using Python environment: $VENV_DIR"
if [ "$CREATED_VENV" -eq 1 ]; then
  echo "Temporary virtualenv was created for this run and will be removed afterwards."
fi

# shellcheck disable=SC2086
"$VENV_DIR/bin/python" -m openclaw_tg_bridge "$AUTH_SUBCMD" --session-path "$SESSION_PATH" $FILTERED

rm -f "$SESSION_PATH-journal" "$SESSION_PATH-wal" "$SESSION_PATH-shm"
rm -f "$SESSION_BASE.session-journal" "$SESSION_BASE.session-wal" "$SESSION_BASE.session-shm"

echo "Saved Telethon session: $SESSION_BASE.session"
