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
  assert.ok(getTool(api, "telegram_shared_get_messages"));
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

test("plugin passes min_id for incremental polling", async () => {
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
  await messagesTool.execute("1", { peer: -1001, limit: 10, min_id: 77 });

  assert.equal(capturedUrl, "http://127.0.0.1:8765/messages?peer=-1001&limit=10&min_id=77");
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
          agentId: "wife-agent",
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
        agentId: "wife-agent",
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
