import assert from "node:assert/strict";
import test from "node:test";

import register from "./index.ts";

function createApi(config = {}) {
  const tools = [];
  return {
    config,
    logger: { warn() {} },
    registerTool(tool) {
      tools.push(tool);
    },
    tools,
  };
}

function getTool(api, name) {
  return api.tools.find((tool) => tool.name === name);
}

test("plugin forwards backend policy overrides through headers", async () => {
  const api = createApi({
    plugins: {
      entries: {
        "telegram-user-bridge": {
          config: {
            baseUrl: "http://127.0.0.1:8765/",
            apiToken: "secret",
            replyDelaySec: 2.5,
            replyDelayMaxSec: 4,
            allowFrom: ["@durov", "-1001"],
            denyFrom: ["spam"],
          },
        },
      },
    },
  });
  register(api);

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

  const sendTool = getTool(api, "telegram_user_send_message");
  const result = await sendTool.execute("1", { peer: "@durov", text: "hello" });

  assert.equal(capturedUrl, "http://127.0.0.1:8765/send_message");
  assert.equal(capturedInit.method, "POST");
  assert.equal(capturedInit.headers.Authorization, "Bearer secret");
  assert.equal(capturedInit.headers["X-OpenClaw-Reply-Delay-Sec"], "2.5");
  assert.equal(capturedInit.headers["X-OpenClaw-Reply-Delay-Max-Sec"], "4");
  assert.equal(capturedInit.headers["X-OpenClaw-Allow-From"], "@durov,-1001");
  assert.equal(capturedInit.headers["X-OpenClaw-Deny-From"], "spam");
  assert.match(result.content[0].text, /Message sent/);
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
