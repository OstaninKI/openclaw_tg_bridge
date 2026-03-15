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
- the one-time Telegram login code and optional 2FA password during session creation.

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
5. Create the Telegram session locally, not on the VPS:
   - prefer `TELEGRAM_API_ID=... TELEGRAM_API_HASH=... TELEGRAM_PHONE=... sh /absolute/path/to/repo/create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session`
   - if the env vars are unknown, run the same script without them and let it prompt
   - only the Telegram code / 2FA should require live user interaction
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

## After install

Once the plugin is linked and `plugins.entries.telegram-user-bridge.config` exists, the plugin-provided Telegram skill can take over normal day-to-day management. Use this setup skill mainly for first install, repair, or structural reconfiguration.
