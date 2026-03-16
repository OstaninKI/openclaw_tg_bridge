---
name: telegram-user-bridge-setup
description: Install and configure the OpenClaw Telegram User Bridge from this repository into the local OpenClaw instance with minimal manual steps.
metadata:
  {"openclaw":{"emoji":"🧩"}}
---

# Telegram User Bridge Setup

Use this skill when the user asks to install, link, configure, repair, or self-manage this repository as an OpenClaw integration.

## Goal

Set up four things with minimal user interaction:

1. the backend Python service;
2. the local OpenClaw plugin from `./plugin`;
3. OpenClaw config entries in `~/.openclaw/openclaw.json`;
4. the Telegram user session file and backend policy file.

## Minimal user input you still need

The agent may do almost everything itself, but these inputs still require the user:

- `api_id` and `api_hash` from `my.telegram.org`, unless they are already available in env/config;
- the Telegram sender id for the primary owner DM;
- any additional trusted sender ids, if needed;
- for phone+code auth: the one-time Telegram login code and optional 2FA password;
- for QR auth: scanning the displayed QR code in the Telegram app, and optionally the 2FA password.

Do not pretend these steps are fully automatable. Telegram login is intentionally interactive.

## Preferred install strategy

Prefer **linking** the plugin from the local repo instead of copying it elsewhere. This keeps the OpenClaw instance pointed at the current workspace and makes future edits/reloads simpler.

## Safe workflow

1. Verify prerequisites:
   - `openclaw`
   - `python3`
   - `npm`
2. Install plugin dependencies in the repo:
   - `cd /absolute/path/to/repo/plugin`
   - `npm ci`
3. Link the plugin into OpenClaw from the local repo:
   - `openclaw plugins install --link /absolute/path/to/repo/plugin`
4. Install backend dependencies:
   - `cd /absolute/path/to/repo/backend`
   - `python3 -m venv .venv`
   - `.venv/bin/pip install -e .`
5. Create the Telegram session locally, not on the VPS. Two methods are available:

   **Method A — phone + code (default):**
   - `TELEGRAM_API_ID=... TELEGRAM_API_HASH=... TELEGRAM_PHONE=... sh /absolute/path/to/repo/create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session`
   - If env vars are unknown, run without them and let the script prompt interactively.
   - Only the Telegram login code and optional 2FA password require live interaction.

   **Method B — QR code (preferred when running inside OpenClaw web chat):**
   - Start the auth server: `TELEGRAM_API_ID=... TELEGRAM_API_HASH=... sh /absolute/path/to/repo/create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session --use-qr --listen 127.0.0.1:8767`
   - The script starts a temporary HTTP server on `127.0.0.1:8767`.
   - Fetch the QR: `GET http://127.0.0.1:8767/qr`
     - Response: `{state, qr_url, qr_png_b64, qr_ascii, expires_at}`
     - Decode `qr_png_b64` from base64 and show it to the user as an inline image in the current conversation.
     - Also show `qr_ascii` in a code block as a terminal fallback.
   - Tell the user: "Open Telegram → Settings → Devices → Link Desktop Device and scan this QR code."
   - Poll `GET http://127.0.0.1:8767/status` every 3 seconds until `state` is no longer `awaiting_scan`.
   - If `state == "awaiting_password"`:
     - Ask the user: "Telegram requires your 2FA password to complete sign-in. Please enter it."
     - `POST http://127.0.0.1:8767/password` with body `{"password": "<user input>"}`
     - Continue polling until `state == "done"` or `"error"`.
   - If the QR expires (25 s without a scan), the server auto-regenerates it. Fetch `/qr` again and show the new image.
   - When `state == "done"`: the session file is saved and the process exits. Continue with the next install step.
   - When `state == "error"`: report `error` from the status response and offer to retry or fall back to Method A.
6. Create backend policy from the example:
   - copy `/absolute/path/to/repo/deploy/policy.json.example` to `~/.openclaw/telethon/policy.json`
   - replace placeholder ids with the real owner/trusted sender ids
   - keep `owner_dm` privileged only if the user wants backend-host file tools and self-account/contact flows
7. Merge OpenClaw config from `/absolute/path/to/repo/deploy/openclaw.json.example` into `~/.openclaw/openclaw.json`
8. Preserve unrelated user config. Only change:
   - `session.dmScope`
   - `plugins.enabled`
   - `plugins.allow`
   - `plugins.entries.telegram-user-bridge`
   - `channels.telegram-user-bridge`
   - exact `bindings` for allowed DM senders
   - dedicated `agents.list` entries and tool allowlists for the bridge
9. Restart or reload OpenClaw after the initial install and structural config changes.
10. Verify:
   - plugin linked and enabled
   - backend health endpoint returns `connected: true`
   - owner DM tools exist
   - `sources_ro` tools exist
   - if inbound DM mode is configured, exact `bindings` match the allowed sender ids

## Editing rules

- Do not overwrite the whole `~/.openclaw/openclaw.json` file if unrelated config already exists.
- Do not rename, remove, or rebind the existing owner baseline unless the user explicitly asks.
- By default add/remove only `trusted*_dm` surfaces.
- Keep `strictPeerBindings: true` for inbound DM routing.
- Keep `session.dmScope = "per-channel-peer"` for multi-user inbox isolation.
- Keep extra trusted profiles non-privileged by default.
- Keep `sources_ro` read-only.

## What to tell the user during install

Keep the explanation short and concrete:

- what you are installing now;
- what input you still need from the user;
- whether Telegram login code / 2FA entry is about to happen;
- where the final files will live:
  - `~/.openclaw/openclaw.json`
  - `~/.openclaw/telethon/policy.json`
  - `~/.openclaw/telethon/openclaw_user.session`

## Re-authenticating a running bridge (session expired or revoked)

When `GET /health` returns `{"needs_reauth": true, "connected": false}`, the session has been revoked and the bridge is running in a limited mode. Re-authenticate without restarting the service:

1. `POST /auth/qr/start` (Authorization: Bearer `<api_token>`) — starts a QR login task in the background.
2. `GET /auth/qr` — fetch the QR code same as Method B above; show PNG and ASCII to the user.
3. Poll `GET /auth/qr/status` every 3 seconds.
4. If `state == "awaiting_password"`: ask the user for the 2FA password, then `POST /auth/qr/2fa {"password": "..."}`.
5. When `state == "done"`: the bridge is live again; confirm with `GET /health` returning `connected: true`.
6. If `state == "error"`: report the error and offer to retry (`POST /auth/qr/start` again).

All `/auth/qr/*` endpoints require the same `TELEGRAM_BRIDGE_API_TOKEN` Bearer token as regular bridge endpoints.

## After install

Once the plugin is linked and `plugins.entries.telegram-user-bridge.config` exists, the plugin-provided Telegram skill can take over normal day-to-day management. Use this setup skill mainly for first install, repair, or structural reconfiguration.
