---
name: telegram-user
description: Send and read messages from one live Telegram user account through isolated OpenClaw contexts. Unofficial integration; use only when the user explicitly asks. Requires Unofficial Telegram User Bridge plugin and running bridge service.
metadata:
  {"openclaw":{"requires":{"config":["plugins.entries.telegram-user-bridge.config"]},"emoji":"✈️"}}
---

# Telegram User (live account, isolated contexts)

This skill exposes tools to **send messages**, **read isolated DMs**, and **poll read-only sources** from one **personal Telegram account** (not a bot), via the OpenClaw Unofficial Telegram User Bridge (Telethon). This is an **unofficial** integration with the Telegram API; the app uses your own API credentials and session.

The same Telegram account may be exposed to multiple OpenClaw contexts, for example `owner_dm`, `trusted_dm`, and `sources_ro`. If you need more than one additional DM user, create separate profiles such as `trusted_alice_dm` and `trusted_bob_dm`. Isolation is done by giving each agent only its own tool set, by enforcing read/write rules in the backend, and for inbound DMs by configuring OpenClaw `session.dmScope = "per-channel-peer"` plus exact `bindings` per Telegram sender id.

## When to use

- Use Telegram tools **only when the user explicitly asks** to send something to Telegram, read chats, or check dialogs.
- Use only the tools that belong to the current context. Do not switch to another profile's tools unless the user explicitly changes context.
- Treat `owner_dm` and every `trusted*_dm` profile as separate private contexts. Never mix facts or summaries between them.
- **Behave like a user**: send one message at a time; the backend enforces a short delay before sending and can restrict allowed chats by id/username.
- Writing is **denied by default**. If a send tool says writing is not allowed, do not retry with another identifier for the same chat. Ask the user to grant write access first.
- Some interactive profiles may expose only the baseline chat surface. Backend-host file tools and self-account/contact mutation tools exist only on profiles explicitly configured with `privilegedTools: true` and backend `write.allow` containing `"me"`.
- If a tool returns that the bridge is unavailable, tell the user once and do not retry repeatedly.
- For `sources_ro`, do not try to send anything. That profile is read-only and exists for scheduled summaries/news digestion.

## What not to do

- Do not use Telegram data (message content, chats) for training, analysis, or any purpose other than fulfilling the current user request.
- Do not log or store message content unless the user explicitly requested a stored summary or report.
- Do not automate read receipts, typing indicators, or "last seen" (ghost mode).
- Do not read chats outside the current context's allowed scopes, even if another tool set would technically have access.
- Do not rename, remove, or rebind the pre-existing primary owner DM profile/agent/binding unless the user explicitly asks for that change.

## Tools

- Interactive DM tools: `telegram_<context>_send_message`, `telegram_<context>_get_dialogs`, `telegram_<context>_list_topics`, `telegram_<context>_get_messages`
- Additional interactive tools:
  - baseline chat/message/admin tools: `telegram_<context>_send_location`, `telegram_<context>_edit_message`, `telegram_<context>_delete_message`, `telegram_<context>_forward_message`, `telegram_<context>_get_media_info`, `telegram_<context>_resolve_username`, `telegram_<context>_get_user_status`, `telegram_<context>_get_participants`, `telegram_<context>_get_admins`, `telegram_<context>_promote_admin`, `telegram_<context>_demote_admin`, `telegram_<context>_get_chat`, `telegram_<context>_get_message`, `telegram_<context>_get_history`, `telegram_<context>_search_messages`, `telegram_<context>_search_public_chats`, `telegram_<context>_get_pinned_messages`, `telegram_<context>_send_reaction`, `telegram_<context>_remove_reaction`, `telegram_<context>_get_message_reactions`
  - privileged backend-host/self-account tools, only on profiles with `privilegedTools: true`: `telegram_<context>_send_file`, `telegram_<context>_send_voice`, `telegram_<context>_send_sticker`, `telegram_<context>_download_media`, `telegram_<context>_list_contacts`, `telegram_<context>_search_contacts`, `telegram_<context>_add_contact`, `telegram_<context>_delete_contact`, `telegram_<context>_block_user`, `telegram_<context>_unblock_user`, `telegram_<context>_get_blocked_users`, `telegram_<context>_create_group`, `telegram_<context>_create_channel`, `telegram_<context>_invite_to_group`, `telegram_<context>_join_chat_by_link`, `telegram_<context>_get_invite_link`, `telegram_<context>_leave_chat`
  - supergroup/channel moderation only: `telegram_<context>_get_banned_users`, `telegram_<context>_ban_user`, `telegram_<context>_unban_user`, `telegram_<context>_get_recent_actions`
- Source polling tools: `telegram_<context>_list_sources`, `telegram_<context>_sync_sources`, `telegram_<context>_list_topics`, `telegram_<context>_get_messages`

Examples of context ids: `owner_dm`, `trusted_dm`, `trusted_alice_dm`, `sources_ro`.

## Managing additional trusted DM users

OpenClaw may manage additional trusted DM users by editing configuration, but only inside the `trusted*_dm` surface. The existing owner baseline must stay untouched unless the user explicitly asks to change it.

Safe default rule:

