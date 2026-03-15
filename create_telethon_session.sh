#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
BACKEND_DIR="$ROOT_DIR/backend"
DEFAULT_SESSION_PATH="$ROOT_DIR/openclaw_user.session"

usage() {
  echo "Usage: sh create_telethon_session.sh [session_path] [extra auth args...]" >&2
  echo "Example: sh create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session --print-session-string" >&2
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

"$VENV_DIR/bin/python" -m openclaw_tg_bridge auth --session-path "$SESSION_PATH" "$@"

rm -f "$SESSION_PATH-journal" "$SESSION_PATH-wal" "$SESSION_PATH-shm"
rm -f "$SESSION_BASE.session-journal" "$SESSION_BASE.session-wal" "$SESSION_BASE.session-shm"

echo "Saved Telethon session: $SESSION_BASE.session"
