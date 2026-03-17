# OpenClaw Self-Install Guide

> This file is the primary entry point for OpenClaw agent self-install.
> For a detailed step-by-step skill with QR auth and config merge logic, see
> [skills/telegram-user-bridge-setup/SKILL.md](./skills/telegram-user-bridge-setup/SKILL.md).

---

## Required user inputs

These steps cannot be automated — Telegram login is intentionally interactive:

- `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org)
- owner Telegram `sender_id` (numeric)
- (optional) additional trusted sender ids
- Telegram login code + optional 2FA password when prompted

---

## Install sequence

1. Run `npm ci` in [`plugin/`](./plugin)
2. Link the plugin: `openclaw plugins install --link /absolute/path/to/repo/plugin`
3. Create/refresh backend venv: `cd backend && python3 -m venv .venv && .venv/bin/pip install -e .`
4. Create the Telegram session via [`create_telethon_session.sh`](./create_telethon_session.sh):
   ```bash
   TELEGRAM_API_ID=... TELEGRAM_API_HASH=... TELEGRAM_PHONE=... \
   sh ./create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session
   ```
   Or use `--use-qr --listen 127.0.0.1:8767` for QR-code auth (preferred in web chat).
5. Copy [`deploy/policy.json.example`](./deploy/policy.json.example) to `~/.openclaw/telethon/policy.json` and replace placeholder ids.
6. Merge [`deploy/openclaw.json.example`](./deploy/openclaw.json.example) into `~/.openclaw/openclaw.json` without overwriting unrelated config.
7. Restart or reload OpenClaw.
8. Verify: `curl http://127.0.0.1:8765/health` → `{"status":"ok","connected":true}`, tool registration, exact DM bindings.

---

## Config reference

### `~/.openclaw/telethon/policy.json`

```json
{
  "defaults": {
    "replyDelaySec": 2,
    "replyDelayMaxSec": 4,
    "read": { "allow": [], "deny": [] },
    "write": { "allow": [], "deny": [] }
  },
  "profiles": {
    "owner_dm": {
      "read":  { "allow": ["OWNER_ID"] },
      "write": { "allow": ["OWNER_ID", "me"] }
    },
    "trusted_dm": {
      "read":  { "allow": ["TRUSTED_ID"] },
      "write": { "allow": ["TRUSTED_ID"] }
    },
    "sources_ro": {
      "read":  { "allow": [], "deny": [] },
      "write": { "allow": [], "deny": [] },
      "sources": {
        "autoDiscover": true,
        "includeTypes": ["group", "supergroup", "forum", "channel"],
        "excludePeers": [],
        "excludeUsernames": []
      }
    },
    "dm_inbox": {
      "read":  { "allow": ["OWNER_ID", "TRUSTED_ID"] },
      "write": { "allow": ["OWNER_ID", "TRUSTED_ID"] }
    }
  }
}
```

### `~/.openclaw/openclaw.json` — plugin entry

```json5
{
  "session": {
    "dmScope": "per-channel-peer"
  },
  "plugins": {
    "enabled": true,
    "allow": ["telegram-user-bridge"],
    "entries": {
      "telegram-user-bridge": {
        "enabled": true,
        "config": {
          "baseUrl": "http://127.0.0.1:8765",
          "apiToken": "secret",
          "timeoutMs": 25000,
          "profiles": [
            {
              "id": "owner_dm",
              "label": "Owner DM",
              "mode": "interactive",
              "privilegedTools": true,
              "policyProfile": "owner_dm"
            },
            {
              "id": "trusted_dm",
              "label": "Trusted DM",
              "mode": "interactive",
              "policyProfile": "trusted_dm"
            },
            {
              "id": "sources_ro",
              "label": "Sources RO",
              "mode": "sources_ro",
              "policyProfile": "sources_ro"
            }
          ]
        }
      }
    }
  }
}
```

### `~/.openclaw/openclaw.json` — inbound DM channel

```json5
{
  "channels": {
    "telegram-user-bridge": {
      "accounts": {
        "default": {
          "enabled": true,
          "label": "Telegram User DM",
          "baseUrl": "http://127.0.0.1:8765",
          "apiToken": "secret",
          "policyProfile": "dm_inbox",
          "strictPeerBindings": true,
          "allowFrom": ["OWNER_ID", "TRUSTED_ID"],
          "writeTo":  ["OWNER_ID", "TRUSTED_ID"],
          "pollTimeoutMs": 25000,
          "pollIntervalMs": 1500
        }
      }
    }
  }
}
```

### `~/.openclaw/openclaw.json` — DM bindings