- keep the current owner DM profile, owner binding, and owner agent unchanged;
- add, update, or remove only `trusted*_dm` profiles by default;
- use numeric Telegram `sender_id` values for DM routing;
- give each trusted sender a dedicated profile, binding, and agent;
- keep extra trusted DM profiles non-privileged by default; do not add `privilegedTools: true` unless the user explicitly wants backend-host file access and self-account/contact mutation on that profile;
- do not merge multiple trusted people into one shared DM context if privacy matters.

When adding a trusted DM user, update all of these places consistently:

1. backend `policy.json`: add a new profile such as `trusted_alice_dm`;
2. plugin profiles: add `trusted_alice_dm` with `mode: "interactive"`, `policyProfile: "trusted_alice_dm"`, and no `privilegedTools`;
3. DM channel account: append the sender id to `allowFrom` and `writeTo`;
4. OpenClaw `bindings`: add one exact direct binding from that sender id to a dedicated agent;
5. OpenClaw agents: add one dedicated agent that can use only `telegram_trusted_alice_dm_*` tools plus shared `sources_ro` tools if needed.

When removing a trusted DM user:

1. remove that sender's binding;
2. remove that sender's dedicated agent or its DM tools;
3. remove the sender id from `allowFrom` and `writeTo` if it is no longer used;
4. remove the matching `trusted*_dm` policy and plugin profile.

## Token economy and schedules

- Use `min_id` for "since the last checkpoint" polling.
- Use `since_unix` for strict time windows such as "last 24 hours".
- If both `min_id` and `since_unix` are set, the bridge applies both filters.
- For `sources_ro`, call `telegram_<context>_list_sources` or `telegram_<context>_sync_sources` before first use or after the Telegram account joins new groups/channels.
- Checkpoints are owned by OpenClaw, not by the bridge. Store them in OpenClaw per `{context, peer}` and, for forum topics, per `{context, peer, topic_id}`.
- Keep `limit` small and summarize deltas instead of rereading entire chats.
- Messages now include sender and topic metadata; use that instead of rereading the full chat when preparing summaries.
- For forum chats, call `telegram_<context>_list_topics` first. Its `topic_id` is the thread root message id that must be passed back into `telegram_<context>_get_messages`.
- Shared groups/channels should be processed through a dedicated read-only source context, separate from personal DM contexts.

## Ready-made scheduler patterns

### 1. Delta summary since the last run

Use this when the user wants "what is new since the last run" or when a scheduler keeps its own checkpoint.

1. Read the OpenClaw checkpoint for `{context, peer}`.
2. Call `telegram_<context>_get_messages(peer=..., min_id=checkpoint, limit=...)`.
3. If no messages are returned, report that there is no new content and keep the checkpoint unchanged.
4. Summarize only the returned delta.
5. Update the OpenClaw checkpoint to the maximum returned message id.

### 2. Exact time-window summary, for example last 24 hours

Use this when the user asks for a strict recent window instead of "since the last successful run".

1. Compute `since_unix`, for example `now_unix - 86400` for the last 24 hours.
2. Call `telegram_<context>_get_messages(peer=..., since_unix=..., limit=...)`.
3. Summarize only the returned messages.
4. Do not overwrite checkpoint-based state just because a time-window query was executed.

Important: `since_unix` is time-based, while `min_id` is checkpoint-based. They solve different problems.

### 3. Delta summary for one forum topic

Use this when the user wants one specific thread inside a forum chat.

1. Call `telegram_<context>_list_topics(peer=...)`.
2. Find the needed topic and take its `topic_id`.
3. Read the OpenClaw checkpoint for `{context, peer, topic_id}`.
4. Call `telegram_<context>_get_messages(peer=..., topic_id=..., min_id=checkpoint, limit=...)`.
5. Summarize only the returned messages for that one topic.
6. Update the OpenClaw checkpoint to the maximum returned message id for that topic.

### 4. Forum-wide scheduled digest

Use this when the user wants "what happened in forum chat X" without naming one topic.

1. Call `telegram_<context>_list_topics(peer=...)`.
2. For each topic, load its OpenClaw checkpoint `{context, peer, topic_id}`.
3. Call `telegram_<context>_get_messages` separately per topic with a small `limit`.
4. Skip topics with no new messages.
5. Produce a digest grouped by topic title, and mention the message authors from returned metadata.
6. Update only the checkpoints for topics that actually produced new messages.

### 5. What the bridge should not own

- Do not ask the bridge to store checkpoints.
- Do not reread full forum histories on every run.
- Do not mix topic checkpoints with whole-chat checkpoints.
- Do not use DM contexts for scheduled source digestion if `sources_ro` is available.
- Do not use destructive tools such as delete/leave unless the user explicitly asked for that exact action.
- Treat backend-host file tools, contact mutation, block/unblock, join-by-link, leave-chat, and destructive media/message tools as high-risk actions. Use them only on explicit user instruction, not as autonomous convenience steps.
- Telegram itself distinguishes basic groups from supergroups/channels. `get_admins`, `promote_admin`, and `demote_admin` work across both, but `get_banned_users`, `ban_user`, `unban_user`, and `get_recent_actions` should be treated as supergroup/channel-only.
