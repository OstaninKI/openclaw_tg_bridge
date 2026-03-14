/**
 * OpenClaw plugin: Unofficial Telegram User Bridge
 * Registers agent tools that call the Telethon HTTP bridge (live user account).
 * Requires the bridge backend to be running; use only with explicit user consent.
 */

import { Type } from "@sinclair/typebox";

const BRIDGE_UNAVAILABLE =
  "Telegram bridge is unavailable. Ensure the bridge service is running and session is configured.";
const CHANNEL_ID = "telegram-user-bridge";
const CHANNEL_LABEL = "Unofficial Telegram User DM";

const HEADER_POLICY_PROFILE = "X-OpenClaw-Policy-Profile";
const HEADER_REPLY_DELAY_SEC = "X-OpenClaw-Reply-Delay-Sec";
const HEADER_REPLY_DELAY_MAX_SEC = "X-OpenClaw-Reply-Delay-Max-Sec";
const HEADER_ALLOW_FROM = "X-OpenClaw-Allow-From";
const HEADER_DENY_FROM = "X-OpenClaw-Deny-From";
const HEADER_WRITE_TO = "X-OpenClaw-Write-To";
const HEADER_DENY_WRITE_TO = "X-OpenClaw-Deny-Write-To";

type ToolContent = { content: Array<{ type: "text"; text: string }> };
type ProfileMode = "interactive" | "sources_ro";

type PolicyHeaderConfig = {
  policyProfile?: string;
  replyDelaySec?: number;
  replyDelayMaxSec?: number;
  allowFrom?: string[];
  denyFrom?: string[];
  writeTo?: string[];
  denyWriteTo?: string[];
};

type ProfileConfig = PolicyHeaderConfig & {
  id: string;
  label: string;
  mode: ProfileMode;
};

type PluginConfig = {
  baseUrl: string;
  apiToken?: string;
  timeoutMs: number;
  profiles: ProfileConfig[];
};

type ChannelAccountConfig = PolicyHeaderConfig & {
  accountId: string;
  defaultAccountId: string;
  enabled: boolean;
  label: string;
  baseUrl: string;
  apiToken?: string;
  strictPeerBindings: boolean;
  timeoutMs: number;
  pollTimeoutMs: number;
  pollIntervalMs: number;
};

type DmChannelConfig = {
  defaultAccountId: string;
  accounts: ChannelAccountConfig[];
};

type BridgeResponse = {
  ok: boolean;
  data?: unknown;
  error?: string;
  status?: number;
  retryAfter?: number;
};

type DmInboxEvent = {
  id: number;
  text: string;
  sender_id: string | number;
  sender_name?: string;
  sender_username?: string;
  date?: string;
};

function toolResult(text: string): ToolContent {
  return { content: [{ type: "text" as const, text }] };
}

function serializePeerList(values: string[] | undefined, fallbackForEmpty: string | null): string | undefined {
  if (values === undefined) return undefined;
  if (values.length === 0) return fallbackForEmpty ?? undefined;
  return values.join(",");
}

function slugifyToolId(value: string): string {
  const slug = value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return slug || "profile";
}

function normalizeProfile(raw: Record<string, unknown>, fallbackId: string): ProfileConfig | null {
  const id = typeof raw.id === "string" && raw.id.trim() ? raw.id.trim() : fallbackId;
  if (raw.enabled === false) return null;
  const mode = raw.mode === "sources_ro" ? "sources_ro" : "interactive";
  return {
    id,
    label: typeof raw.label === "string" && raw.label.trim() ? raw.label.trim() : id,
    mode,
    policyProfile:
      typeof raw.policyProfile === "string" && raw.policyProfile.trim() ? raw.policyProfile.trim() : undefined,
    replyDelaySec: typeof raw.replyDelaySec === "number" ? raw.replyDelaySec : undefined,
    replyDelayMaxSec: typeof raw.replyDelayMaxSec === "number" ? raw.replyDelayMaxSec : undefined,
    allowFrom: Array.isArray(raw.allowFrom) ? (raw.allowFrom as string[]) : undefined,
    denyFrom: Array.isArray(raw.denyFrom) ? (raw.denyFrom as string[]) : undefined,
    writeTo: Array.isArray(raw.writeTo) ? (raw.writeTo as string[]) : undefined,
    denyWriteTo: Array.isArray(raw.denyWriteTo) ? (raw.denyWriteTo as string[]) : undefined,
  };
}

