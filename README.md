# OpenClaw Unofficial Telegram Bridge

Connect one **live Telegram user account** (not a bot) to [OpenClaw](https://openclaw.ai) via [Telethon](https://codeberg.org/Lonami/Telethon) (MTProto), while keeping **multiple isolated OpenClaw contexts** on top of the same Telegram session.

This repository is designed for a setup like this:

- one real Telegram account that represents OpenClaw;
- one isolated DM context for you: `owner_dm`;
- one isolated DM context for one additional trusted sender: `trusted_dm`;
- one scheduler-only, read-only context for groups/channels/forums: `sources_ro`;
- **writes denied by default** until you explicitly allow them;
- automatic discovery of new source chats for `sources_ro`, so joining a new channel/group does not require manual config edits.

> **OpenClaw agent installing this bridge?** → See [OPENCLAW.md](./OPENCLAW.md) for the compact self-install guide (config snippets, install sequence, editing rules). Load that file instead of this one.

## Table of Contents

- [Architecture](#architecture)
- [What is implemented](#what-is-implemented)
- [DM isolation model](#dm-isolation-model)
- [Quick start](#quick-start)
  - [1. Get Telegram API credentials](#1-get-telegram-api-credentials)
  - [2. Create the Telegram session locally](#2-create-the-telegram-session-locally)
  - [3. Run the backend on the VPS](#3-run-the-backend-on-the-vps)
  - [4. Create the backend policy file](#4-create-the-backend-policy-file)
  - [5. Configure the plugin](#5-configure-the-plugin)
  - [6. Configure the inbound DM channel](#6-configure-the-inbound-dm-channel)
  - [7. Bind tools to separate agents](#7-bind-tools-to-separate-agents)
- [OpenClaw self-install playbook](#openclaw-self-install-playbook)
- [Scheduling and token economy](#scheduling-and-token-economy)
- [Manual smoke checklist](#manual-smoke-checklist)
- [Re-authenticating a running bridge](#re-authenticating-a-running-bridge-session-revoked)
- [Backend configuration](#backend-configuration)
- [Plugin configuration](#plugin-configuration)
- [Safety model](#safety-model)
- [Compliance with Telegram ToS](#compliance-with-telegram-tos)

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

<details>
<summary>Full feature list</summary>

- Multiple isolated policy profiles on top of one Telegram session.
- Separate **read** and **write** scopes per profile.
- **Write denied by default** if no explicit write allowlist is configured.
- Conservative `FloodWait` handling:
  - short waits can be retried once;
  - longer waits return `429` with `Retry-After`;
  - no hidden long sleeps after the OpenClaw-side timeout.
- `get_messages(min_id=...)` for incremental polling and token savings.
- `get_messages(since_unix=...)` for strict recent time windows such as the last 24 hours.
- Topic-aware forum helpers:
  - `list_topics(peer)` for Telegram forum chats
  - `get_messages(peer, topic_id=...)` for one specific forum thread
- Richer interactive actions:
  - `send_file`
  - `send_voice`
  - `send_sticker`
  - `send_location`
  - `edit_message`
  - `delete_message`
  - `forward_message`
  - `search_messages`
  - `download_media`
  - `media_info`
  - `get_participants`
  - `get_admins` for groups, supergroups, and channels
  - `get_banned_users` for supergroups/channels
  - `list_contacts`
  - `search_contacts`
  - `add_contact`
  - `delete_contact`
  - `block_user`
  - `unblock_user`
  - `get_blocked_users`
  - `create_group`
  - `create_channel`
  - `invite_to_group`
  - `join_chat_by_link`
  - `get_invite_link`
  - `promote_admin` for groups, supergroups, and channels
  - `demote_admin` for groups, supergroups, and channels
  - `ban_user` for supergroups/channels
  - `unban_user` for supergroups/channels
  - `get_chat`
  - `get_history`
  - `search_public_chats`
  - `get_recent_actions` for supergroups/channels
  - `get_pinned_messages`
  - `send_reaction`
  - `remove_reaction`
  - `get_message_reactions`
  - `leave_chat`
- Message reads are returned in ascending order (`oldest -> newest`) for safer checkpoint updates.
- Richer message metadata for summaries:
  - `sender_id`
  - `sender_name`
  - `chat_id`
  - `chat_title`
  - `chat_username`
  - `topic_id` (forum thread root/top message id)
  - `reply_to_message_id`
  - `has_media`
  - `media_type`
  - `file_name`
  - `mime_type`
  - `media_path` (auto-downloaded inbound DM attachment path when available)
  - `media_paths` (array form for agent context compatibility)
  - `contact_phone`
  - `contact_first_name`
  - `contact_last_name`
  - `contact_user_id`
  - `contact_vcard` (truncated to 512 chars)
  - `latitude` / `longitude`
- Auto-discovery of sourceable dialogs for `sources_ro`:
  - channels
  - groups
  - supergroups
  - forum chats
- Read-only `sources_ro` toolset:
  - `list_sources`
  - `sync_sources`
  - `list_topics`
  - `get_messages`
- Event-driven inbound DM channel:
  - backend long-lived Telethon listener
  - OpenClaw channel polling endpoint `/dm/inbox/poll`
  - acknowledged cursors in `dm_inbox_state.json`
  - direct replies back to the same Telegram sender

</details>

## DM isolation model

For inbound DMs, the critical OpenClaw setting is:

```json5
{
  session: {
    dmScope: "per-channel-peer"
  }
}
```

This is the recommended secure DM mode in OpenClaw for multi-user inboxes. It keeps separate session keys per channel + sender, so your DM history and each additional trusted sender's DM history do not mix even though all of them write to the same Telegram account.

To make routing deterministic as well, add exact OpenClaw `bindings` by Telegram `sender_id`. The plugin now refuses fallback inbound DM routing by default. In strict mode it validates `allowFrom`, `writeTo`, and exact peer `bindings` at channel startup and refuses to start if they diverge.

## Quick start

### 1. Get Telegram API credentials

1. Go to [my.telegram.org](https://my.telegram.org) and sign in with the Telegram account you want to use.
2. Open **API Development tools** and create an application.
3. Save `api_id` and `api_hash`.

### 2. Create the Telegram session locally

Run locally, not on the VPS. Two auth methods are available.

#### Method A — phone + code (default)

Fast path from the repository root:

```bash
sh ./create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session
```

The script reuses `backend/.venv` if it already exists. Otherwise it creates a temporary virtualenv, installs the local backend package, runs the existing interactive `auth` CLI, removes the temporary virtualenv on exit, and cleans leftover SQLite sidecar files such as `-journal` / `-wal` / `-shm`. Only the final `.session` file remains.

You can still pass extra auth flags through the script, for example:

```bash
sh ./create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session --print-session-string
```

To minimize prompts, you can prefill the auth step via env vars or CLI flags. The auth CLI now accepts `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_PHONE` (or `--api-id`, `--api-hash`, `--phone`). That lets OpenClaw preconfigure everything except the live Telegram code / optional 2FA password:

```bash
TELEGRAM_API_ID=12345 \
TELEGRAM_API_HASH=your_api_hash \
TELEGRAM_PHONE=+1234567890 \
sh ./create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session
```

Manual path:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m openclaw_tg_bridge auth --print-session-string
```

#### Method B — QR code (preferred when running inside OpenClaw web chat)

Start a one-shot local HTTP server that serves the QR code over `localhost`:

```bash
TELEGRAM_API_ID=12345 TELEGRAM_API_HASH=your_api_hash \
sh ./create_telethon_session.sh ~/.openclaw/telethon/openclaw_user.session \
  --use-qr --listen 127.0.0.1:8767
```

Once the server is running:

1. `GET http://127.0.0.1:8767/qr` → returns `{state, qr_url, qr_png_b64, qr_ascii, expires_at}`.
   - Decode `qr_png_b64` from base64 and display as an inline image in the chat.
   - Also show `qr_ascii` in a code block as a terminal fallback.
2. Tell the user: "Open Telegram → Settings → Devices → Link Desktop Device and scan this QR code."
3. Poll `GET http://127.0.0.1:8767/status` every 3 seconds.
   - If `state == "awaiting_password"`: ask the user for their 2FA password, then `POST http://127.0.0.1:8767/password` with body `{"password": "<user input>"}`. Continue polling.
   - If `state == "done"`: the session file is saved and the process exits automatically. Proceed with the next install step.
   - If `state == "error"`: report `error` from the status response and offer to retry or fall back to Method A.
4. QR codes expire after ~25 s. If the current QR expires the server auto-regenerates it. Fetch `/qr` again and show the new image.

No Telegram session is needed to start the HTTP server — it is a standalone process that runs only during auth.

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
export TELEGRAM_LOCK_PATH=~/.openclaw/telethon/bridge.lock
export TELEGRAM_BRIDGE_API_TOKEN=secret
python -m openclaw_tg_bridge run
```

For a persistent deploy, use the example systemd unit at [deploy/openclaw-tg-bridge.service](./deploy/openclaw-tg-bridge.service). It already includes restart policy and a small set of safe hardening flags.

Recommended production rule:

- keep `TELEGRAM_BRIDGE_LISTEN=127.0.0.1:8765`;
- expose the backend only through local OpenClaw or a reverse proxy you control;
- keep `TELEGRAM_BRIDGE_API_TOKEN` enabled;
- store the Telethon session, policy JSON, inventory JSON, and inbox cursor JSON under a dedicated service-owned directory.

Minimal systemd install flow:

```bash
sudo cp deploy/openclaw-tg-bridge.service /etc/systemd/system/openclaw-tg-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-tg-bridge
sudo systemctl status openclaw-tg-bridge
```

### 4. Create the backend policy file

Example `policy.json`. The easiest path is to copy [deploy/policy.json.example](./deploy/policy.json.example) to `~/.openclaw/telethon/policy.json` and replace the placeholder ids:

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
        "allow": ["123456789", "me"]
      }
    },
    "trusted_dm": {
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
- Add `"me"` to `write.allow` only for profiles that should be allowed to use backend-host file tools or self-account/contact mutation tools.
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

This will register tools like:

- `telegram_owner_dm_get_dialogs`
- `telegram_owner_dm_list_topics`
- `telegram_owner_dm_get_messages`
- `telegram_owner_dm_send_message`
- `telegram_owner_dm_join_chat_by_link`
- `telegram_owner_dm_list_dialog_folders`
- `telegram_owner_dm_upsert_dialog_folder`
- `telegram_owner_dm_delete_dialog_folder`
- `telegram_trusted_dm_get_dialogs`
- `telegram_trusted_dm_list_topics`
- `telegram_trusted_dm_get_messages`
- `telegram_trusted_dm_send_message`
- `telegram_sources_ro_list_sources`
- `telegram_sources_ro_sync_sources`
- `telegram_sources_ro_list_topics`
- `telegram_sources_ro_get_messages`

All interactive profiles expose the baseline chat/message/admin surface. That includes tools such as `send_message`, `send_location`, `edit_message`, `delete_message`, `forward_message`, `get_message`, `get_history`, `search_messages`, `get_participants`, `get_admins`, `promote_admin`, `demote_admin`, `get_chat`, `search_public_chats`, `get_pinned_messages`, `send_reaction`, `remove_reaction`, `get_message_reactions`, `resolve_username`, `get_user_status`, `get_media_info`, and topic-aware reading.

Profiles with `"privilegedTools": true` keep that same baseline surface and additionally expose backend-host file tools plus self-account/contact mutation tools. The extra tools are:

- backend-host file tools:
  - `telegram_owner_dm_send_file`
  - `telegram_owner_dm_send_voice`
  - `telegram_owner_dm_send_sticker`
  - `telegram_owner_dm_download_media`
- contacts and self-account flows:
  - `telegram_owner_dm_list_contacts`
  - `telegram_owner_dm_search_contacts`
  - `telegram_owner_dm_add_contact`
  - `telegram_owner_dm_delete_contact`
  - `telegram_owner_dm_block_user`
  - `telegram_owner_dm_unblock_user`
  - `telegram_owner_dm_get_blocked_users`
- account/group lifecycle flows:
  - `telegram_owner_dm_create_group`
  - `telegram_owner_dm_create_channel`
  - `telegram_owner_dm_invite_to_group`
  - `telegram_owner_dm_join_chat_by_link`
  - `telegram_owner_dm_list_dialog_folders`
  - `telegram_owner_dm_upsert_dialog_folder`
  - `telegram_owner_dm_delete_dialog_folder`
  - `telegram_owner_dm_get_invite_link`
  - `telegram_owner_dm_leave_chat`

`join_chat_by_link` and dialog-folder tools are intentionally owner-only in plugin code and are registered only for owner-prefixed profile ids (for example `owner_dm`).

Some moderation tools remain baseline, but only make sense on supergroups/channels:

- `telegram_owner_dm_get_banned_users`
- `telegram_owner_dm_ban_user`
- `telegram_owner_dm_unban_user`
- `telegram_owner_dm_get_recent_actions`

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
      "agentId": "trusted-agent",
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

Add bridge tools to each OpenClaw agent:

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

If global `tools.profile` is set (for example `coding`), use `tools.alsoAllow` for Telegram plugin tools.
Using plugin-only `tools.allow` under a profile can be stripped by policy pipeline and leave only core tools.
If agents use explicit `tools.allow`, include `telegram_owner_dm_join_chat_by_link` for the owner profile, otherwise the agent cannot self-join channels from `t.me` links.
Dialog-folder management tools are owner-only and are registered only for profile ids that start with `owner` (for example `owner_dm`).
Compatibility note: if only one owner profile exists with id `owner` or `owner_dm`, the plugin also registers the alternate owner prefix as aliases (`telegram_owner_*` and `telegram_owner_dm_*`) to reduce migration mismatches between profile id and `tools.allow`.

This is the main separation mechanism. One agent should never get the other agent's DM tool set.

If you want more than one additional trusted DM sender, do not reuse one shared `trusted_dm`. Create separate profiles and agents such as:

- `trusted_alice_dm`
- `trusted_bob_dm`
- `trusted_parent_dm`

For each additional sender, add:

- a dedicated backend policy profile;
- a dedicated plugin profile;
- one exact DM `binding` by Telegram `sender_id`;
- one OpenClaw agent with only that profile's tools;
- the sender id in the shared channel account `allowFrom` and `writeTo`.

The channel implementation already supports this model. You do not need extra bridge instances or extra Telegram sessions.

<details>
<summary>Copy-paste pattern: adding more trusted DM users</summary>

If you want OpenClaw to add more trusted DM users itself, use one dedicated `trusted_<alias>_dm` profile per person and leave the existing owner baseline untouched unless you explicitly ask to change it.

Example for adding two more trusted senders:

`policy.json`

```json
{
  "profiles": {
    "trusted_alice_dm": {
      "read": { "allow": ["555111222"] },
      "write": { "allow": ["555111222"] }
    },
    "trusted_bob_dm": {
      "read": { "allow": ["555333444"] },
      "write": { "allow": ["555333444"] }
    }
  }
}
```

`plugins.entries.telegram-user-bridge.config.profiles`

```json
[
  {
    "id": "trusted_alice_dm",
    "label": "Trusted Alice DM",
    "mode": "interactive",
    "policyProfile": "trusted_alice_dm"
  },
  {
    "id": "trusted_bob_dm",
    "label": "Trusted Bob DM",
    "mode": "interactive",
    "policyProfile": "trusted_bob_dm"
  }
]
```

Do not set `privilegedTools: true` on these extra trusted DM profiles unless you explicitly want them to access backend-host files and self-account/contact mutation flows.

`channels.telegram-user-bridge.accounts.default`

```json
{
  "allowFrom": ["123456789", "987654321", "555111222", "555333444"],
  "writeTo": ["123456789", "987654321", "555111222", "555333444"]
}
```

`bindings`

```json
[
  {
    "agentId": "trusted-alice-agent",
    "match": {
      "channel": "telegram-user-bridge",
      "accountId": "default",
      "peer": { "kind": "direct", "id": "555111222" }
    }
  },
  {
    "agentId": "trusted-bob-agent",
    "match": {
      "channel": "telegram-user-bridge",
      "accountId": "default",
      "peer": { "kind": "direct", "id": "555333444" }
    }
  }
]
```

`agents.list`

```json
[
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
  },
  {
    "id": "trusted-bob-agent",
    "tools": {
      "alsoAllow": [
        "telegram_trusted_bob_dm_get_dialogs",
        "telegram_trusted_bob_dm_list_topics",
        "telegram_trusted_bob_dm_get_messages",
        "telegram_trusted_bob_dm_send_message",
        "telegram_sources_ro_list_sources",
        "telegram_sources_ro_sync_sources",
        "telegram_sources_ro_list_topics",
        "telegram_sources_ro_get_messages"
      ]
    }
  }
]
```

Operational rule for OpenClaw self-management:

- it may add, update, or remove only `trusted*_dm` entries by default;
- it must not rename, remove, or rebind the existing owner DM baseline unless you explicitly ask for that change.

</details>

## OpenClaw self-install playbook

See **[OPENCLAW.md](./OPENCLAW.md)** for the compact machine-readable guide: install sequence, all config snippets, editing rules, and re-auth flow in one file.

For a detailed step-by-step skill with QR auth and config merge logic, see [skills/telegram-user-bridge-setup/SKILL.md](./skills/telegram-user-bridge-setup/SKILL.md).

<details>
<summary>Sequence summary &amp; editing boundaries</summary>

Minimal user interaction should be only:

- provide or confirm `api_id` / `api_hash`;
- provide or confirm the owner Telegram `sender_id`;
- optionally provide additional trusted sender ids;
- enter the Telegram login code and optional 2FA password when prompted.

Recommended self-install sequence for OpenClaw:

1. run `npm ci` in [plugin](./plugin);
2. link the plugin from the local repo with `openclaw plugins install --link /absolute/path/to/repo/plugin`;
3. create or refresh [backend/.venv](./backend/.venv) and run `pip install -e .` in [backend](./backend);
4. create the Telegram session via [create_telethon_session.sh](./create_telethon_session.sh), preferably with `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_PHONE` prefilled;
5. copy [deploy/policy.json.example](./deploy/policy.json.example) to `~/.openclaw/telethon/policy.json` and replace placeholder ids;
6. merge [deploy/openclaw.json.example](./deploy/openclaw.json.example) into `~/.openclaw/openclaw.json` without overwriting unrelated user config;
7. restart or reload OpenClaw after the initial install and structural config changes;
8. verify `curl http://127.0.0.1:8765/health`, tool registration, and exact DM `bindings`.

Editing boundaries for OpenClaw:

- it should only modify the `telegram-user-bridge` plugin/channel subtrees, exact related `bindings`, and the dedicated `agents.list` entries for this bridge;
- it should preserve unrelated OpenClaw config;
- it should keep `strictPeerBindings: true` and `session.dmScope = "per-channel-peer"` for the multi-user DM model;
- it should treat `owner_dm` as protected baseline and extend only `trusted*_dm` by default.

</details>

## Scheduling and token economy

For groups/channels/forums and periodic processing:

- schedule jobs inside OpenClaw via cron/heartbeat/automations;
- keep checkpoints in OpenClaw, not in the bridge;
- keep a checkpoint per `{profile, peer}`;
- if forum topics matter, keep checkpoints per `{profile, peer, topic_id}`;
- use `min_id` for "since the last run";
- use `since_unix` for exact time windows such as "last 24 hours";
- if both are set, the bridge applies both filters;
- call `telegram_sources_ro_list_sources` or `telegram_sources_ro_sync_sources` before first use or after the Telegram account joins new sources;
- for forum chats, call `telegram_sources_ro_list_topics(peer=...)` and use the returned `topic_id` as the thread fetch id;
- call `telegram_sources_ro_get_messages` with a small `limit` and `min_id`;
- summarize only deltas, not whole chats;
- use returned sender/topic metadata instead of rereading the same history.

Example polling pattern:

1. call `telegram_sources_ro_sync_sources(limit=500)` after the account joins new channels/groups;
2. read stored `last_message_id` for `{sources_ro, -1003333333333}`;
3. call `telegram_sources_ro_get_messages(peer=-1003333333333, min_id=last_message_id, limit=20)`;
4. summarize only returned messages;
5. update the checkpoint with the new max message id.

Example forum-topic polling pattern:

1. call `telegram_sources_ro_list_topics(peer=-1003333333333, limit=20)`;
2. pick the needed `topic_id` from the result;
3. read stored `last_message_id` for `{sources_ro, -1003333333333, topic_id}`;
4. call `telegram_sources_ro_get_messages(peer=-1003333333333, topic_id=900, min_id=last_message_id, limit=20)`;
5. summarize only returned messages and update the OpenClaw checkpoint.

Example strict "last 24 hours" pattern:

1. compute `since_unix = now_unix - 86400`;
2. call `telegram_sources_ro_get_messages(peer=-1003333333333, since_unix=since_unix, limit=20)`;
3. summarize only returned messages;
4. do not overwrite checkpoint-based state just because a time-window query was executed.

Example forum-wide digest pattern:

1. call `telegram_sources_ro_list_topics(peer=-1003333333333, limit=50)`;
2. for each returned topic, load the OpenClaw checkpoint for `{sources_ro, -1003333333333, topic_id}`;
3. call `telegram_sources_ro_get_messages(peer=-1003333333333, topic_id=<topic_id>, min_id=<checkpoint>, limit=20)` separately per topic;
4. skip topics with no new messages;
5. produce the digest grouped by topic title and mention message authors from returned metadata;
6. update only the checkpoints for topics that produced new messages.

Operational rule: checkpoints belong to OpenClaw, not to the bridge. The bridge only returns deltas and topic metadata.

<details>
<summary>Manual smoke checklist &amp; re-auth flow</summary>

## Manual smoke checklist

Automated tests cover backend/plugin logic, but live Telegram permissions and RPC behavior still need a manual smoke pass on a real account.

Recommended minimal manual checks after deployment:

1. send a plain text message;
2. for every interactive profile: edit/delete one of your own messages and read recent messages;
3. send a location pin from any interactive profile that is allowed to write, and from the privileged owner profile also send a file, voice note, and sticker;
4. for the privileged owner profile only: read one message, download its media, and inspect media metadata;
5. for the privileged owner profile only: list contacts, add one test contact, then delete it;
6. for the privileged owner profile only: block and unblock one test user;
7. list participants/admins on one group or channel;
8. send/remove a reaction and fetch the reaction list;
9. if you rely on admin workflows, test promote/demote on a disposable basic group and on a disposable supergroup; test ban/unban and recent admin actions on a disposable supergroup or channel;
10. if you rely on privileged onboarding/account flows, test invite link generation, join-by-link, and leave-chat on a disposable group;
11. verify that a second backend process refuses to start because `TELEGRAM_LOCK_PATH` is already held.

Useful live checks:

- `curl http://127.0.0.1:8765/health` should return `{"status":"ok","connected":true}` in the healthy state;
- if Telegram connectivity is temporarily down, the endpoint returns HTTP `503` with `{"connected":false}`;
- if the Telegram session was revoked (logged out from another device), the endpoint returns HTTP `503` with `{"connected":false,"needs_reauth":true}`. In this case use the re-auth flow described below — do not restart the service;
- `systemctl status openclaw-tg-bridge` should show automatic restarts after a crash if you use the bundled unit file.

## Re-authenticating a running bridge (session revoked)

If `GET /health` returns `{"connected":false,"needs_reauth":true}`, the Telegram session was revoked (e.g. the account was logged out from another device). The bridge continues running in a limited mode — all tool calls return `503` with `needs_reauth: true` in the body.

Re-authenticate without restarting the service:

1. `POST /auth/qr/start` (Authorization: Bearer `<TELEGRAM_BRIDGE_API_TOKEN>`) — starts a background QR login task.
2. `GET /auth/qr` — returns `{state, qr_url, qr_png_b64, qr_ascii, expires_at}`. Decode and show the PNG inline; show `qr_ascii` as a code-block fallback.
3. Tell the user: "Open Telegram → Settings → Devices → Link Desktop Device and scan this QR code."
4. Poll `GET /auth/qr/status` every 3 seconds.
   - `state == "awaiting_password"` → ask for 2FA, then `POST /auth/qr/2fa {"password":"..."}`, continue polling.
   - `state == "done"` → bridge is live again. Verify with `GET /health` returning `connected: true`.
   - `state == "error"` → report `error` from the status body and offer to retry (`POST /auth/qr/start` again).
5. If the QR expires before scanning, the backend auto-regenerates it. Fetch `/auth/qr` again.

All `/auth/qr/*` endpoints require the same `TELEGRAM_BRIDGE_API_TOKEN` Bearer token as regular bridge endpoints.

</details>

## Backend configuration

<details>
<summary>Environment variables</summary>

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
| `TELEGRAM_DM_AUTO_DOWNLOAD_MEDIA` | Auto-download inbound DM attachments during `/dm/inbox/poll` (default `true`) |
| `TELEGRAM_DM_MEDIA_PATH` | Directory for auto-downloaded inbound DM attachments |
| `TELEGRAM_SOURCES_REFRESH_SEC` | Minimum delay between automatic inventory refreshes |
| `TELEGRAM_SOURCES_DIALOG_LIMIT` | How many dialogs to scan when refreshing inventory |
| `TELEGRAM_BRIDGE_API_TOKEN` | Optional bearer token for plugin/backend auth |
| `TELEGRAM_RPC_TIMEOUT_SEC` | Telegram RPC timeout per request |
| `TELEGRAM_FLOOD_WAIT_MAX_SLEEP_SEC` | Retry once only for short flood waits up to this threshold |

</details>

## Plugin configuration

Root plugin config still supports a legacy single-context mode, but the recommended mode is `profiles`.

Each profile can define:

- `id`
- `label`
- `mode`
  - `interactive`
  - `sources_ro`
- `privilegedTools`
- `policyProfile`
- `replyDelaySec`
- `replyDelayMaxSec`
- `allowFrom`
- `denyFrom`
- `writeTo`
- `denyWriteTo`

These are backend-enforced overrides on top of the JSON policy file. If omitted, the backend uses the policy file and environment defaults.

`privilegedTools: true` exposes backend-host file tools and self-account/contact mutation tools for that profile. Profiles without it still get the normal chat/message/admin reading surface, but not file/download/contact/create/invite/leave flows. Join-by-link and dialog-folder flows are additionally owner-only in plugin code. Any profile that uses privileged tools should also include `"me"` in backend `write.allow`.

For event-driven DMs, also configure `channels.telegram-user-bridge.accounts.<id>` with:

- `baseUrl`
- `apiToken`
- `policyProfile`
- `allowFrom`
- `writeTo`
- `pollTimeoutMs`
- `pollIntervalMs`
- `strictPeerBindings`
- `markReadOnInbound` (optional, default `true`)
- `typingWhileReplying` (optional, default `true`)

Use numeric Telegram user ids there when possible. They are more stable than usernames for inbound routing and cursor tracking.
With `strictPeerBindings: true`, the plugin accepts inbound DMs only when `cfg.bindings` contains an exact peer binding for that sender.
`markReadOnInbound: false` disables Telegram read receipts for accepted inbound DMs on this channel account. When enabled, read status is sent only for DMs that are allowed for interaction, not merely readable.
`typingWhileReplying: false` disables Telegram typing status while OpenClaw is generating a DM reply on this channel account.
In strict mode, startup validation now also checks bound agents with explicit `tools.allow`: at least one `telegram_<context>_*` tool must be present, otherwise channel startup fails fast with a clear config error instead of silently falling back to core tools.
When global or agent-level `tools.profile` is active, strict mode also fails startup for plugin-only `tools.allow` and asks to move Telegram tools into `tools.alsoAllow`.
Strict mode also fails startup when a bound agent resolves to `runtime.type=acp`, because ACP runtime setups may expose only ACP developer tools to the model and hide plugin-generated tools.
Inbound DM media auto-download is enabled by default in backend (`TELEGRAM_DM_AUTO_DOWNLOAD_MEDIA=true`) and stores files under `TELEGRAM_DM_MEDIA_PATH`. Download happens only for DMs that pass this account's interaction allowlist at poll time.
The channel also retries `/dm/inbox/ack` with a short request-level backoff, because ack is idempotent and safe to retry; the full inbound reply flow is not retried wholesale.
Channel reload now watches both `channels.telegram-user-bridge` and `plugins.entries.telegram-user-bridge`, so channel account changes and plugin profile changes reload through the standard OpenClaw mechanism.
When `/dm/inbox/poll` keeps failing, the DM channel also backs off progressively instead of retrying every `pollIntervalMs`.

## Safety model

This setup reduces risk, but does **not** guarantee that Telegram will never rate-limit or restrict the account.

What the implementation does to reduce risk:

- no mass sending;
- one message per request;
- serialized sends with human-like delays;
- explicit rate-limit handling;
- DM read receipts / typing indicators enabled by default for the interactive DM channel, but individually disableable in channel config;
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
