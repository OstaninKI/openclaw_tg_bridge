---
name: telegram-user
description: Send and read messages from one live Telegram user account through isolated OpenClaw contexts. Unofficial integration; use only when the user explicitly asks. Requires Unofficial Telegram User Bridge plugin and running bridge service.
metadata:
  {"openclaw":{"requires":{"config":["plugins.entries.telegram-user-bridge"]},"emoji":"✈️"}}
---

# Telegram User (live account, isolated contexts)

This skill exposes tools to **send messages**, **read isolated DMs**, and **poll read-only sources** from one **personal Telegram account** (not a bot), via the OpenClaw Unofficial Telegram User Bridge (Telethon). This is an **unofficial** integration with the Telegram API; the app uses your own API credentials and session.

The same Telegram account may be exposed to multiple OpenClaw contexts, for example `owner_dm`, `wife_dm`, and `sources_ro`. Isolation is done by giving each agent only its own tool set, by enforcing read/write rules in the backend, and for inbound DMs by configuring OpenClaw `session.dmScope = "per-channel-peer"` plus exact `bindings` per Telegram sender id.

## When to use

- Use Telegram tools **only when the user explicitly asks** to send something to Telegram, read chats, or check dialogs.
- Use only the tools that belong to the current context. Do not switch to another profile's tools unless the user explicitly changes context.
- Treat `owner_dm` and `wife_dm` as separate private contexts. Never mix facts or summaries between them.
- **Behave like a user**: send one message at a time; the backend enforces a short delay before sending and can restrict allowed chats by id/username.
- Writing is **denied by default**. If a send tool says writing is not allowed, do not retry with another identifier for the same chat. Ask the user to grant write access first.
- If a tool returns that the bridge is unavailable, tell the user once and do not retry repeatedly.
- For `sources_ro`, do not try to send anything. That profile is read-only and exists for scheduled summaries/news digestion.

## What not to do

- Do not use Telegram data (message content, chats) for training, analysis, or any purpose other than fulfilling the current user request.
- Do not log or store message content unless the user explicitly requested a stored summary or report.
- Do not automate read receipts, typing indicators, or "last seen" (ghost mode).
- Do not read chats outside the current context's allowed scopes, even if another tool set would technically have access.

## Tools

- Interactive DM tools: `telegram_<context>_send_message`, `telegram_<context>_get_dialogs`, `telegram_<context>_get_messages`
- Source polling tools: `telegram_<context>_list_sources`, `telegram_<context>_sync_sources`, `telegram_<context>_get_messages`

Examples of context ids: `owner_dm`, `wife_dm`, `sources_ro`.

## Token economy and schedules

- For scheduled checks, always prefer `telegram_<context>_get_messages` with `min_id`, so only new messages are fetched since the last checkpoint.
- For `sources_ro`, call `telegram_<context>_list_sources` or `telegram_<context>_sync_sources` before first use or after the Telegram account joins new groups/channels.
- Store checkpoints per `{context, peer}`. If forum topics matter, keep checkpoints per `{context, peer, topic_id}`.
- Keep `limit` small and summarize deltas instead of rereading entire chats.
- Messages now include sender and topic metadata; use that instead of rereading the full chat when preparing summaries.
- Shared groups/channels should be processed through a dedicated read-only source context, separate from personal DM contexts.