function getConfig(api: { config: Record<string, unknown> }): PluginConfig {
  const entries = (api.config?.plugins as Record<string, unknown>)?.entries as
    | Record<string, Record<string, unknown>>
    | undefined;
  const cfg = entries?.[CHANNEL_ID]?.config as Record<string, unknown> | undefined;

  const rawProfiles = Array.isArray(cfg?.profiles) ? (cfg?.profiles as Array<Record<string, unknown>>) : [];
  const profiles = rawProfiles
    .map((profile, index) => normalizeProfile(profile, `profile_${index + 1}`))
    .filter((profile): profile is ProfileConfig => profile !== null);

  if (profiles.length === 0) {
    profiles.push({
      id: "user",
      label: "User",
      mode: "interactive",
      policyProfile:
        typeof cfg?.policyProfile === "string" && (cfg.policyProfile as string).trim()
          ? (cfg.policyProfile as string).trim()
          : undefined,
      replyDelaySec: typeof cfg?.replyDelaySec === "number" ? (cfg.replyDelaySec as number) : undefined,
      replyDelayMaxSec: typeof cfg?.replyDelayMaxSec === "number" ? (cfg.replyDelayMaxSec as number) : undefined,
      allowFrom: Array.isArray(cfg?.allowFrom) ? (cfg.allowFrom as string[]) : undefined,
      denyFrom: Array.isArray(cfg?.denyFrom) ? (cfg.denyFrom as string[]) : undefined,
      writeTo: Array.isArray(cfg?.writeTo) ? (cfg.writeTo as string[]) : undefined,
      denyWriteTo: Array.isArray(cfg?.denyWriteTo) ? (cfg.denyWriteTo as string[]) : undefined,
    });
  }

  return {
    baseUrl: ((cfg?.baseUrl as string) || "http://127.0.0.1:8765").replace(/\/$/, ""),
    apiToken: cfg?.apiToken as string | undefined,
    timeoutMs: (cfg?.timeoutMs as number) || 25000,
    profiles,
  };
}

function normalizeChannelAccount(
  raw: Record<string, unknown>,
  accountId: string,
  pluginConfig: PluginConfig
): ChannelAccountConfig {
  const pollTimeoutMs =
    typeof raw.pollTimeoutMs === "number" && raw.pollTimeoutMs > 0 ? raw.pollTimeoutMs : 25000;
  const timeoutMsRaw =
    typeof raw.timeoutMs === "number" && raw.timeoutMs > 0 ? raw.timeoutMs : pluginConfig.timeoutMs;
  const timeoutMs = Math.max(timeoutMsRaw, pollTimeoutMs + 5000);
  return {
    accountId,
    defaultAccountId: accountId,
    enabled: raw.enabled !== false,
    label: typeof raw.label === "string" && raw.label.trim() ? raw.label.trim() : accountId,
    baseUrl:
      typeof raw.baseUrl === "string" && raw.baseUrl.trim()
        ? raw.baseUrl.trim().replace(/\/$/, "")
        : pluginConfig.baseUrl,
    apiToken: typeof raw.apiToken === "string" && raw.apiToken.trim() ? raw.apiToken.trim() : pluginConfig.apiToken,
    strictPeerBindings: typeof raw.strictPeerBindings === "boolean" ? raw.strictPeerBindings : true,
    timeoutMs,
    pollTimeoutMs,
    pollIntervalMs:
      typeof raw.pollIntervalMs === "number" && raw.pollIntervalMs >= 0 ? raw.pollIntervalMs : 1500,
    policyProfile:
      typeof raw.policyProfile === "string" && raw.policyProfile.trim() ? raw.policyProfile.trim() : undefined,
    replyDelaySec: typeof raw.replyDelaySec === "number" ? raw.replyDelaySec : undefined,
    replyDelayMaxSec: typeof raw.replyDelayMaxSec === "number" ? raw.replyDelayMaxSec : undefined,
    allowFrom: Array.isArray(raw.allowFrom) ? (raw.allowFrom as string[]) : undefined,
    denyFrom: Array.isArray(raw.denyFrom) ? (raw.denyFrom as string[]) : undefined,
    writeTo: Array.isArray(raw.writeTo) ? (raw.writeTo as string[]) : undefined,
    denyWriteTo: Array.isArray(raw.denyWriteTo) ? (raw.denyWriteTo as string[]) : undefined,
  };
}

function getDmChannelConfig(api: { config: Record<string, unknown> }): DmChannelConfig | null {
  return getDmChannelConfigFromConfig(api.config, getConfig(api));
}

function getDmChannelConfigFromConfig(
  config: Record<string, unknown>,
  pluginConfig: PluginConfig
): DmChannelConfig | null {
  const channels = config?.channels as Record<string, unknown> | undefined;
  const cfg = channels?.[CHANNEL_ID] as Record<string, unknown> | undefined;
  const rawAccounts = (cfg?.accounts as Record<string, Record<string, unknown>> | undefined) ?? {};
  const accountIds = Object.keys(rawAccounts);
  if (accountIds.length === 0) {
    return null;
  }
  const accounts = accountIds.map((accountId) =>
    normalizeChannelAccount(rawAccounts[accountId] ?? {}, accountId, pluginConfig)
  );
  const defaultAccountId =
    typeof cfg?.defaultAccountId === "string" && cfg.defaultAccountId.trim()
      ? cfg.defaultAccountId.trim()
      : accounts[0]?.accountId ?? "default";
  return {
    defaultAccountId,
    accounts: accounts.map((account) => ({
      ...account,
      defaultAccountId,
    })),
  };
}

