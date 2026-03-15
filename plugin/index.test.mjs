import assert from "node:assert/strict";
import test from "node:test";

import register, { __test } from "./index.ts";

function createApi(config = {}) {
  const tools = [];
  const channels = [];
  return {
    config,
    logger: { warn() {} },
    registerTool(tool) {
      tools.push(tool);
    },
    registerChannel({ plugin }) {
      channels.push(plugin);
    },
    tools,
    channels,
  };
}

function getTool(api, name) {
  return api.tools.find((tool) => tool.name === name);
}

test("plugin registers isolated profile toolsets and forwards profile headers", async () => {
  const api = createApi({
    plugins: {
      entries: {
        "telegram-user-bridge": {
          config: {
            baseUrl: "http://127.0.0.1:8765/",
            apiToken: "secret",
            profiles: [
              {
                id: "owner",
                label: "Owner",
                privilegedTools: true,
                policyProfile: "owner",
                allowFrom: ["@durov", "-1001"],
                writeTo: ["me"],
              },
              {
                id: "shared",
                label: "Shared",
                policyProfile: "shared",
                allowFrom: ["-1001"],
                writeTo: [],
              },
            ],
          },
        },
      },
    },
  });
  register(api);

  assert.ok(getTool(api, "telegram_owner_send_message"));
  assert.ok(getTool(api, "telegram_owner_send_file"));
  assert.ok(getTool(api, "telegram_owner_send_location"));
  assert.ok(getTool(api, "telegram_owner_edit_message"));
  assert.ok(getTool(api, "telegram_owner_delete_message"));
  assert.ok(getTool(api, "telegram_owner_forward_message"));
  assert.ok(getTool(api, "telegram_owner_get_message"));
  assert.ok(getTool(api, "telegram_owner_search_messages"));
  assert.ok(getTool(api, "telegram_owner_download_media"));
  assert.ok(getTool(api, "telegram_owner_get_participants"));
  assert.ok(getTool(api, "telegram_owner_get_admins"));
  assert.ok(getTool(api, "telegram_shared_get_messages"));
  assert.equal(getTool(api, "telegram_shared_send_file"), undefined);
  assert.equal(getTool(api, "telegram_shared_download_media"), undefined);
  assert.equal(getTool(api, "telegram_shared_add_contact"), undefined);
  assert.equal(getTool(api, "telegram_user_send_message"), undefined);

  let capturedUrl = "";
  let capturedInit = undefined;
  globalThis.fetch = async (url, init) => {
    capturedUrl = url;
    capturedInit = init;
    return new Response(JSON.stringify({ ok: true, message_id: 123 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  const sendTool = getTool(api, "telegram_owner_send_message");
  const result = await sendTool.execute("1", { peer: "@durov", text: "hello" });

  assert.equal(capturedUrl, "http://127.0.0.1:8765/send_message");
  assert.equal(capturedInit.method, "POST");
  assert.equal(capturedInit.headers.Authorization, "Bearer secret");
  assert.equal(capturedInit.headers["X-OpenClaw-Policy-Profile"], "owner");
  assert.equal(capturedInit.headers["X-OpenClaw-Allow-From"], "@durov,-1001");
  assert.equal(capturedInit.headers["X-OpenClaw-Write-To"], "me");
  assert.match(result.content[0].text, /Message sent/);
});

test("plugin sends file via backend endpoint", async () => {
  const api = createApi();
  register(api);

  let capturedUrl = "";
  let capturedInit = undefined;
  globalThis.fetch = async (url, init) => {
    capturedUrl = url;
    capturedInit = init;
    return new Response(JSON.stringify({ ok: true, message_id: 456 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  const tool = getTool(api, "telegram_user_send_file");
  const result = await tool.execute("1", { peer: "me", file_path: "/tmp/test.txt", caption: "doc" });

  assert.equal(capturedUrl, "http://127.0.0.1:8765/send_file");
  assert.equal(capturedInit.method, "POST");
  assert.match(capturedInit.body, /\/tmp\/test\.txt/);
  assert.match(result.content[0].text, /File sent/);
});

test("explicit interactive profile skips privileged tools by default", async () => {
  const api = createApi({
    plugins: {
      entries: {
        "telegram-user-bridge": {
          config: {
            profiles: [{ id: "trusted", label: "Trusted", mode: "interactive", policyProfile: "trusted_dm" }],
          },
        },
      },
    },
  });
  register(api);

  assert.ok(getTool(api, "telegram_trusted_send_message"));
  assert.equal(getTool(api, "telegram_trusted_send_file"), undefined);
  assert.equal(getTool(api, "telegram_trusted_download_media"), undefined);
  assert.equal(getTool(api, "telegram_trusted_list_contacts"), undefined);
  assert.equal(getTool(api, "telegram_trusted_create_group"), undefined);
});

test("plugin downloads media via backend endpoint", async () => {
  const api = createApi();
  register(api);

  let capturedUrl = "";
  globalThis.fetch = async (url) => {
    capturedUrl = url;
    return new Response(JSON.stringify({ path: "/tmp/photo.jpg" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  const tool = getTool(api, "telegram_user_download_media");
  const result = await tool.execute("1", { peer: -1001, message_id: 77, output_path: "/tmp/photo.jpg" });

  assert.equal(
    capturedUrl,
    "http://127.0.0.1:8765/download_media?peer=-1001&message_id=77&output_path=%2Ftmp%2Fphoto.jpg"
  );
  assert.match(result.content[0].text, /Media downloaded/);
});

test("plugin sends reaction and block user via backend endpoints", async () => {
  const api = createApi();
  register(api);

  const seen = [];
  globalThis.fetch = async (url, init) => {
    seen.push({ url, init });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  await getTool(api, "telegram_user_send_reaction").execute("1", {
    peer: -1001,
    message_id: 77,
    emoji: "🔥",
    big: true,
  });
  await getTool(api, "telegram_user_block_user").execute("2", { peer: "@durov" });

  assert.equal(seen[0].url, "http://127.0.0.1:8765/send_reaction");
  assert.match(seen[0].init.body, /"emoji":"🔥"/);
  assert.equal(seen[1].url, "http://127.0.0.1:8765/block_user");
  assert.match(seen[1].init.body, /@durov/);
});

test("plugin reads contacts and recent actions via backend endpoints", async () => {
  const api = createApi();
  register(api);

  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(url);
    const isContacts = String(url).includes("/contacts");
    return new Response(
      JSON.stringify(
        isContacts
          ? { contacts: [{ id: 1, title: "Alice", username: "alice", phone: "123" }] }
          : { events: [{ id: 9, user_id: 7, action: "MessageEdit", date: "2026-03-14T10:00:00+00:00" }] }
      ),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      }
    );
  };

  const contacts = await getTool(api, "telegram_user_list_contacts").execute("1", {});
  const actions = await getTool(api, "telegram_user_get_recent_actions").execute("2", { peer: -1001, limit: 10 });

  assert.equal(urls[0], "http://127.0.0.1:8765/contacts");
  assert.equal(urls[1], "http://127.0.0.1:8765/recent_actions?peer=-1001&limit=10");
  assert.match(contacts.content[0].text, /Alice/);
  assert.match(actions.content[0].text, /MessageEdit/);
});

test("plugin passes min_id, since_unix and topic_id for polling", async () => {
  const api = createApi({
    plugins: {
      entries: {
        "telegram-user-bridge": {
          config: {
            profiles: [{ id: "shared", policyProfile: "shared" }],
          },
        },
      },
    },
  });
  register(api);

  let capturedUrl = "";
  globalThis.fetch = async (url) => {
    capturedUrl = url;
    return new Response(JSON.stringify({ messages: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  const messagesTool = getTool(api, "telegram_shared_get_messages");
  await messagesTool.execute("1", { peer: -1001, limit: 10, min_id: 77, since_unix: 1710000000, topic_id: 900 });

  assert.equal(
    capturedUrl,
    "http://127.0.0.1:8765/messages?peer=-1001&limit=10&min_id=77&since_unix=1710000000&topic_id=900"
  );
});

test("sources_ro profile is read-only and exposes source inventory tools", async () => {
  const api = createApi({
    plugins: {
      entries: {
        "telegram-user-bridge": {
          config: {
            profiles: [{ id: "sources", label: "Sources", mode: "sources_ro", policyProfile: "sources_ro" }],
          },
        },
      },
    },
  });
  register(api);

  assert.equal(getTool(api, "telegram_sources_send_message"), undefined);
  assert.ok(getTool(api, "telegram_sources_list_sources"));
  assert.ok(getTool(api, "telegram_sources_sync_sources"));
  assert.ok(getTool(api, "telegram_sources_list_topics"));
  assert.ok(getTool(api, "telegram_sources_get_messages"));

  let capturedUrl = "";
  let capturedInit = undefined;
  globalThis.fetch = async (url, init) => {
    capturedUrl = url;
    capturedInit = init;
    return new Response(
      JSON.stringify({
        sources: [{ peer_id: -1001, title: "News", username: "news", type: "channel", is_forum: false }],
      }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      }
    );
  };

  const listSourcesTool = getTool(api, "telegram_sources_list_sources");
  const result = await listSourcesTool.execute("1", { refresh: true });

  assert.equal(capturedUrl, "http://127.0.0.1:8765/sources?refresh=true");
  assert.equal(capturedInit.method, "GET");
  assert.match(result.content[0].text, /News/);
});

test("plugin lists forum topics and formats topic fetch ids", async () => {
  const api = createApi({
    plugins: {
      entries: {
        "telegram-user-bridge": {
          config: {
            profiles: [{ id: "sources", mode: "sources_ro", policyProfile: "sources_ro" }],
          },
        },
      },
    },
  });
  register(api);

  let capturedUrl = "";
  globalThis.fetch = async (url) => {
    capturedUrl = url;
    return new Response(
      JSON.stringify({
        topics: [{ id: 12, topic_id: 900, title: "Releases", unread_count: 2, pinned: true }],
      }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      }
    );
  };

  const topicsTool = getTool(api, "telegram_sources_list_topics");
  const result = await topicsTool.execute("1", { peer: -1001, limit: 10 });

  assert.equal(capturedUrl, "http://127.0.0.1:8765/topics?peer=-1001&limit=10");
  assert.match(result.content[0].text, /Releases/);
  assert.match(result.content[0].text, /topic_id: 900/);
});

test("plugin formats richer message metadata for source polling", async () => {
  const api = createApi({
    plugins: {
      entries: {
        "telegram-user-bridge": {
          config: {
            profiles: [{ id: "sources", mode: "sources_ro", policyProfile: "sources_ro" }],
          },
        },
      },
    },
  });
  register(api);

  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        messages: [
          {
            id: 77,
            text: "important update",
            out: false,
            sender_name: "Alice",
            topic_id: 900,
            date: "2026-03-14T10:00:00+00:00",
          },
        ],
      }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      }
    );

  const messagesTool = getTool(api, "telegram_sources_get_messages");
  const result = await messagesTool.execute("1", { peer: -1001, limit: 10, min_id: 70 });

  assert.match(result.content[0].text, /Alice/);
  assert.match(result.content[0].text, /topic:900/);
  assert.match(result.content[0].text, /important update/);
});

test("plugin returns clear rate limit message", async () => {
  const api = createApi();
  register(api);

  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: "Telegram rate limit hit. Retry after 17s." }), {
      status: 429,
      headers: {
        "content-type": "application/json",
        "retry-after": "17",
      },
    });

  const sendTool = getTool(api, "telegram_user_send_message");
  const result = await sendTool.execute("1", { peer: "@durov", text: "hello" });

  assert.match(result.content[0].text, /17s/);
});

test("plugin maps backend timeout to bridge unavailable text", async () => {
  const api = createApi();
  register(api);

  globalThis.fetch = async () => {
    throw new DOMException("The operation was aborted.", "AbortError");
  };

  const dialogsTool = getTool(api, "telegram_user_get_dialogs");
  const result = await dialogsTool.execute("1", {});

  assert.match(result.content[0].text, /Telegram bridge is unavailable/);
});

test("plugin registers DM channel and channel outbound uses backend send_message", async () => {
  const api = createApi({
    plugins: {
      entries: {
        "telegram-user-bridge": {
          config: {
            baseUrl: "http://127.0.0.1:8765",
            apiToken: "secret",
          },
        },
      },
    },
    channels: {
      "telegram-user-bridge": {
        accounts: {
          default: {
            enabled: true,
            policyProfile: "dm_inbox",
            allowFrom: ["123"],
            writeTo: ["123"],
          },
        },
      },
    },
  });
  register(api);

  assert.equal(api.channels.length, 1);
  assert.equal(api.channels[0].id, "telegram-user-bridge");
  assert.deepEqual(api.channels[0].reload.configPrefixes, [
    "channels.telegram-user-bridge",
    "plugins.entries.telegram-user-bridge",
  ]);
  assert.equal(typeof api.channels[0].config.inspectAccount, "function");
  assert.equal(typeof api.channels[0].gateway.stopAccount, "function");
  assert.deepEqual(api.channels[0].config.inspectAccount(api.config, "default"), {
    ok: true,
    accountId: "default",
    defaultAccountId: "default",
    configured: true,
    enabled: true,
    label: "default",
    baseUrl: "http://127.0.0.1:8765",
    timeoutMs: 30000,
    pollTimeoutMs: 25000,
    pollIntervalMs: 1500,
    strictPeerBindings: true,
    policyProfile: "dm_inbox",
    allowFrom: ["123"],
    writeTo: ["123"],
  });

  let capturedUrl = "";
  let capturedInit = undefined;
  globalThis.fetch = async (url, init) => {
    capturedUrl = url;
    capturedInit = init;
    return new Response(JSON.stringify({ ok: true, message_id: 321 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  const sendResult = await api.channels[0].outbound.sendText({
    to: "123",
    text: "hello",
    accountId: "default",
    cfg: api.config,
  });

  assert.equal(capturedUrl, "http://127.0.0.1:8765/send_message");
  assert.equal(capturedInit.method, "POST");
  assert.equal(capturedInit.headers.Authorization, "Bearer secret");
  assert.equal(capturedInit.headers["X-OpenClaw-Policy-Profile"], "dm_inbox");
  assert.equal(capturedInit.headers["X-OpenClaw-Allow-From"], "123");
  assert.equal(capturedInit.headers["X-OpenClaw-Write-To"], "123");
  assert.equal(sendResult.messageId, 321);
});

test("DM channel outbound rejects missing target with clear error", async () => {
  const api = createApi({
    channels: {
      "telegram-user-bridge": {
        accounts: {
          default: {
            enabled: true,
            policyProfile: "dm_inbox",
            allowFrom: ["123"],
            writeTo: ["123"],
          },
        },
      },
    },
  });
  register(api);

  await assert.rejects(
    api.channels[0].outbound.sendText({
      to: undefined,
      text: "hello",
      accountId: "default",
      cfg: api.config,
    }),
    /outbound target is missing or invalid/i
  );
});

test("DM gateway startAccount works with channelRuntime and explicit stopAccount", async () => {
  const api = createApi({
    channels: {
      "telegram-user-bridge": {
        accounts: {
          default: {
            enabled: true,
            strictPeerBindings: false,
          },
        },
      },
    },
  });
  register(api);

  globalThis.fetch = async () =>
    new Response(JSON.stringify({ events: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });

  const account = api.channels[0].config.resolveAccount(api.config, "default");
  const abortController = new AbortController();
  abortController.abort();
  const statuses = [];
  const channelRuntime = {
    reply: {
      resolveEnvelopeFormatOptions() {
        return {};
      },
      formatAgentEnvelope({ body }) {
        return body;
      },
      finalizeInboundContext(ctx) {
        return ctx;
      },
      async dispatchReplyWithBufferedBlockDispatcher() {},
    },
    routing: {
      resolveAgentRoute() {
        return { agentId: "owner-agent", accountId: "default", sessionKey: "test" };
      },
      buildAgentSessionKey() {
        return "test";
      },
    },
    session: {
      resolveStorePath() {
        return "/tmp/test";
      },
      readSessionUpdatedAt() {
        return 0;
      },
      async recordInboundSession() {},
    },
  };

  const handle = await api.channels[0].gateway.startAccount({
    account,
    cfg: api.config,
    channelRuntime,
    abortSignal: abortController.signal,
    getStatus() {
      return statuses.at(-1) ?? null;
    },
    setStatus(value) {
      statuses.push(value);
    },
  });

  assert.equal(typeof handle.stop, "function");
  await api.channels[0].gateway.stopAccount({
    account,
    cfg: api.config,
    channelRuntime,
    setStatus(value) {
      statuses.push(value);
    },
    getStatus() {
      return statuses.at(-1) ?? null;
    },
  });
  assert.match(JSON.stringify(statuses), /stopped/);
});

test("DM gateway records structured last route and acknowledges processed inbound events", async () => {
  const api = createApi({
    channels: {
      "telegram-user-bridge": {
        accounts: {
          default: {
            enabled: true,
            strictPeerBindings: true,
            allowFrom: ["123456789"],
            writeTo: ["123456789"],
          },
        },
      },
    },
    bindings: [
      {
        agentId: "owner-agent",
        match: {
          channel: "telegram-user-bridge",
          accountId: "default",
          peer: { kind: "direct", id: "123456789" },
        },
      },
    ],
  });
  register(api);

  const account = api.channels[0].config.resolveAccount(api.config, "default");
  const abortController = new AbortController();
  const statuses = [];
  let recordArgs = null;
  let sendPayload = null;
  let ackPayload = null;
  let acked = null;
  const ackedPromise = new Promise((resolve) => {
    acked = resolve;
  });
  const event = {
    id: 42,
    text: "hello from telegram",
    sender_id: "123456789",
    sender_name: "Alice",
    sender_username: "alice",
    date: "2026-03-14T10:00:00+00:00",
  };
  let pollCount = 0;

  globalThis.fetch = async (url, init) => {
    const normalizedUrl = String(url);
    if (normalizedUrl.includes("/dm/inbox/poll")) {
      pollCount += 1;
      return new Response(JSON.stringify({ events: pollCount === 1 ? [event] : [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (normalizedUrl.includes("/send_message")) {
      sendPayload = JSON.parse(init.body);
      return new Response(JSON.stringify({ ok: true, message_id: 999 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (normalizedUrl.includes("/dm/inbox/ack")) {
      ackPayload = JSON.parse(init.body);
      abortController.abort();
      acked();
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    throw new Error(`unexpected fetch: ${normalizedUrl}`);
  };

  const channelRuntime = {
    reply: {
      resolveEnvelopeFormatOptions() {
        return {};
      },
      formatAgentEnvelope({ body }) {
        return body;
      },
      finalizeInboundContext(ctx) {
        return ctx;
      },
      async dispatchReplyWithBufferedBlockDispatcher({ dispatcherOptions }) {
        await dispatcherOptions.deliver({ text: "reply from agent" });
      },
    },
    routing: {
      resolveAgentRoute() {
        throw new Error("resolveAgentRoute should not be used when strict bindings are enabled");
      },
      buildAgentSessionKey() {
        return "dm-session-key";
      },
    },
    session: {
      resolveStorePath() {
        return "/tmp/test";
      },
      readSessionUpdatedAt() {
        return 0;
      },
      async recordInboundSession(args) {
        recordArgs = args;
        args.updateLastRoute.channel.trim();
        args.updateLastRoute.to.trim();
      },
    },
  };

  let handle = null;
  try {
    handle = await api.channels[0].gateway.startAccount({
      account,
      cfg: api.config,
      channelRuntime,
      abortSignal: abortController.signal,
      getStatus() {
        return statuses.at(-1) ?? null;
      },
      setStatus(value) {
        statuses.push(value);
      },
    });

    await Promise.race([
      ackedPromise,
      new Promise((_, reject) => setTimeout(() => reject(new Error("timed out waiting for inbound DM ack")), 500)),
    ]);
  } finally {
    if (handle) {
      await handle.stop();
    }
  }

  assert.deepEqual(recordArgs.updateLastRoute, {
    sessionKey: "dm-session-key",
    channel: "telegram-user-bridge",
    to: "telegram-user-bridge:123456789",
    accountId: "default",
  });
  assert.deepEqual(sendPayload, {
    peer: "123456789",
    text: "reply from agent",
    reply_to: null,
  });
  assert.deepEqual(ackPayload, {
    sender_id: "123456789",
    sender_username: "alice",
    message_id: 42,
  });
  assert.match(JSON.stringify(statuses), /polling/);
});

test("strict DM binding resolves exact sender to agent", () => {
  const route = __test.resolveConfiguredDmBinding(
    {
      bindings: [
        {
          agentId: "owner-agent",
          match: {
            channel: "telegram-user-bridge",
            accountId: "default",
            peer: { kind: "direct", id: "123456789" },
          },
        },
        {
          agentId: "trusted-agent",
          match: {
            channel: "telegram-user-bridge",
            accountId: "default",
            peer: { kind: "direct", id: "987654321" },
          },
        },
      ],
    },
    {
      accountId: "default",
      defaultAccountId: "default",
      enabled: true,
      label: "Telegram User DM",
      baseUrl: "http://127.0.0.1:8765",
      strictPeerBindings: true,
      timeoutMs: 30000,
      pollTimeoutMs: 25000,
      pollIntervalMs: 1500,
    },
    {
      id: 10,
      text: "hello",
      sender_id: "123456789",
    }
  );

  assert.deepEqual(route, { agentId: "owner-agent", accountId: "default" });
});

test("strict DM binding does not match without exact peer binding", () => {
  const route = __test.resolveConfiguredDmBinding(
    {
      bindings: [
        {
          agentId: "owner-agent",
          match: {
            channel: "telegram-user-bridge",
            accountId: "default",
            peer: { kind: "direct", id: "123456789" },
          },
        },
      ],
    },
    {
      accountId: "default",
      defaultAccountId: "default",
      enabled: true,
      label: "Telegram User DM",
      baseUrl: "http://127.0.0.1:8765",
      strictPeerBindings: true,
      timeoutMs: 30000,
      pollTimeoutMs: 25000,
      pollIntervalMs: 1500,
    },
    {
      id: 10,
      text: "hello",
      sender_id: "987654321",
    }
  );

  assert.equal(route, null);
});

test("strict DM channel startup fails fast when allowFrom and bindings diverge", async () => {
  const api = createApi({
    plugins: {
      entries: {
        "telegram-user-bridge": {
          config: {
            baseUrl: "http://127.0.0.1:8765",
          },
        },
      },
    },
    channels: {
      "telegram-user-bridge": {
        accounts: {
          default: {
            enabled: true,
            strictPeerBindings: true,
            allowFrom: ["123456789"],
            writeTo: ["123456789"],
          },
        },
      },
    },
    bindings: [
      {
        agentId: "trusted-agent",
        match: {
          channel: "telegram-user-bridge",
          accountId: "default",
          peer: { kind: "direct", id: "987654321" },
        },
      },
    ],
  });
  register(api);

  await assert.rejects(
    api.channels[0].gateway.startAccount({
      account: api.channels[0].config.resolveAccount(api.config, "default"),
      cfg: api.config,
      runtime: {},
    }),
    /missing exact binding for allowed sender 123456789/
  );
});

test("ack helper retries transient failures and eventually succeeds", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (calls.length < 3) {
      return new Response(JSON.stringify({ detail: "Telegram bridge is temporarily unavailable." }), {
        status: 503,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  await __test.ackInboundDmEvent(
    {
      accountId: "default",
      defaultAccountId: "default",
      enabled: true,
      label: "Telegram User DM",
      baseUrl: "http://127.0.0.1:8765",
      strictPeerBindings: true,
      timeoutMs: 30000,
      pollTimeoutMs: 25000,
      pollIntervalMs: 1500,
    },
    {
      id: 11,
      text: "hello",
      sender_id: "123456789",
    }
  );

  assert.equal(calls.length, 3);
  assert.equal(calls[0].url, "http://127.0.0.1:8765/dm/inbox/ack");
});

test("poll backoff helper doubles delay and caps it", () => {
  assert.equal(__test.nextPollBackoffMs(1500, 1500), 3000);
  assert.equal(__test.nextPollBackoffMs(3000, 1500), 6000);
  assert.equal(__test.nextPollBackoffMs(20000, 1500), 30000);
});
