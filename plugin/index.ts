/**
 * OpenClaw plugin: Unofficial Telegram User Bridge
 * Registers agent tools that call the Telethon HTTP bridge (live user account).
 * Requires the bridge backend to be running; use only with explicit user consent.
 */

import { Type } from "@sinclair/typebox";

const BRIDGE_UNAVAILABLE =
  "Telegram bridge is unavailable. Ensure the bridge service is running and session is configured.";

const HEADER_REPLY_DELAY_SEC = "X-OpenClaw-Reply-Delay-Sec";
const HEADER_REPLY_DELAY_MAX_SEC = "X-OpenClaw-Reply-Delay-Max-Sec";
const HEADER_ALLOW_FROM = "X-OpenClaw-Allow-From";
const HEADER_DENY_FROM = "X-OpenClaw-Deny-From";

type ToolContent = { content: Array<{ type: "text"; text: string }> };

type BridgeConfig = {
  baseUrl: string;
  apiToken?: string;
  timeoutMs: number;
  replyDelaySec?: number;
  replyDelayMaxSec?: number;
  allowFrom?: string[];
  denyFrom?: string[];
};

type BridgeResponse = {
  ok: boolean;
  data?: unknown;
  error?: string;
  status?: number;
  retryAfter?: number;
};

function toolResult(text: string): ToolContent {
  return { content: [{ type: "text" as const, text }] };
}

function serializePeerList(values: string[] | undefined, fallbackForEmpty: string | null): string | undefined {
  if (values === undefined) return undefined;
  if (values.length === 0) return fallbackForEmpty ?? undefined;
  return values.join(",");
}

function getConfig(api: { config: Record<string, unknown> }): BridgeConfig {
  const entries = (api.config?.plugins as Record<string, unknown>)?.entries as
    | Record<string, Record<string, unknown>>
    | undefined;
  const cfg = entries?.["telegram-user-bridge"]?.config as Record<string, unknown> | undefined;

  return {
    baseUrl: ((cfg?.baseUrl as string) || "http://127.0.0.1:8765").replace(/\/$/, ""),
    apiToken: cfg?.apiToken as string | undefined,
    timeoutMs: (cfg?.timeoutMs as number) || 25000,
    replyDelaySec: typeof cfg?.replyDelaySec === "number" ? (cfg.replyDelaySec as number) : undefined,
    replyDelayMaxSec:
      typeof cfg?.replyDelayMaxSec === "number" ? (cfg.replyDelayMaxSec as number) : undefined,
    allowFrom: Array.isArray(cfg?.allowFrom) ? (cfg.allowFrom as string[]) : undefined,
    denyFrom: Array.isArray(cfg?.denyFrom) ? (cfg.denyFrom as string[]) : undefined,
  };
}

function buildHeaders(config: BridgeConfig, extraHeaders: Record<string, string> = {}): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...extraHeaders };
  if (config.apiToken) headers["Authorization"] = `Bearer ${config.apiToken}`;
  if (config.replyDelaySec !== undefined) headers[HEADER_REPLY_DELAY_SEC] = String(config.replyDelaySec);
  if (config.replyDelayMaxSec !== undefined) headers[HEADER_REPLY_DELAY_MAX_SEC] = String(config.replyDelayMaxSec);

  const allowFrom = serializePeerList(config.allowFrom, "*");
  const denyFrom = serializePeerList(config.denyFrom, "");
  if (allowFrom !== undefined) headers[HEADER_ALLOW_FROM] = allowFrom;
  if (denyFrom !== undefined) headers[HEADER_DENY_FROM] = denyFrom;
  return headers;
}

async function fetchBridge(
  api: { config: Record<string, unknown>; logger?: { warn: (s: string) => void } },
  path: string,
  options: { method?: string; body?: string; headers?: Record<string, string> } = {}
): Promise<BridgeResponse> {
  const config = getConfig(api);
  const url = `${config.baseUrl}${path}`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.timeoutMs);

  try {
    const res = await fetch(url, {
      method: options.method || "GET",
      body: options.body,
      headers: buildHeaders(config, options.headers),
      signal: controller.signal,
    });
    clearTimeout(timeout);

    const text = await res.text();
    let data: unknown;
    try {
      data = text ? JSON.parse(text) : undefined;
    } catch {
      data = text;
    }

    if (!res.ok) {
      const detail =
        typeof data === "object" && data !== null && "detail" in data
          ? String((data as { detail: unknown }).detail)
          : res.statusText;
      const retryAfterHeader = res.headers.get("retry-after");
      const retryAfter = retryAfterHeader ? Number(retryAfterHeader) : undefined;
      return {
        ok: false,
        error: detail || `HTTP ${res.status}`,
        status: res.status,
        retryAfter: Number.isFinite(retryAfter) ? retryAfter : undefined,
      };
    }

    return { ok: true, data };
  } catch (error) {
    clearTimeout(timeout);
    const err = error instanceof Error ? error : new Error(String(error));
    if (err.name === "AbortError") {
      return { ok: false, error: "Request timed out" };
    }
    return { ok: false, error: err.message || "Network error" };
  }
}