function resolveDmChannelAccount(
  api: { config: Record<string, unknown> },
  accountId?: string | null
): ChannelAccountConfig {
  return resolveDmChannelAccountFromConfig(api.config, getConfig(api), accountId);
}

function resolveDmChannelAccountFromConfig(
  config: Record<string, unknown>,
  pluginConfig: PluginConfig,
  accountId?: string | null
): ChannelAccountConfig {
  const channelConfig = getDmChannelConfigFromConfig(config, pluginConfig);
  if (!channelConfig) {
    return normalizeChannelAccount({}, accountId?.trim() || "default", pluginConfig);
  }
  const desiredId = accountId?.trim() || channelConfig.defaultAccountId;
  return (
    channelConfig.accounts.find((account) => account.accountId === desiredId) ??
    channelConfig.accounts[0]
  );
}

function buildHeaders(
  requestConfig: { apiToken?: string },
  policyConfig: PolicyHeaderConfig,
  extraHeaders: Record<string, string> = {}
): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...extraHeaders };
  if (requestConfig.apiToken) headers["Authorization"] = `Bearer ${requestConfig.apiToken}`;
  if (policyConfig.policyProfile) headers[HEADER_POLICY_PROFILE] = policyConfig.policyProfile;
  if (policyConfig.replyDelaySec !== undefined) headers[HEADER_REPLY_DELAY_SEC] = String(policyConfig.replyDelaySec);
  if (policyConfig.replyDelayMaxSec !== undefined)
    headers[HEADER_REPLY_DELAY_MAX_SEC] = String(policyConfig.replyDelayMaxSec);

  const allowFrom = serializePeerList(policyConfig.allowFrom, null);
  const denyFrom = serializePeerList(policyConfig.denyFrom, "");
  const writeTo = serializePeerList(policyConfig.writeTo, "");
  const denyWriteTo = serializePeerList(policyConfig.denyWriteTo, "");
  if (allowFrom !== undefined) headers[HEADER_ALLOW_FROM] = allowFrom;
  if (denyFrom !== undefined) headers[HEADER_DENY_FROM] = denyFrom;
  if (writeTo !== undefined) headers[HEADER_WRITE_TO] = writeTo;
  if (denyWriteTo !== undefined) headers[HEADER_DENY_WRITE_TO] = denyWriteTo;
  return headers;
}

