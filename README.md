# OpenClaw Unofficial Telegram Bridge

Connect one **live Telegram user account** (not a bot) to [OpenClaw](https://openclaw.ai) via [Telethon](https://codeberg.org/Lonami/Telethon) (MTProto), while keeping **multiple isolated OpenClaw contexts** on top of the same Telegram session.

This repository is designed for a setup like this:

- one real Telegram account that represents OpenClaw;
- one isolated DM context for you: `owner_dm`;
- one isolated DM context for your wife: `wife_dm`;
- one scheduler-only, read-only context for groups/channels/forums: `sources_ro`;
- **writes denied by default** until you explicitly allow them;
- automatic discovery of new source chats for `sources_ro`, so joining a new channel/group does not require manual config edits.

## Architecture

- **Backend** (Python): one long-running Telethon client using one Telegram session; enforces all read/write policy decisions and keeps a source inventory.
- **Plugin** (Node/TS): registers OpenClaw tools and the inbound DM channel, binding each surface to a fixed backend policy profile.
- **Skill**: instructs the agent to stay inside its own context and to use incremental polling for scheduled jobs.
- **Policy file**: JSON file that OpenClaw itself can edit to grant/revoke read or write access without code changes.
- **Sources inventory**: JSON-backed cache of discovered groups/channels/forums; used by `sources_ro`.

The isolation boundary is **not** the Telegram account. It is the combination of:

1. separate OpenClaw agents/tool allowlists;
2. separate plugin tool sets per context;
3. backend-enforced read/write scopes per context profile.

## What is implemented

- Multiple isolated policy profiles on top of one Telegram session.
- Separate **read** and **write** scopes per profile.
- **Write denied by default** if no explicit write allowlist is configured.
- Conservative `FloodWait` handling:
  - short waits can be retried once;
  - longer waits return `429` with `Retry-After`;
  - no hidden long sleeps after the OpenClaw-side timeout.
- `get_messages(min_id=...)` for incremental polling and token savings.
- Richer message metadata for summaries:
  - `sender_id`
  - `sender_name`
  - `chat_id`
  - `chat_title`
  - `chat_username`
  - `topic_id`
  - `reply_to_message_id`
- Auto-discovery of sourceable dialogs for `sources_ro`:
  - channels
  - groups
  - supergroups
  - forum chats
- Read-only `sources_ro` toolset:
  - `list_sources`
  - `sync_sources`
  - `get_messages`
- Event-driven inbound DM channel:
  - backend long-lived Telethon listener
  - OpenClaw channel polling endpoint `/dm/inbox/poll`
  - acknowledged cursors in `dm_inbox_state.json`
  - direct replies back to the same Telegram sender

## DM isolation model

For inbound DMs, the critical OpenClaw setting is:

```json5
{
  session: {
    dmScope: "per-channel-peer"
  }
}
```

This is the recommended secure DM mode in OpenClaw for multi-user inboxes. It keeps separate session keys per channel + sender, so your DM history and your wife's DM history do not mix even though both write to the same Telegram account.

To make routing deterministic as well, add exact OpenClaw `bindings` by Telegram `sender_id`. The plugin now refuses fallback inbound DM routing by default. In strict mode it validates `allowFrom`, `writeTo`, and exact peer `bindings` at channel startup and refuses to start if they diverge.

## Quick start

### 1. Get Telegram API credentials

1. Go to [my.telegram.org](https://my.telegram.org) and sign in with the Telegram account you want to use.
2. Open **API Development tools** and create an application.
3. Save `api_id` and `api_hash`.

### 2. Create the Telegram session locally

Run locally, not on the VPS:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m openclaw_tg_bridge auth --print-session-string
```

You can deploy either:

- with `.session` files via `TELEGRAM_SESSION_PATH`;
- or with `TELEGRAM_SESSION_STRING`.

### 3. Run the backend on the VPS

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

export TELEGRAM_API_ID=your_api_id
export TELEGRAM_API_HASH=your_api_hash
export TELEGRAM_SESSION_PATH=~/.openclaw/telethon/openclaw_user.session
# or: export TELEGRAM_SESSION_STRING='...'

export TELEGRAM_POLICY_PATH=~/.openclaw/telethon/policy.json
export TELEGRAM_SOURCES_INVENTORY_PATH=~/.openclaw/telethon/sources_inventory.json
export TELEGRAM_BRIDGE_API_TOKEN=secret
python -m openclaw_tg_bridge run
```

### 4. Create the backend policy file

Example `policy.json`:

```json
{
  "defaults": {
    "replyDelaySec": 2,
    "replyDelayMaxSec": 4,
    "read": {
      "allow": [],
      "deny": []
    },
    "write": {
      "allow": [],
      "deny": []
    }
  },
  "profiles": {
    "owner_dm": {
      "read": {
        "allow": ["123456789"]
      },
      "write": {
        "allow": ["123456789"]
      }
    },
    "wife_dm": {
      "read": {
        "allow": ["987654321"]
      },
      "write": {
        "allow": ["987654321"]
      }
    },
    "sources_ro": {
      "read": {
        "allow": [],
        "deny": []
      },
      "write": {
        "allow": [],
        "deny": []
      },
      "sources": {
        "autoDiscover": true,
        "includeTypes": ["group", "supergroup", "forum", "channel"],
        "excludePeers": [],
        "excludeUsernames": []
      }
    }
  }
}
```

Notes:

- `read.allow: []` means the profile cannot read anything unless another layer supplies peers.
- `sources.autoDiscover: true` tells the backend to expand `sources_ro` with auto-discovered groups/channels/forums from `sources_inventory.json`.
- `write.allow: []` means **write denied**.
- To allow writing later, OpenClaw can edit this file and add a chat id or username to `write.allow`.
- The backend reloads the JSON file automatically on the next request.

### 5. Configure the plugin

Recommended plugin config:

```json5
{
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
              "policyProfile": "owner_dm"
            },
            {
              "id": "wife_dm",
              "label": "Wife DM",
              "mode": "interactive",
              "policyProfile": "wife_dm"
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

This will register tools like:

- `telegram_owner_dm_get_dialogs`
- `telegram_owner_dm_get_messages`
- `telegram_owner_dm_send_message`
- `telegram_wife_dm_get_dialogs`
- `telegram_wife_dm_get_messages`
- `telegram_wife_dm_send_message`
- `telegram_sources_ro_list_sources`
- `telegram_sources_ro_sync_sources`
- `telegram_sources_ro_get_messages`

### 6. Configure the inbound DM channel

Add a channel account for event-driven DMs from the same Telegram account:

```json5
{
  "session": {
    "dmScope": "per-channel-peer"
  },
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
          "allowFrom": ["123456789", "987654321"],
          "writeTo": ["123456789", "987654321"],
          "pollTimeoutMs": 25000,
          "pollIntervalMs": 1500
        }
      }
    }
  }
}
```

Add exact DM bindings for each allowed sender:

```json5
{
  "bindings": [
    {
      "agentId": "owner-agent",
      "match": {
        "channel": "telegram-user-bridge",
        "accountId": "default",
        "peer": { "kind": "direct", "id": "123456789" }
      }
    },
    {
      "agentId": "wife-agent",
      "match": {
        "channel": "telegram-user-bridge",
        "accountId": "default",
        "peer": { "kind": "direct", "id": "987654321" }
      }
    }
  ]
}
```

Recommended `dm_inbox` policy:

```json
{
  "profiles": {
    "dm_inbox": {
      "read": {
        "allow": ["123456789", "987654321"]
      },
      "write": {
        "allow": ["123456789", "987654321"]
      }
    }
  }
}
```

This channel is for **direct messages only**. It does not read groups/channels; those stay in `sources_ro`.
For privacy-sensitive multi-user inboxes, use numeric Telegram user ids and keep `strictPeerBindings: true`.
In this mode, startup validation expects:

- explicit numeric `allowFrom`
- explicit numeric `writeTo`
- one exact `binding` per allowed sender id
- no stale bindings for senders missing from `allowFrom`

### 7. Bind tools to separate agents

Give each OpenClaw agent only its own tools:

```json5
{
  "agents": {
    "list": [
      {
        "id": "owner-agent",
        "tools": {
          "allow": [
            "telegram_owner_dm_get_dialogs",
            "telegram_owner_dm_get_messages",
            "telegram_owner_dm_send_message",
            "telegram_sources_ro_list_sources",
            "telegram_sources_ro_sync_sources",
            "telegram_sources_ro_get_messages"
          ]
        }
      },
      {
        "id": "wife-agent",
        "tools": {
          "allow": [
            "telegram_wife_dm_get_dialogs",
            "telegram_wife_dm_get_messages",
            "telegram_wife_dm_send_message",
            "telegram_sources_ro_list_sources",
            "telegram_sources_ro_sync_sources",
            "telegram_sources_ro_get_messages"
          ]
        }
      }
    ]
  }
}
```

This is the main separation mechanism. One agent should never get the other agent's DM tool set.

## Scheduling and token economy

For groups/channels/forums and periodic processing:

- schedule jobs inside OpenClaw via cron/heartbeat/automations;
- keep a checkpoint per `{profile, peer}`;
- if forum topics matter, keep checkpoints per `{profile, peer, topic_id}`;
- call `telegram_sources_ro_list_sources` or `telegram_sources_ro_sync_sources` before first use or after the Telegram account joins new sources;
- call `telegram_sources_ro_get_messages` with a small `limit` and `min_id`;
- summarize only deltas, not whole chats;
- use returned sender/topic metadata instead of rereading the same history.

Example polling pattern:

1. call `telegram_sources_ro_sync_sources(limit=500)` after the account joins new channels/groups;
2. read stored `last_message_id` for `{sources_ro, -1003333333333}`;
3. call `telegram_sources_ro_get_messages(peer=-1003333333333, min_id=last_message_id, limit=20)`;
4. summarize only returned messages;
5. update the checkpoint with the new max message id.

## Backend configuration

Environment variables:

| Variable | Description |
|---|---|
| `TELEGRAM_API_ID` | Telegram app id from my.telegram.org |
| `TELEGRAM_API_HASH` | Telegram app hash |
| `TELEGRAM_SESSION_PATH` | Path to `.session` file |
| `TELEGRAM_SESSION_STRING` | Alternative to session file |
| `TELEGRAM_BRIDGE_LISTEN` | Default `127.0.0.1:8765` |
| `TELEGRAM_REPLY_DELAY_SEC` | Default reply delay |
| `TELEGRAM_REPLY_DELAY_MAX_SEC` | Default max randomized delay |
| `TELEGRAM_ALLOW_CHAT_IDS` | Global default read allowlist |
| `TELEGRAM_DENY_CHAT_IDS` | Global default read denylist |
| `TELEGRAM_WRITE_ALLOW_CHAT_IDS` | Global default write allowlist; empty means deny all |
| `TELEGRAM_WRITE_DENY_CHAT_IDS` | Global default write denylist |
| `TELEGRAM_POLICY_PATH` | Path to JSON policy file |
| `TELEGRAM_POLICY_DEFAULT_PROFILE` | Optional default backend policy profile |
| `TELEGRAM_SOURCES_INVENTORY_PATH` | Path to JSON inventory of discovered source dialogs |
| `TELEGRAM_INBOX_STATE_PATH` | Path to JSON file with acknowledged inbound DM cursors |
| `TELEGRAM_SOURCES_REFRESH_SEC` | Minimum delay between automatic inventory refreshes |
| `TELEGRAM_SOURCES_DIALOG_LIMIT` | How many dialogs to scan when refreshing inventory |
| `TELEGRAM_BRIDGE_API_TOKEN` | Optional bearer token for plugin/backend auth |
| `TELEGRAM_RPC_TIMEOUT_SEC` | Telegram RPC timeout per request |
| `TELEGRAM_FLOOD_WAIT_MAX_SLEEP_SEC` | Retry once only for short flood waits up to this threshold |

## Plugin configuration

Root plugin config still supports a legacy single-context mode, but the recommended mode is `profiles`.

Each profile can define:

- `id`
- `label`
- `mode`
  - `interactive`
  - `sources_ro`
- `policyProfile`
- `replyDelaySec`
- `replyDelayMaxSec`
- `allowFrom`
- `denyFrom`
- `writeTo`
- `denyWriteTo`

These are backend-enforced overrides on top of the JSON policy file. If omitted, the backend uses the policy file and environment defaults.

For event-driven DMs, also configure `channels.telegram-user-bridge.accounts.<id>` with:

- `baseUrl`
- `apiToken`
- `policyProfile`
- `allowFrom`
- `writeTo`
- `pollTimeoutMs`
- `pollIntervalMs`
- `strictPeerBindings`

Use numeric Telegram user ids there when possible. They are more stable than usernames for inbound routing and cursor tracking.
With `strictPeerBindings: true`, the plugin accepts inbound DMs only when `cfg.bindings` contains an exact peer binding for that sender.
The channel also retries `/dm/inbox/ack` with a short request-level backoff, because ack is idempotent and safe to retry; the full inbound reply flow is not retried wholesale.

## Safety model

This setup reduces risk, but does **not** guarantee that Telegram will never rate-limit or restrict the account.

What the implementation does to reduce risk:

- no mass sending;
- one message per request;
- serialized sends with human-like delays;
- explicit rate-limit handling;
- no hidden read-status or typing hacks;
- default deny on writes;
- read-only source ingestion by scheduler;
- small, incremental reads for scheduled jobs.

## Compliance with Telegram ToS

This project uses the Telegram API and must comply with:

- [Telegram API Terms of Service](https://core.telegram.org/api/terms)
- [Content Licensing and AI Scraping](https://telegram.org/tos/content-licensing)

Key operational rules:

- use your own `api_id` and `api_hash`;
- perform actions only on explicit user request or explicit scheduled workflows you configured;
- do not use Telegram data for model training;
- disclose that this is an **unofficial** Telegram integration;
- do not bypass read receipts / typing / last-seen semantics.
