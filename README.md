# OpenClaw Unofficial Telegram Bridge

Connect your **live Telegram user account** (not a bot) to [OpenClaw](https://openclaw.ai) via [Telethon](https://codeberg.org/Lonami/Telethon) (MTProto). The agent can send and read messages on your behalf when you explicitly ask, with user-like delays and optional chat allow/deny lists.

**Requirements:** OpenClaw on a VPS, Telegram API credentials from [my.telegram.org](https://my.telegram.org), session created **locally** (no login on VPS).

---

## Architecture

- **Backend** (Python): Long-running Telethon client; exposes a small HTTP API (send message, dialogs, messages, me, health). Runs on the same VPS as OpenClaw.
- **Plugin** (Node/TS): OpenClaw plugin that registers agent tools and calls the backend over HTTP (localhost). Plugin config can override reply delay and allow/deny lists, but enforcement always happens in the backend.
- **Skill**: Instructions for the agent (when to use Telegram tools, behave like a user).

---

## Quick start

### 1. Get Telegram API credentials

1. Go to [my.telegram.org](https://my.telegram.org) and sign in with the Telegram account you want to use.
2. Open **API Development tools** and create an application.
3. Note **api_id** (number) and **api_hash** (string). Keep them secret.

### 2. Create session locally (do not run on VPS)

On your **local machine** (where you can receive the Telegram login code):

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e .
python -m openclaw_tg_bridge auth --print-session-string
```

Enter api_id, api_hash, and a session path (e.g. `./openclaw_user`). Then enter your phone (international format, e.g. `+79001234567`) and the code Telegram sends you. If you have 2FA, enter your password when prompted.

This creates a `.session` file (and possibly `.session-journal`). If you pass `--print-session-string` or `--session-string-out /secure/path/session.txt`, the CLI also exports a Telethon `StringSession`.

Use one of these deployment options:

- **File session**: copy `.session` files to your VPS (e.g. into `~/.openclaw/telethon/`) using scp or another secure method.
- **StringSession**: set `TELEGRAM_SESSION_STRING` on the VPS and skip copying session files.

Do **not** log in from the VPS.

### 3. Run the bridge on the VPS

On the VPS:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

export TELEGRAM_API_ID=your_api_id
export TELEGRAM_API_HASH=your_api_hash
export TELEGRAM_SESSION_PATH=~/.openclaw/telethon/openclaw_user.session
# Or: export TELEGRAM_SESSION_STRING='...'
# Optional: TELEGRAM_REPLY_DELAY_SEC=2  (default 2)
# Optional: TELEGRAM_RPC_TIMEOUT_SEC=30
# Optional: TELEGRAM_FLOOD_WAIT_MAX_SLEEP_SEC=3
# Optional: TELEGRAM_BRIDGE_API_TOKEN=secret  (if you want to protect the HTTP API)
python -m openclaw_tg_bridge run
```

Or use the provided systemd unit (see [Deployment](#deployment)).

**Secure the session files:**

```bash
chmod 600 ~/.openclaw/telethon/*.session
# Ensure the process runs as a dedicated user that owns these files
```

### 4. Install and enable the OpenClaw plugin

```bash
openclaw plugins install /path/to/OpenClaw_tg_bridge/plugin
openclaw plugins enable telegram-user-bridge
```

Add the plugin tools to your agent allowlist in `~/.openclaw/openclaw.json` (or `agents.list[].tools.allow`):

```json5
{
  "agents": {
    "list": [
      {
        "id": "main",
        "tools": {
          "allow": ["telegram-user-bridge", "telegram_user_send_message", "telegram_user_get_dialogs", "telegram_user_get_messages"]
        }
      }
    ]
  }
}
```

Or allow the whole plugin: `"allow": ["telegram-user-bridge"]`.

Restart the OpenClaw gateway. The skill is loaded from the plugin; ensure the bridge backend is running so the tools work.

---

## Configuration

### Backend (environment)

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_API_ID` | Yes | From my.telegram.org |
| `TELEGRAM_API_HASH` | Yes | From my.telegram.org |
| `TELEGRAM_SESSION_PATH` | Yes* | Path to `.session` file (*or use `TELEGRAM_SESSION_STRING`) |
| `TELEGRAM_SESSION_STRING` | Yes* | Session string (alternative to file) |
| `TELEGRAM_BRIDGE_LISTEN` | No | `127.0.0.1:8765` (default) |
| `TELEGRAM_REPLY_DELAY_SEC` | No | Default delay before sending (default 2) |
| `TELEGRAM_REPLY_DELAY_MAX_SEC` | No | Default max delay for random range |
| `TELEGRAM_ALLOW_CHAT_IDS` | No | Default comma-separated allowlist (empty = all) |
| `TELEGRAM_DENY_CHAT_IDS` | No | Default comma-separated denylist |
| `TELEGRAM_BRIDGE_API_TOKEN` | No | Require `Authorization: Bearer <token>` |
| `TELEGRAM_RPC_TIMEOUT_SEC` | No | Telegram API timeout per request (default 30) |
| `TELEGRAM_FLOOD_WAIT_MAX_SLEEP_SEC` | No | Retry once only for short FloodWait values up to this threshold (default 3) |

### Plugin (OpenClaw config)

Under `plugins.entries.telegram-user-bridge.config`:

- `baseUrl`: Bridge URL (default `http://127.0.0.1:8765`)
- `apiToken`: Optional Bearer token (must match backend if set)
- `timeoutMs`: Request timeout (default 25000)
- `replyDelaySec`, `replyDelayMaxSec`: Optional backend-enforced delay overrides
- `allowFrom`, `denyFrom`: Optional backend-enforced chat policy overrides by username and/or numeric chat id

If plugin overrides are not set, the backend uses its environment defaults.

---

## Deployment

### systemd (example)

A sample unit file is in `deploy/openclaw-tg-bridge.service`. Copy and adjust paths:

Create `/etc/systemd/system/openclaw-tg-bridge.service`:

```ini
[Unit]
Description=OpenClaw Telegram Bridge
After=network.target

[Service]
Type=simple
User=openclaw
WorkingDirectory=/opt/openclaw-tg-bridge/backend
EnvironmentFile=/etc/openclaw-tg-bridge.env
ExecStart=/opt/openclaw-tg-bridge/backend/.venv/bin/python -m openclaw_tg_bridge run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Put secrets in `/etc/openclaw-tg-bridge.env` (e.g. `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_PATH`) with restricted permissions.

### Proxy (SOCKS5 or MTProxy)

If Telegram is blocked on the VPS:

- **SOCKS5**: `TELEGRAM_PROXY_TYPE=socks5`, `TELEGRAM_PROXY_HOST=...`, `TELEGRAM_PROXY_PORT=...`
- **MTProxy**: `TELEGRAM_PROXY_TYPE=mtproxy`, `TELEGRAM_PROXY_HOST=...`, `TELEGRAM_PROXY_PORT=...`, `TELEGRAM_PROXY_SECRET=...`

---

## Session invalidation

If you change your Telegram password or revoke the session, the bridge will fail with auth errors. Fix by creating a new session locally (run `python -m openclaw_tg_bridge auth` again), then copy the new `.session` file to the VPS and restart the bridge.

---

## Compliance with Telegram ToS

This project uses the Telegram API and must comply with [Telegram API Terms of Service](https://core.telegram.org/api/terms) and [Content Licensing and AI Scraping](https://telegram.org/tos/content-licensing). By using this bridge you agree to:

- Use your **own** api_id/api_hash from [my.telegram.org](https://my.telegram.org).
- Perform actions **only with the user’s explicit request** (no automation without consent).
- **Not** tamper with read status, typing indicators, or “ghost mode.”
- **Not** use Telegram data to train or improve AI/ML models.
- Disclose that the integration is **unofficial** and uses the Telegram API.
- Not use the official Telegram name/logo in a misleading way.

We do not log message content; we apply reply delays and optional allow/deny lists to reduce abuse risk. Flood waits are surfaced as explicit rate-limit errors instead of hidden retries. You are responsible for your use of the API.

---

## License

See [LICENSE](LICENSE) if present; otherwise use at your own responsibility.