async function fetchBridgeWithConfig(
  requestConfig: { baseUrl: string; apiToken?: string; timeoutMs: number },
  policyConfig: PolicyHeaderConfig,
  path: string,
  options: { method?: string; body?: string; headers?: Record<string, string> } = {}
): Promise<BridgeResponse> {
  const url = `${requestConfig.baseUrl}${path}`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), requestConfig.timeoutMs);

  try {
    const res = await fetch(url, {
      method: options.method || "GET",
      body: options.body,
      headers: buildHeaders(requestConfig, policyConfig, options.headers),
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

async function fetchBridge(
  api: { config: Record<string, unknown>; logger?: { warn: (s: string) => void } },
  profile: ProfileConfig,
  path: string,
  options: { method?: string; body?: string; headers?: Record<string, string> } = {}
): Promise<BridgeResponse> {
  const config = getConfig(api);
  return fetchBridgeWithConfig(config, profile, path, options);
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
      return res.error
        ? `${res.error} Retry after ${res.retryAfter}s.`
        : `Telegram rate limit hit. Retry after ${res.retryAfter}s.`;
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
  registerChannel?: (options: { plugin: unknown }) => void;
  runtime?: {
    channel?: {
      reply: Record<string, (...args: unknown[]) => unknown>;
      routing: Record<string, (...args: unknown[]) => unknown>;
      session: Record<string, (...args: unknown[]) => unknown>;
    };
  };
  logger?: { warn: (s: string) => void };
}

function resolveDmScope(cfg: Record<string, unknown>): string {
  const session = cfg.session as Record<string, unknown> | undefined;
  const dmScope = typeof session?.dmScope === "string" ? session.dmScope : undefined;
  return !dmScope || dmScope === "main" ? "per-channel-peer" : dmScope;
}

function normalizePeerKey(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  let normalized = String(value).trim();
  if (!normalized) return null;
  if (normalized.startsWith("@")) normalized = normalized.slice(1);
  if (/^-?\d+$/.test(normalized)) {
    try {
      return BigInt(normalized).toString();
    } catch {
      return normalized;
    }
  }
  return normalized.toLowerCase();
}

function resolveConfiguredDmBinding(
  cfg: Record<string, unknown>,
  account: ChannelAccountConfig,
  event: DmInboxEvent
): { agentId: string; accountId: string } | null {
  const bindings = Array.isArray(cfg.bindings) ? (cfg.bindings as Array<Record<string, unknown>>) : [];
  const senderKeys = new Set(
    [normalizePeerKey(event.sender_id), normalizePeerKey(event.sender_username)].filter(
      (value): value is string => Boolean(value)
    )
  );
  if (senderKeys.size === 0) {
    return null;
  }

  for (const binding of bindings) {
    const agentId = typeof binding.agentId === "string" && binding.agentId.trim() ? binding.agentId.trim() : null;
    const match = binding.match as Record<string, unknown> | undefined;
    const peer = match?.peer as Record<string, unknown> | undefined;
    const bindingChannel = typeof match?.channel === "string" ? match.channel.trim() : "";
    const bindingAccountId =
      typeof match?.accountId === "string" && match.accountId.trim() ? match.accountId.trim() : undefined;
    const bindingPeerKind = typeof peer?.kind === "string" ? peer.kind.trim() : "";
    const bindingPeerId = normalizePeerKey(peer?.id);

    if (!agentId || bindingChannel !== CHANNEL_ID || bindingPeerKind !== "direct" || !bindingPeerId) {
      continue;
    }
    if (bindingAccountId && bindingAccountId !== "*" && bindingAccountId !== account.accountId) {
      continue;
    }
    if (!bindingAccountId && account.accountId !== account.defaultAccountId) {
      continue;
    }
    if (!senderKeys.has(bindingPeerId)) {
      continue;
    }
    return {
      agentId,
      accountId: account.accountId,
    };
  }

  return null;
}

function collectRelevantDirectBindings(
  cfg: Record<string, unknown>,
  account: ChannelAccountConfig
): Array<{ agentId: string; peerId: string }> {
  const bindings = Array.isArray(cfg.bindings) ? (cfg.bindings as Array<Record<string, unknown>>) : [];
  const relevant: Array<{ agentId: string; peerId: string }> = [];

  for (const binding of bindings) {
    const agentId = typeof binding.agentId === "string" && binding.agentId.trim() ? binding.agentId.trim() : null;
    const match = binding.match as Record<string, unknown> | undefined;
    const peer = match?.peer as Record<string, unknown> | undefined;
    const bindingChannel = typeof match?.channel === "string" ? match.channel.trim() : "";
    const bindingAccountId =
      typeof match?.accountId === "string" && match.accountId.trim() ? match.accountId.trim() : undefined;
    const bindingPeerKind = typeof peer?.kind === "string" ? peer.kind.trim() : "";
    const bindingPeerId = normalizePeerKey(peer?.id);

    if (!agentId || bindingChannel !== CHANNEL_ID || bindingPeerKind !== "direct" || !bindingPeerId) {
      continue;
    }
    if (bindingAccountId && bindingAccountId !== "*" && bindingAccountId !== account.accountId) {
      continue;
    }
    if (!bindingAccountId && account.accountId !== account.defaultAccountId) {
      continue;
    }
    relevant.push({ agentId, peerId: bindingPeerId });
  }

  return relevant;
}

function validateStrictDmAccountConfig(cfg: Record<string, unknown>, account: ChannelAccountConfig): string[] {
  if (!account.strictPeerBindings) {
    return [];
  }

  const errors: string[] = [];
  const allowFrom = Array.isArray(account.allowFrom) ? account.allowFrom : [];
  const writeTo = Array.isArray(account.writeTo) ? account.writeTo : [];
  const allowIds = new Set<string>();
  const writeIds = new Set<string>();

  if (allowFrom.length === 0) {
    errors.push(`account "${account.accountId}": strict mode requires explicit numeric allowFrom list`);
  }
  if (writeTo.length === 0) {
    errors.push(`account "${account.accountId}": strict mode requires explicit numeric writeTo list`);
  }

  for (const rawPeer of allowFrom) {
    const peerId = normalizePeerKey(rawPeer);
    if (!peerId || !/^-?\d+$/.test(peerId)) {
      errors.push(`account "${account.accountId}": allowFrom must contain numeric Telegram user ids only (${String(rawPeer)})`);
      continue;
    }
    allowIds.add(peerId);
  }

  for (const rawPeer of writeTo) {
    const peerId = normalizePeerKey(rawPeer);
    if (!peerId || !/^-?\d+$/.test(peerId)) {
      errors.push(`account "${account.accountId}": writeTo must contain numeric Telegram user ids only (${String(rawPeer)})`);
      continue;
    }
    writeIds.add(peerId);
  }

  const bindings = collectRelevantDirectBindings(cfg, account);
  const bindingByPeer = new Map<string, string>();
  const duplicateBindingPeers = new Set<string>();
  for (const binding of bindings) {
    const previous = bindingByPeer.get(binding.peerId);
    if (previous && previous !== binding.agentId) {
      duplicateBindingPeers.add(binding.peerId);
      continue;
    }
    bindingByPeer.set(binding.peerId, binding.agentId);
  }

  for (const peerId of duplicateBindingPeers) {
    errors.push(`account "${account.accountId}": multiple agent bindings found for sender ${peerId}`);
  }

  for (const peerId of allowIds) {
    if (!writeIds.has(peerId)) {
      errors.push(`account "${account.accountId}": writeTo must include every allowed sender (${peerId})`);
    }
    if (!bindingByPeer.has(peerId)) {
      errors.push(`account "${account.accountId}": missing exact binding for allowed sender ${peerId}`);
    }
  }

  for (const peerId of bindingByPeer.keys()) {
    if (!allowIds.has(peerId)) {
      errors.push(`account "${account.accountId}": binding exists for sender ${peerId} but allowFrom does not include it`);
    }
  }

  return errors;
}

function sleepWithAbort(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, Math.max(0, ms));
    const onAbort = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      resolve();
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function buildDmTarget(senderId: string | number): string {
  return `${CHANNEL_ID}:${String(senderId)}`;
}

function stripDmTargetPrefix(target: string): string {
  return target.trim().replace(/^(telegram-user-bridge|tguser|tgdm):/i, "").trim();
}

function buildDmText(payload: { text?: string; mediaUrl?: string; mediaUrls?: string[] }): string {
  const parts = [
    typeof payload.text === "string" ? payload.text.trim() : "",
    typeof payload.mediaUrl === "string" ? payload.mediaUrl.trim() : "",
    ...(Array.isArray(payload.mediaUrls) ? payload.mediaUrls.map((item) => String(item).trim()) : []),
  ].filter(Boolean);
  return parts.join("\n\n");
}

async function ackInboundDmEvent(account: ChannelAccountConfig, event: DmInboxEvent): Promise<void> {
  const backoffMs = [0, 250, 750, 1500];
  let lastError: Error | null = null;
  let nextDelayMs = 0;

  for (let attempt = 0; attempt < backoffMs.length; attempt += 1) {
    if (nextDelayMs > 0) {
      await sleepWithAbort(nextDelayMs);
    }
    const response = await fetchBridgeWithConfig(account, account, "/dm/inbox/ack", {
      method: "POST",
      body: JSON.stringify({
        sender_id: event.sender_id,
        sender_username: event.sender_username ?? null,
        message_id: event.id,
      }),
    });
    if (response.ok) {
      return;
    }

    lastError = new Error(formatBridgeError(response));
    const shouldRetry =
      attempt < backoffMs.length - 1 &&
      (response.status === 429 ||
        response.status === 502 ||
        response.status === 503 ||
        response.status === 504 ||
        response.error === "Request timed out" ||
        response.error === "Network error");
    if (!shouldRetry) {
      throw lastError;
    }
    nextDelayMs =
      typeof response.retryAfter === "number" && Number.isFinite(response.retryAfter) && response.retryAfter > 0
        ? response.retryAfter * 1000
        : backoffMs[attempt + 1] ?? 0;
  }
  throw lastError ?? new Error("Failed to acknowledge inbound DM.");
}

async function deliverInboundDmReply(
  account: ChannelAccountConfig,
  senderId: string | number,
  payload: { text?: string; mediaUrl?: string; mediaUrls?: string[] }
): Promise<void> {
  const text = buildDmText(payload);
  if (!text) return;
  const response = await fetchBridgeWithConfig(account, account, "/send_message", {
    method: "POST",
    body: JSON.stringify({
      peer: senderId,
      text,
      reply_to: null,
    }),
  });
  if (!response.ok) {
    throw new Error(formatBridgeError(response));
  }
}

async function processInboundDmEvent(params: {
  api: PluginApi;
  cfg: Record<string, unknown>;
  account: ChannelAccountConfig;
  event: DmInboxEvent;
  runtime: NonNullable<PluginApi["runtime"]>;
}): Promise<"processed" | "skipped"> {
  const core = params.runtime.channel;
  if (!core) {
    throw new Error("OpenClaw channel runtime is not available.");
  }
  const senderId = String(params.event.sender_id);
  const configuredRoute = resolveConfiguredDmBinding(params.cfg, params.account, params.event);
  const route =
    configuredRoute ??
    (params.account.strictPeerBindings
      ? null
      : (core.routing.resolveAgentRoute as (...args: unknown[]) => {
          agentId: string;
          accountId: string;
          sessionKey: string;
        })({
          cfg: params.cfg,
          channel: CHANNEL_ID,
          accountId: params.account.accountId,
          peer: { kind: "direct", id: senderId },
        }));
  if (!route) {
    params.api.logger?.warn(
      `telegram-user-bridge DM dropped: no exact binding for sender ${senderId} on account ${params.account.accountId}`
    );
    return "skipped";
  }
  const storePath = (core.session.resolveStorePath as (...args: unknown[]) => string)(
    (params.cfg.session as Record<string, unknown> | undefined)?.store,
    { agentId: route.agentId }
  );
  const sessionKey = String(
    (core.routing.buildAgentSessionKey as (...args: unknown[]) => unknown)({
      agentId: route.agentId,
      channel: CHANNEL_ID,
      accountId: route.accountId,
      peer: { kind: "direct", id: senderId },
      dmScope: resolveDmScope(params.cfg),
      identityLinks: (params.cfg.session as Record<string, unknown> | undefined)?.identityLinks,
    })
  ).toLowerCase();
  const previousTimestamp = (core.session.readSessionUpdatedAt as (...args: unknown[]) => unknown)({
    storePath,
    sessionKey,
  });
  const envelopeOptions = (core.reply.resolveEnvelopeFormatOptions as (...args: unknown[]) => unknown)(params.cfg);
  const rawBody = params.event.text?.trim() || "[Non-text Telegram message]";
  const fromLabel =
    params.event.sender_name ||
    (params.event.sender_username ? `@${params.event.sender_username}` : `user:${senderId}`);
  const body = (core.reply.formatAgentEnvelope as (...args: unknown[]) => string)({
    channel: CHANNEL_LABEL,
    from: fromLabel,
    timestamp: params.event.date ? Date.parse(params.event.date) : Date.now(),
    previousTimestamp,
    envelope: envelopeOptions,
    body: rawBody,
  });
  const normalizedTarget = buildDmTarget(senderId);
  const ctxPayload = (core.reply.finalizeInboundContext as (...args: unknown[]) => Record<string, unknown>)({
    Body: body,
    BodyForAgent: rawBody,
    RawBody: rawBody,
    CommandBody: rawBody,
    BodyForCommands: rawBody,
    From: normalizedTarget,
    To: normalizedTarget,
    SessionKey: sessionKey,
    AccountId: route.accountId,
    ChatType: "direct",
    ConversationLabel: fromLabel,
    SenderName: params.event.sender_name ?? undefined,
    SenderId: senderId,
    SenderUsername: params.event.sender_username ?? undefined,
    CommandAuthorized: true,
    Provider: CHANNEL_ID,
    Surface: CHANNEL_ID,
    MessageSid: String(params.event.id),
    OriginatingChannel: CHANNEL_ID,
    OriginatingTo: normalizedTarget,
  });
  await (core.session.recordInboundSession as (...args: unknown[]) => Promise<void>)({
    storePath,
    sessionKey,
    ctx: ctxPayload,
    onRecordError: (error: unknown) => {
      params.api.logger?.warn(`telegram-user-bridge session meta update failed: ${String(error)}`);
    },
  });
  await (core.reply.dispatchReplyWithBufferedBlockDispatcher as (...args: unknown[]) => Promise<void>)({
    ctx: ctxPayload,
    cfg: params.cfg,
    dispatcherOptions: {
      deliver: async (payload: { text?: string; mediaUrl?: string; mediaUrls?: string[] }) => {
        await deliverInboundDmReply(params.account, senderId, payload);
      },
      onError: (error: unknown) => {
        params.api.logger?.warn(`telegram-user-bridge DM reply failed: ${String(error)}`);
      },
    },
    replyOptions: {},
  });
  return "processed";
}

async function startDmChannelMonitor(params: {
  api: PluginApi;
  cfg: Record<string, unknown>;
  account: ChannelAccountConfig;
  runtime: NonNullable<PluginApi["runtime"]>;
  abortSignal?: AbortSignal;
}): Promise<{ stop: () => Promise<void> }> {
  let stopped = false;
  const loop = (async () => {
    while (!stopped && !params.abortSignal?.aborted) {
      const response = await fetchBridgeWithConfig(
        params.account,
        params.account,
        `/dm/inbox/poll?timeout_ms=${Math.max(1000, params.account.pollTimeoutMs)}&limit=10`
      );
      if (!response.ok) {
        params.api.logger?.warn(`telegram-user-bridge DM poll failed: ${formatBridgeError(response)}`);
        await sleepWithAbort(params.account.pollIntervalMs, params.abortSignal);
        continue;
      }
      const events = ((response.data as { events?: DmInboxEvent[] } | undefined)?.events ?? []).filter(
        (event): event is DmInboxEvent => typeof event?.id === "number"
      );
      if (events.length === 0) {
        continue;
      }
      for (const event of events) {
        if (stopped || params.abortSignal?.aborted) {
          return;
        }
        try {
          const result = await processInboundDmEvent({
            api: params.api,
            cfg: params.cfg,
            account: params.account,
            event,
            runtime: params.runtime,
          });
          if (result === "processed" || result === "skipped") {
            await ackInboundDmEvent(params.account, event);
          }
        } catch (error) {
          params.api.logger?.warn(`telegram-user-bridge DM event failed: ${String(error)}`);
          await sleepWithAbort(params.account.pollIntervalMs, params.abortSignal);
          break;
        }
      }
    }
  })();

  return {
    stop: async () => {
      stopped = true;
      await Promise.race([loop, sleepWithAbort(params.account.timeoutMs)]);
    },
  };
}

function registerDmChannel(api: PluginApi): void {
  if (typeof api.registerChannel !== "function") {
    return;
  }
  const pluginConfig = getConfig(api);
  const plugin = {
    id: CHANNEL_ID,
    meta: {
      id: CHANNEL_ID,
      label: CHANNEL_LABEL,
      selectionLabel: CHANNEL_LABEL,
      docsPath: "/channels/telegram-user-bridge",
      blurb: "Unofficial live Telegram user DM channel via Telethon bridge.",
      aliases: ["tguser", "tgdm"],
    },
    capabilities: {
      chatTypes: ["direct"],
      media: false,
      threads: false,
      polls: false,
      nativeCommands: false,
      blockStreaming: true,
    },
    reload: { configPrefixes: [`channels.${CHANNEL_ID}`] },
    config: {
      listAccountIds: (cfg: Record<string, unknown>) =>
        getDmChannelConfigFromConfig(cfg, pluginConfig)?.accounts.map((account) => account.accountId) ?? [],
      resolveAccount: (cfg: Record<string, unknown>, accountId?: string) =>
        resolveDmChannelAccountFromConfig(cfg, pluginConfig, accountId),
      defaultAccountId: (cfg: Record<string, unknown>) =>
        getDmChannelConfigFromConfig(cfg, pluginConfig)?.defaultAccountId ?? "default",
    },
    outbound: {
      deliveryMode: "direct",
      sendText: async ({
        to,
        text,
        accountId,
        cfg,
      }: {
        to: string;
        text: string;
        accountId?: string;
        cfg: Record<string, unknown>;
      }) => {
        const account = resolveDmChannelAccountFromConfig(cfg, pluginConfig, accountId);
        const response = await fetchBridgeWithConfig(account, account, "/send_message", {
          method: "POST",
          body: JSON.stringify({ peer: stripDmTargetPrefix(to), text, reply_to: null }),
        });
        if (!response.ok) {
          throw new Error(formatBridgeError(response));
        }
        const messageId = (response.data as { message_id?: unknown } | undefined)?.message_id;
        return { ok: true, channel: CHANNEL_ID, messageId };
      },
    },
    gateway: {
      startAccount: async (ctx: {
        account: ChannelAccountConfig;
        cfg: Record<string, unknown>;
        runtime: NonNullable<PluginApi["runtime"]>;
        abortSignal?: AbortSignal;
        setStatus?: (value: unknown) => void;
      }) => {
        const validationErrors = validateStrictDmAccountConfig(ctx.cfg, ctx.account);
        if (validationErrors.length > 0) {
          throw new Error(`telegram-user-bridge configuration error:\n- ${validationErrors.join("\n- ")}`);
        }
        ctx.setStatus?.({ accountId: ctx.account.accountId, mode: "polling" });
        return startDmChannelMonitor({
          api,
          cfg: ctx.cfg,
          account: ctx.account,
          runtime: ctx.runtime,
          abortSignal: ctx.abortSignal,
        });
      },
    },
  };

  api.registerChannel({ plugin });
}

function registerProfileTools(api: PluginApi, profile: ProfileConfig): void {
  const slug = slugifyToolId(profile.id);
  const prefix = `telegram_${slug}`;
  const profileLabel = profile.label;
  const optional = { optional: true };
  const isSourcesReadOnly = profile.mode === "sources_ro";

  if (!isSourcesReadOnly) {
    api.registerTool(
      {
        name: `${prefix}_send_message`,
        description:
          `Send a text message from the live Telegram user account using the "${profileLabel}" context. ` +
          "Use only when the user explicitly asks. Peer can be username (e.g. @durov), chat id, or 'me'. " +
          "Writing is blocked by default unless this context is explicitly allowed to write.",
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], {
            description: "Username (@name), chat id, or 'me'",
          }),
          text: Type.String({ minLength: 1, maxLength: 4096, description: "Message text" }),
          reply_to: Type.Optional(Type.Number({ description: "Message id to reply to" })),
        }),
        async execute(_id: string, params: { peer: string | number; text: string; reply_to?: number }) {
          const res = await fetchBridge(api, profile, "/send_message", {
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
  }

  if (isSourcesReadOnly) {
    api.registerTool(
      {
        name: `${prefix}_list_sources`,
        description:
          `List auto-discovered read-only sources for the "${profileLabel}" context. ` +
          "Use this before scheduled summarization jobs or when the user asks which groups/channels are available.",
        parameters: Type.Object({
          refresh: Type.Optional(
            Type.Boolean({
              default: false,
              description: "Force dialog inventory refresh before returning sources",
            })
          ),
        }),
        async execute(_id: string, params: { refresh?: boolean }) {
          const refresh = params.refresh ? "?refresh=true" : "";
          const res = await fetchBridge(api, profile, `/sources${refresh}`);
          if (!res.ok) {
            return toolResult(formatBridgeError(res));
          }
          const sources =
            (res.data as {
              sources?: Array<{ title?: string; type?: string; peer_id?: unknown; username?: string; is_forum?: boolean }>;
            })?.sources ?? [];
          const lines = sources.map((source) => {
            const tags = Array.from(new Set([source.type, source.is_forum ? "forum" : null].filter(Boolean))).join(
              ", "
            );
            return `- ${source.title || source.username || source.peer_id} (id: ${source.peer_id}${
              source.username ? `, @${source.username}` : ""
            }${tags ? `, ${tags}` : ""})`;
          });
          return toolResult(lines.length ? lines.join("\n") : "No sources.");
        },
      },
      optional
    );

    api.registerTool(
      {
        name: `${prefix}_sync_sources`,
        description:
          `Force discovery refresh for the "${profileLabel}" read-only sources. ` +
          "Use after the Telegram account joins a new group/channel and the source list must be updated immediately.",
        parameters: Type.Object({
          limit: Type.Optional(
            Type.Number({
              minimum: 1,
              maximum: 2000,
              default: 500,
              description: "How many recent dialogs to scan during sync",
            })
          ),
        }),
        async execute(_id: string, params: { limit?: number }) {
          const limit = Math.min(2000, Math.max(1, params.limit ?? 500));
          const res = await fetchBridge(api, profile, "/sources/sync", {
            method: "POST",
            body: JSON.stringify({ limit }),
          });
          if (!res.ok) {
            return toolResult(formatBridgeError(res));
          }
          const meta = (res.data as { meta?: { dialog_count?: unknown } } | undefined)?.meta;
          const count = typeof meta?.dialog_count === "number" ? meta.dialog_count : undefined;
          return toolResult(count !== undefined ? `Sources synced (${count} sourceable dialogs).` : "Sources synced.");
        },
      },
      optional
    );
  } else {
    api.registerTool(
      {
        name: `${prefix}_get_dialogs`,
        description:
          `List recent dialogs visible to the "${profileLabel}" context from the live Telegram user account. ` +
          "Use when the user asks to see Telegram chats or find a chat.",
        parameters: Type.Object({
          limit: Type.Optional(
            Type.Number({ minimum: 1, maximum: 50, default: 20, description: "Max number of dialogs" })
          ),
        }),
        async execute(_id: string, params: { limit?: number }) {
          const res = await fetchBridge(
            api,
            profile,
            `/dialogs?limit=${Math.min(50, Math.max(1, params.limit ?? 20))}`
          );
          if (!res.ok) {
            return toolResult(formatBridgeError(res));
          }
          const dialogs =
            (res.data as { dialogs?: Array<{ id: unknown; title: string; username?: string }> })?.dialogs ?? [];
          const lines = dialogs.map(
            (dialog) =>
              `- ${dialog.title || dialog.username || dialog.id} (id: ${dialog.id}${
                dialog.username ? `, @${dialog.username}` : ""
              })`
          );
          return toolResult(lines.length ? lines.join("\n") : "No dialogs.");
        },
      },
      optional
    );
  }

  api.registerTool(
    {
      name: `${prefix}_get_messages`,
      description:
        isSourcesReadOnly
          ? `Get recent messages from an auto-discovered read-only Telegram source using the "${profileLabel}" context. ` +
            "Use this in scheduled polling jobs. Prefer min_id to fetch only new messages and save tokens."
          : `Get recent messages from a Telegram chat using the "${profileLabel}" context. ` +
            "Use when the user explicitly asks to read Telegram messages or when a scheduled workflow polls new messages. " +
            "Use min_id to fetch only newer messages and reduce token usage.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Username, chat id, or 'me'" }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 50, default: 20 })),
        min_id: Type.Optional(Type.Number({ minimum: 1, description: "Only return messages newer than this id" })),
      }),
      async execute(_id: string, params: { peer: string | number; limit?: number; min_id?: number }) {
        const limit = Math.min(50, Math.max(1, params.limit ?? 20));
        const peer = encodeURIComponent(String(params.peer));
        const minId = typeof params.min_id === "number" ? `&min_id=${Math.max(1, params.min_id)}` : "";
        const res = await fetchBridge(api, profile, `/messages?peer=${peer}&limit=${limit}${minId}`);
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const messages =
          (res.data as {
            messages?: Array<{
              id: unknown;
              text: string;
              date?: string;
              out?: boolean;
              sender_name?: string;
              sender_id?: unknown;
              topic_id?: unknown;
            }>;
          })?.messages ?? [];
        const lines = messages.map((message) => {
          const parts = [
            `[${message.out ? "out" : "in"}]`,
            message.sender_name ? `${message.sender_name}` : message.sender_id ? `sender:${message.sender_id}` : null,
            message.topic_id ? `topic:${message.topic_id}` : null,
            message.date ? `${message.date}` : null,
          ].filter(Boolean);
          return `${parts.join(" | ")}${parts.length ? " | " : ""}${message.text || "(no text)"}`;
        });
        return toolResult(lines.length ? lines.join("\n") : "No messages.");
      },
    },
    optional
  );
}

export const __test = {
  ackInboundDmEvent,
  collectRelevantDirectBindings,
  normalizePeerKey,
  resolveConfiguredDmBinding,
  validateStrictDmAccountConfig,
};

export default function register(api: PluginApi) {
  const config = getConfig(api);
  registerDmChannel(api);
  for (const profile of config.profiles) {
    registerProfileTools(api, profile);
  }
}
