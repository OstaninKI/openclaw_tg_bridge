---
name: telegram-user
description: Send and read messages from your live Telegram user account (MTProto). Unofficial integration; use only when the user explicitly asks. Requires Unofficial Telegram User Bridge plugin and running bridge service.
metadata:
  {"openclaw":{"requires":{"config":["plugins.entries.telegram-user-bridge"]},"emoji":"✈️"}}
---

# Telegram User (live account)

This skill exposes tools to **send messages** and **read dialogs/messages** from your **personal Telegram account** (not a bot), via the OpenClaw Unofficial Telegram User Bridge (Telethon). This is an **unofficial** integration with the Telegram API; the app uses your own API credentials and session.

## When to use

- Use Telegram tools **only when the user explicitly asks** to send something to Telegram, read their Telegram chats, or check dialogs.
- **Do not** suggest or perform Telegram actions without a clear user request.
- **Behave like a user**: send one message at a time; the backend enforces a short delay before sending and can restrict allowed chats by id/username. Do not initiate mass sending or rapid successive messages.
- If a tool returns that the bridge is unavailable, tell the user once and do not retry repeatedly.

## What not to do

- Do not use Telegram data (message content, chats) for training, analysis, or any purpose other than fulfilling the current user request.
- Do not log or store message content.
- Do not automate read receipts, typing indicators, or "last seen" (ghost mode).

## Tools

- **telegram_user_send_message**: Send a text message. `peer` can be a username (e.g. `@durov`), numeric chat id, or `me` for Saved Messages.
- **telegram_user_get_dialogs**: List recent dialogs; use to find chat ids or show the user their chats.
- **telegram_user_get_messages**: Get recent messages from a chat (by peer username or id).

To find a chat id, use `telegram_user_get_dialogs` and read the ids from the list. If the backend reports that a chat is not allowed, do not try alternate identifiers for the same chat.