```json5
{
  "bindings": [
    {
      "agentId": "owner-agent",
      "match": {
        "channel": "telegram-user-bridge",
        "accountId": "default",
        "peer": { "kind": "direct", "id": "OWNER_ID" }
      }
    },
    {
      "agentId": "trusted-agent",
      "match": {
        "channel": "telegram-user-bridge",
        "accountId": "default",
        "peer": { "kind": "direct", "id": "TRUSTED_ID" }
      }
    }
  ]
}
```

### `~/.openclaw/openclaw.json` — agents tool allowlists

```json5
{
  "agents": {
    "list": [
      {
        "id": "owner-agent",
        "tools": {
          "alsoAllow": [
            "telegram_owner_dm_get_dialogs",
            "telegram_owner_dm_list_topics",
            "telegram_owner_dm_get_messages",
            "telegram_owner_dm_send_message",
            "telegram_owner_dm_join_chat_by_link",
            "telegram_owner_dm_list_dialog_folders",
            "telegram_owner_dm_upsert_dialog_folder",
            "telegram_owner_dm_delete_dialog_folder",
            "telegram_sources_ro_list_sources",
            "telegram_sources_ro_sync_sources",
            "telegram_sources_ro_list_topics",
            "telegram_sources_ro_get_messages"
          ]
        }
      },
      {
        "id": "trusted-agent",
        "tools": {
          "alsoAllow": [
            "telegram_trusted_dm_get_dialogs",
            "telegram_trusted_dm_list_topics",
            "telegram_trusted_dm_get_messages",
            "telegram_trusted_dm_send_message",
            "telegram_sources_ro_list_sources",
            "telegram_sources_ro_sync_sources",
            "telegram_sources_ro_list_topics",
            "telegram_sources_ro_get_messages"
          ]
        }
      }
    ]
  }
}
```

---

## Adding more trusted DM users

For each additional sender (e.g. `alice`, `bob`), add all of these:

**`policy.json` — new profiles:**
```json
{
  "profiles": {
    "trusted_alice_dm": {
      "read":  { "allow": ["ALICE_ID"] },
      "write": { "allow": ["ALICE_ID"] }
    }
  }
}
```

**Plugin profile entry:**
```json
{ "id": "trusted_alice_dm", "label": "Trusted Alice DM", "mode": "interactive", "policyProfile": "trusted_alice_dm" }
```

**Channel — extend `allowFrom` and `writeTo`:**
```json
{ "allowFrom": ["OWNER_ID", "TRUSTED_ID", "ALICE_ID"], "writeTo": ["OWNER_ID", "TRUSTED_ID", "ALICE_ID"] }
```

**One binding per sender:**
```json
{ "agentId": "trusted-alice-agent", "match": { "channel": "telegram-user-bridge", "accountId": "default", "peer": { "kind": "direct", "id": "ALICE_ID" } } }
```

**Agent with that profile's tools only:**
```json
{
  "id": "trusted-alice-agent",
  "tools": {
    "alsoAllow": [
      "telegram_trusted_alice_dm_get_dialogs",
      "telegram_trusted_alice_dm_list_topics",
      "telegram_trusted_alice_dm_get_messages",
      "telegram_trusted_alice_dm_send_message",
      "telegram_sources_ro_list_sources",
      "telegram_sources_ro_sync_sources",
      "telegram_sources_ro_list_topics",
      "telegram_sources_ro_get_messages"
    ]
  }
}
```

Do not set `privilegedTools: true` on extra trusted profiles unless explicitly requested.

---

## Editing rules

- Only modify the `telegram-user-bridge` plugin/channel subtrees, related `bindings`, and dedicated `agents.list` entries.
- Preserve all unrelated OpenClaw config.
- Keep `strictPeerBindings: true` and `session.dmScope = "per-channel-peer"`.
- Treat `owner_dm` as a protected baseline — extend only `trusted*_dm` by default.
- Any profile with `privilegedTools: true` must have `"me"` in backend `write.allow`.

---

## Re-authenticating a running bridge

If `GET /health` returns `{"connected": false, "needs_reauth": true}`:

1. `POST /auth/qr/start` (Bearer token required) — starts background QR login.
2. `GET /auth/qr` → `{state, qr_url, qr_png_b64, qr_ascii, expires_at}` — show PNG + ASCII to user.
3. Tell user: "Open Telegram → Settings → Devices → Link Desktop Device and scan this QR code."
4. Poll `GET /auth/qr/status` every 3 s.
   - `state == "awaiting_password"` → ask for 2FA, then `POST /auth/qr/2fa {"password":"..."}`, continue polling.
   - `state == "done"` → verify with `GET /health` returning `connected: true`.
   - `state == "error"` → report error, offer to retry (`POST /auth/qr/start`).
5. If QR expires before scanning, fetch `/auth/qr` again — backend auto-regenerates it.

All `/auth/qr/*` endpoints require the same `TELEGRAM_BRIDGE_API_TOKEN` Bearer token.
