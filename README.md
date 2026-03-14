# OpenClaw Unofficial Telegram Bridge

Connect one **live Telegram user account** (not a bot) to [OpenClaw](https://openclaw.ai) via [Telethon](https://codeberg.org/Lonami/Telethon) (MTProto), while keeping **multiple isolated OpenClaw contexts** on top of the same Telegram session.

This repository is designed for scenarios like:

- one real Telegram account;
- one OpenClaw context for you;
- one separate OpenClaw context for your wife;
- one optional `shared` context for common groups/channels;
- **reading allowed only where explicitly scoped**;
- **writing denied by default** until you explicitly allow it.

## Architecture

- **Backend** (Python): one long-running Telethon client using one Telegram session; enforces all read/write policy decisions.
- **Plugin** (Node/TS): registers OpenClaw tools and binds each tool set to a fixed backend policy profile.
- **Skill**: instructs the agent to stay inside its own context and to use incremental polling for scheduled jobs.
- **Policy file**: JSON file that OpenClaw itself can edit to grant/revoke access without changing code.

The isolation boundary is **not** the Telegram account. It is the combination of:

1. separate OpenClaw agents/tool allowlists;
2. separate plugin tool sets per context;
3. backend-enforced read/write scopes per context profile.

## Core behavior

- One Telegram session can back multiple OpenClaw contexts.
- Contexts are separated by **policy profiles** such as `owner`, `wife`, `shared`.
- Each profile has separate **read** and **write** scopes.
- **Read and write are independent**.
- **Write is denied by default** if no explicit write allowlist is configured.
- Current ACL granularity is **peer/chat/channel-level**. Separate write ACL for Telegram forum topics/threads is not implemented yet.
- `get_messages` supports `min_id`, so scheduled jobs can fetch only deltas and save tokens.
- `FloodWait` is handled conservatively:
  - short waits can be retried once;
  - longer waits return `429` with `Retry-After`;
  - no hidden long sleeps that would later send a message after OpenClaw already timed out.

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
    "owner": {
      "read": {
        "allow": ["me", "@owner_private", "-1001111111111"]
      },
      "write": {
        "allow": ["me"]
      }
    },
    "wife": {
      "read": {
        "allow": ["@wife_private", "-1002222222222"]
      },
      "write": {
        "allow": []
      }
    },
    "shared": {
      "read": {
        "allow": ["-1003333333333", "@shared_channel"]
      },
      "write": {
        "allow": []
      }
    }
  }
}
```

Notes:

- `read.allow: []` means that profile cannot read anything unless another layer overrides it.
- `write.allow: []` means **write denied**.
- To allow writing later, OpenClaw can edit this file and add a chat id or username to `write.allow`.
- The backend reloads the JSON file automatically on the next request.

### 5. Configure the plugin

The plugin can register separate tool sets per context profile:

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
              "id": "owner",
              "label": "Owner",
              "policyProfile": "owner"
            },
            {
              "id": "wife",
              "label": "Wife",
              "policyProfile": "wife"
            },
            {
              "id": "shared",
              "label": "Shared",
              "policyProfile": "shared"
            }
          ]
        }
      }
    }
  }
}
```

This will register tools like:

- `telegram_owner_get_dialogs`
- `telegram_owner_get_messages`
- `telegram_owner_send_message`
- `telegram_wife_get_dialogs`
- `telegram_shared_get_messages`

### 6. Bind tools to separate agents

Give each OpenClaw agent only its own tools:

```json5
{
  "agents": {
    "list": [
      {
        "id": "owner-agent",
        "tools": {
          "allow": [
            "telegram_owner_get_dialogs",
            "telegram_owner_get_messages",
            "telegram_owner_send_message",
            "telegram_shared_get_dialogs",
            "telegram_shared_get_messages",
            "telegram_shared_send_message"
          ]
        }
      },
      {
        "id": "wife-agent",
        "tools": {
          "allow": [
            "telegram_wife_get_dialogs",
            "telegram_wife_get_messages",
            "telegram_wife_send_message",
            "telegram_shared_get_dialogs",
            "telegram_shared_get_messages",
            "telegram_shared_send_message"
          ]
        }
      }
    ]
  }
}
```

This is the main separation mechanism. One agent should never get the other agent's tool set.

## Scheduling and token economy

For groups/channels and periodic processing:

- schedule jobs inside OpenClaw via cron/heartbeat/automations;
- keep a checkpoint per `{profile, peer}`;
- call `telegram_<profile>_get_messages` with a small `limit` and `min_id`;
- summarize only deltas, not whole chats;
- use a dedicated `shared` profile for common groups/channels whenever possible.

Example polling pattern:

1. read stored `last_message_id` for `{shared, -1003333333333}`;
2. call `telegram_shared_get_messages(peer=-1003333333333, min_id=last_message_id, limit=20)`;
3. summarize only returned messages;
4. store the new max message id.

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
| `TELEGRAM_BRIDGE_API_TOKEN` | Optional bearer token for plugin/backend auth |
| `TELEGRAM_RPC_TIMEOUT_SEC` | Telegram RPC timeout per request |
| `TELEGRAM_FLOOD_WAIT_MAX_SLEEP_SEC` | Retry once only for short flood waits up to this threshold |

## Plugin configuration

Root plugin config supports a legacy single-context mode, but the recommended mode is `profiles`.

Each profile can define:

- `id`
- `label`
- `policyProfile`
- `replyDelaySec`
- `replyDelayMaxSec`
- `allowFrom`
- `denyFrom`
- `writeTo`
- `denyWriteTo`

These are backend-enforced overrides on top of the JSON policy file. If omitted, the backend uses the policy file and environment defaults.

## Safety model

This setup reduces risk, but does **not** guarantee that Telegram will never rate-limit or restrict the account.

What the implementation does to reduce risk:

- no mass sending;
- one message per request;
- serialized sends with human-like delays;
- explicit rate-limit handling;
- no hidden read-status or typing hacks;
- default deny on writes;
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