function formatBridgeError(res: BridgeResponse): string {
  if (res.status === 401) {
    return "Telegram bridge rejected plugin credentials. Check apiToken.";
  }
  if (res.status === 429) {
    if (res.retryAfter) {
      if (res.error && /retry after/i.test(res.error)) {
        return res.error;
      }
      return res.error ? `${res.error} Retry after ${res.retryAfter}s.` : `Telegram rate limit hit. Retry after ${res.retryAfter}s.`;
    }
    return res.error || "Telegram rate limit hit. Try again later.";
  }
  if (res.status === 403 || res.status === 400) {
    return res.error || "Telegram request was rejected.";
  }
  if (res.status === 504) {
    return "Telegram API did not respond in time. Retry carefully.";
  }
  if (
    res.status === 502 ||
    res.status === 503 ||
    res.error === "Request failed" ||
    res.error === "Request timed out"
  ) {
    return BRIDGE_UNAVAILABLE;
  }
  return res.error || BRIDGE_UNAVAILABLE;
}

interface PluginApi {
  config: Record<string, unknown>;
  registerTool: (tool: unknown, opts?: { optional?: boolean }) => void;
  logger?: { warn: (s: string) => void };
}

export default function register(api: PluginApi) {
  const optional = { optional: true };

  api.registerTool(
    {
      name: "telegram_user_send_message",
      description:
        "Send a text message from your live Telegram user account (MTProto). Use only when the user explicitly asks to send something to Telegram. Peer can be username (e.g. @durov), chat id, or 'me' for Saved Messages.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], {
          description: "Username (@name), chat id, or 'me'",
        }),
        text: Type.String({ minLength: 1, maxLength: 4096, description: "Message text" }),
        reply_to: Type.Optional(Type.Number({ description: "Message id to reply to" })),
      }),
      async execute(_id: string, params: { peer: string | number; text: string; reply_to?: number }) {
        const res = await fetchBridge(api, "/send_message", {
          method: "POST",
          body: JSON.stringify({
            peer: params.peer,
            text: params.text,
            reply_to: params.reply_to ?? null,
          }),
        });
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const messageId = (res.data as { message_id?: unknown } | undefined)?.message_id;
        return toolResult(messageId ? `Message sent (id: ${messageId}).` : "Message sent.");
      },
    },
    optional
  );

  api.registerTool(
    {
      name: "telegram_user_get_dialogs",
      description:
        "List recent dialogs (chats) from your live Telegram user account. Use when the user asks to see Telegram chats or find a chat. Requires bridge to be running.",
      parameters: Type.Object({
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 50, default: 20, description: "Max number of dialogs" })),
      }),
      async execute(_id: string, params: { limit?: number }) {
        const res = await fetchBridge(api, `/dialogs?limit=${Math.min(50, Math.max(1, params.limit ?? 20))}`);
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const dialogs = (res.data as { dialogs?: Array<{ id: unknown; title: string; username?: string }> })?.dialogs ?? [];
        const lines = dialogs.map((dialog) => `- ${dialog.title || dialog.username || dialog.id} (id: ${dialog.id}${dialog.username ? `, @${dialog.username}` : ""})`);
        return toolResult(lines.length ? lines.join("\n") : "No dialogs.");
      },
    },
    optional
  );

  api.registerTool(
    {
      name: "telegram_user_get_messages",
      description:
        "Get recent messages from a Telegram chat. Peer can be username (@name), chat id, or 'me'. Use when the user explicitly asks to read messages from Telegram.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Username, chat id, or 'me'" }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 50, default: 20 })),
      }),
      async execute(_id: string, params: { peer: string | number; limit?: number }) {
        const limit = Math.min(50, Math.max(1, params.limit ?? 20));
        const peer = encodeURIComponent(String(params.peer));
        const res = await fetchBridge(api, `/messages?peer=${peer}&limit=${limit}`);
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const messages = (res.data as { messages?: Array<{ id: unknown; text: string; date?: string; out?: boolean }> })?.messages ?? [];
        const lines = messages.map((message) => `[${message.out ? "out" : "in"}] ${message.text || "(no text)"}`);
        return toolResult(lines.length ? lines.join("\n") : "No messages.");
      },
    },
    optional
  );
}
