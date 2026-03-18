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
  privilegedTools: boolean;
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
  markReadOnInbound: boolean;
  typingWhileReplying: boolean;
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
  needsReauth?: boolean;
};

type DmInboxEvent = {
  id: number;
  text: string;
  sender_id: string | number;
  sender_name?: string;
  sender_username?: string;
  date?: string;
  has_media?: boolean;
  media_type?: string | null;
  file_name?: string | null;
  mime_type?: string | null;
  file_size?: number | null;
  media_path?: string | null;
  media_paths?: string[] | null;
  latitude?: number | null;
  longitude?: number | null;
  venue_title?: string | null;
  venue_address?: string | null;
  venue_provider?: string | null;
  venue_id?: string | null;
  contact_phone?: string | null;
  contact_first_name?: string | null;
  contact_last_name?: string | null;
  contact_user_id?: string | number | null;
  contact_vcard?: string | null;
  entities?: Array<{ type?: string; text?: string; url?: string }>;
  can_transcribe?: boolean | null;
  reply_to_message_id?: number | null;
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

function resolveOwnerCompatAliasSlug(profile: ProfileConfig, existingSlugs: Set<string>): string | null {
  if (!isOwnerProfile(profile)) return null;
  const primary = slugifyToolId(profile.id);
  if (primary === "owner" && !existingSlugs.has("owner_dm")) return "owner_dm";
  if (primary === "owner_dm" && !existingSlugs.has("owner")) return "owner";
  return null;
}

function collectProfileToolPrefixes(profiles: ProfileConfig[]): string[] {
  const slugs = new Set(profiles.map((profile) => slugifyToolId(profile.id)));
  const prefixes = new Set<string>();
  for (const profile of profiles) {
    prefixes.add(`telegram_${slugifyToolId(profile.id)}`);
    const ownerCompatAlias = resolveOwnerCompatAliasSlug(profile, slugs);
    if (ownerCompatAlias) {
      prefixes.add(`telegram_${ownerCompatAlias}`);
    }
  }
  return Array.from(prefixes);
}

function extractRuntimeType(runtime: unknown): string | null {
  if (!runtime || typeof runtime !== "object") return null;
  const value = runtime as Record<string, unknown>;
  if (typeof value.type === "string" && value.type.trim()) {
    return value.type.trim().toLowerCase();
  }
  if (value.acp && typeof value.acp === "object") {
    return "acp";
  }
  return null;
}

function resolveConfiguredAgentRuntimeType(cfg: Record<string, unknown>, agentId: string): string | null {
  const agents = cfg.agents as Record<string, unknown> | undefined;
  const defaults = (agents?.defaults as Record<string, unknown> | undefined)?.runtime;
  const list = (agents?.list as Array<unknown> | undefined) ?? [];
  const agent = list.find(
    (item) => item && typeof item === "object" && typeof (item as Record<string, unknown>).id === "string" &&
      ((item as Record<string, unknown>).id as string).trim() === agentId
  ) as Record<string, unknown> | undefined;
  const explicit = extractRuntimeType(agent?.runtime);
  if (explicit) return explicit;
  return extractRuntimeType(defaults);
}

function isOwnerProfile(profile: ProfileConfig): boolean {
  return /^owner(?:$|[_-])/i.test(profile.id.trim());
}

function normalizeProfile(raw: Record<string, unknown>, fallbackId: string): ProfileConfig | null {
  const id = typeof raw.id === "string" && raw.id.trim() ? raw.id.trim() : fallbackId;
  if (raw.enabled === false) return null;
  const mode = raw.mode === "sources_ro" ? "sources_ro" : "interactive";
  return {
    id,
    label: typeof raw.label === "string" && raw.label.trim() ? raw.label.trim() : id,
    mode,
    privilegedTools: raw.privilegedTools === true,
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
      privilegedTools: true,
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
    markReadOnInbound: raw.markReadOnInbound !== false,
    typingWhileReplying: raw.typingWhileReplying !== false,
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
      const needsReauth =
        res.status === 503 &&
        typeof data === "object" &&
        data !== null &&
        "needs_reauth" in data &&
        (data as { needs_reauth: unknown }).needs_reauth === true;
      return {
        ok: false,
        error: detail || `HTTP ${res.status}`,
        status: res.status,
        retryAfter: Number.isFinite(retryAfter) ? retryAfter : undefined,
        needsReauth: needsReauth || undefined,
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
  const detail = (res.error || "").trim();
  if (res.status === 401) {
    return "Telegram bridge rejected plugin credentials. Check apiToken.";
  }
  if (res.status === 429) {
    if (res.retryAfter) {
      if (detail && /retry after/i.test(detail)) {
        return detail;
      }
      return detail ? `${detail} Retry after ${res.retryAfter}s.` : `Telegram rate limit hit. Retry after ${res.retryAfter}s.`;
    }
    return detail || "Telegram rate limit hit. Try again later.";
  }
  if (res.status === 403 || res.status === 400) {
    return detail || "Telegram request was rejected.";
  }
  if (res.status === 504) {
    return "Telegram API did not respond in time. Retry carefully.";
  }
  if (res.status === 502) {
    if (detail && detail !== "Request failed" && detail !== "Request timed out") {
      return detail;
    }
    return BRIDGE_UNAVAILABLE;
  }
  if (res.status === 503) {
    if (res.needsReauth) {
      return "Telegram session was revoked. Use the telegram-user-bridge-setup skill to re-authenticate via QR code (POST /auth/qr/start).";
    }
    return BRIDGE_UNAVAILABLE;
  }
  if (detail === "Request failed" || detail === "Request timed out") {
    return BRIDGE_UNAVAILABLE;
  }
  return detail || BRIDGE_UNAVAILABLE;
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

type ChannelRuntimeCore = NonNullable<NonNullable<PluginApi["runtime"]>["channel"]>;

type GatewayStartContext = {
  account: ChannelAccountConfig;
  accountId?: string;
  cfg: Record<string, unknown>;
  runtime?: PluginApi["runtime"];
  channelRuntime?: ChannelRuntimeCore;
  abortSignal?: AbortSignal;
  getStatus?: () => unknown;
  setStatus?: (value: unknown) => void;
};

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

function isValidDmInboxEvent(event: unknown): event is DmInboxEvent {
  if (!event || typeof event !== "object") return false;
  const candidate = event as { id?: unknown; sender_id?: unknown };
  if (!Number.isInteger(candidate.id) || Number(candidate.id) <= 0) return false;
  const sender = candidate.sender_id;
  if (typeof sender !== "string" && typeof sender !== "number") return false;
  if (typeof sender === "number" && !Number.isFinite(sender)) return false;
  return normalizePeerKey(sender) !== null;
}

function collectInboundMediaPaths(event: DmInboxEvent): string[] {
  const rawPaths = [
    typeof event.media_path === "string" ? event.media_path.trim() : "",
    ...(Array.isArray(event.media_paths) ? event.media_paths.map((item) => String(item).trim()) : []),
  ].filter((item): item is string => Boolean(item));
  const deduped: string[] = [];
  const seen = new Set<string>();
  for (const path of rawPaths) {
    if (seen.has(path)) continue;
    seen.add(path);
    deduped.push(path);
  }
  return deduped;
}

function sanitizeInboundDmUserText(text: string): string {
  return text.replace(/\[Telegram\s/gi, "[TG ");
}

function sanitizeInboundHintValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const raw = String(value).trim();
  if (!raw) return null;
  const sanitized = raw
    .replace(/[\r\n]+/g, " ")
    .replace(/\|+/g, " / ")
    .replace(/\[/g, "(")
    .replace(/\]/g, ")")
    .replace(/\s+/g, " ")
    .trim();
  return sanitized || null;
}

function buildInboundDmBody(event: DmInboxEvent): string {
  const text = sanitizeInboundDmUserText(event.text?.trim() || "");
  const mediaPaths = collectInboundMediaPaths(event);
  const hasMedia =
    event.has_media === true || Boolean(event.media_type) || Boolean(event.file_name) || mediaPaths.length > 0;
  const hints: string[] = [];
  if (typeof event.reply_to_message_id === "number" && event.reply_to_message_id > 0) {
    hints.push(`[Reply to message | id:${event.reply_to_message_id}]`);
  }
  if (hasMedia) {
    const mediaType = sanitizeInboundHintValue(event.media_type ?? "unknown") ?? "unknown";
    const fileName = sanitizeInboundHintValue(event.file_name);
    const mimeType = sanitizeInboundHintValue(event.mime_type);
    const mediaParts = [
      `type:${mediaType}`,
      fileName ? `file:${fileName}` : null,
      mimeType ? `mime:${mimeType}` : null,
      typeof event.file_size === "number" && Number.isFinite(event.file_size) ? `size:${event.file_size}` : null,
    ].filter(Boolean);
    hints.push(`[Telegram media attached${mediaParts.length ? ` | ${mediaParts.join(" | ")}` : ""}]`);
    if (mediaPaths.length > 0) {
      const preview = mediaPaths
        .slice(0, 3)
        .map((path) => sanitizeInboundHintValue(path))
        .filter((value): value is string => Boolean(value))
        .join(", ");
      const moreSuffix = mediaPaths.length > 3 ? " | more:true" : "";
      if (preview) {
        hints.push(`[Telegram media files | paths:${preview}${moreSuffix}]`);
      }
    }
    if (event.can_transcribe === true) {
      hints.push(`[Telegram transcription available | use: transcribe_voice | id:${event.id}]`);
    }
  }
  const hasLatitude = typeof event.latitude === "number" && Number.isFinite(event.latitude);
  const hasLongitude = typeof event.longitude === "number" && Number.isFinite(event.longitude);
  const hasGeo = hasLatitude && hasLongitude;
  const hasVenue =
    Boolean(event.venue_title) || Boolean(event.venue_address) || Boolean(event.venue_provider) || Boolean(event.venue_id);
  if (hasGeo || hasVenue) {
    const venueTitle = sanitizeInboundHintValue(event.venue_title);
    const venueAddress = sanitizeInboundHintValue(event.venue_address);
    const venueProvider = sanitizeInboundHintValue(event.venue_provider);
    const venueId = sanitizeInboundHintValue(event.venue_id);
    const geoParts = [
      hasGeo ? `geo:${event.latitude},${event.longitude}` : null,
      venueTitle ? `venue:${venueTitle}` : null,
      venueAddress ? `address:${venueAddress}` : null,
      venueProvider ? `provider:${venueProvider}` : null,
      venueId ? `venue_id:${venueId}` : null,
    ].filter(Boolean);
    hints.push(`[Telegram location${geoParts.length ? ` | ${geoParts.join(" | ")}` : ""}]`);
  }
  const hasContact =
    Boolean(event.contact_phone) ||
    Boolean(event.contact_first_name) ||
    Boolean(event.contact_last_name) ||
    (event.contact_user_id !== null && event.contact_user_id !== undefined);
  if (hasContact) {
    const contactPhone = sanitizeInboundHintValue(event.contact_phone);
    const contactFirstName = sanitizeInboundHintValue(event.contact_first_name);
    const contactLastName = sanitizeInboundHintValue(event.contact_last_name);
    const contactName = [contactFirstName, contactLastName].filter((value): value is string => Boolean(value)).join(" ");
    const contactParts = [
      contactPhone ? `phone:${contactPhone}` : null,
      contactName ? `name:${contactName}` : null,
      event.contact_user_id !== null && event.contact_user_id !== undefined ? `user_id:${event.contact_user_id}` : null,
      event.contact_vcard ? "vcard:yes" : null,
    ].filter(Boolean);
    hints.push(`[Telegram contact${contactParts.length ? ` | ${contactParts.join(" | ")}` : ""}]`);
  }
  const entitySamples = Array.isArray(event.entities) ? event.entities : [];
  if (entitySamples.length > 0) {
    const summarized = entitySamples
      .slice(0, 3)
      .map((entity) => entity?.type || "entity")
      .filter((item): item is string => Boolean(item));
    if (summarized.length > 0) {
      const suffix = entitySamples.length > 3 ? " | more:true" : "";
      hints.push(`[Telegram entities | ${summarized.join(",")}${suffix}]`);
    }
  }
  const content = text || "[Non-text Telegram message]";
  if (hints.length === 0) {
    return content;
  }
  return `${content}\n\n${hints.join("\n")}`;
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

function validateStrictDmAccountConfig(
  cfg: Record<string, unknown>,
  account: ChannelAccountConfig,
  expectedToolPrefixes: string[]
): string[] {
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

  // Strict DM routing assumes dedicated agents; if an explicit allowlist exists but excludes
  // telegram_* tools, OpenClaw falls back to core tools in-session, which breaks owner actions.
  const configuredAgents = ((cfg.agents as Record<string, unknown> | undefined)?.list ?? []) as Array<unknown>;
  const globalToolsConfig = (cfg.tools as Record<string, unknown> | undefined) ?? undefined;
  const globalToolsProfile =
    typeof globalToolsConfig?.profile === "string" && globalToolsConfig.profile.trim()
      ? globalToolsConfig.profile.trim()
      : null;
  const byAgentId = new Map<string, Record<string, unknown>>();
  for (const rawAgent of configuredAgents) {
    if (!rawAgent || typeof rawAgent !== "object") continue;
    const agent = rawAgent as Record<string, unknown>;
    const agentId = typeof agent.id === "string" ? agent.id.trim() : "";
    if (!agentId) continue;
    byAgentId.set(agentId, agent);
  }

  const uniqueBoundAgents = new Set(Array.from(bindingByPeer.values()));
  for (const agentId of uniqueBoundAgents) {
    const runtimeType = resolveConfiguredAgentRuntimeType(cfg, agentId);
    if (runtimeType === "acp") {
      errors.push(
        `account "${account.accountId}": agent "${agentId}" uses runtime.type=acp; this runtime may expose only ACP developer tools and hide plugin tools in model schema`
      );
      continue;
    }
    const agent = byAgentId.get(agentId);
    if (!agent) continue;
    const tools = (agent.tools as Record<string, unknown> | undefined) ?? undefined;
    if (!tools || !Object.prototype.hasOwnProperty.call(tools, "allow")) continue;
    const allowRaw = tools.allow;
    if (!Array.isArray(allowRaw)) continue;
    const allow = allowRaw
      .filter((value): value is string => typeof value === "string" && value.trim().length > 0)
      .map((value) => value.trim());
    const alsoAllowRaw = tools.alsoAllow;
    const alsoAllow = Array.isArray(alsoAllowRaw)
      ? alsoAllowRaw
          .filter((value): value is string => typeof value === "string" && value.trim().length > 0)
          .map((value) => value.trim())
      : [];
    const matchesTelegramPrefix = (toolName: string): boolean =>
      expectedToolPrefixes.some((prefix) => toolName === prefix || toolName.startsWith(`${prefix}_`))
    const hasTelegramInAllow = allow.some((toolName) => matchesTelegramPrefix(toolName));
    const hasTelegramInAlsoAllow = alsoAllow.some((toolName) => matchesTelegramPrefix(toolName));
    const hasNonTelegramInAllow = allow.some((toolName) => !matchesTelegramPrefix(toolName));
    const activeToolsProfile =
      (typeof tools.profile === "string" && tools.profile.trim() ? tools.profile.trim() : null) ?? globalToolsProfile;

    if (activeToolsProfile && hasTelegramInAllow && !hasNonTelegramInAllow && !hasTelegramInAlsoAllow) {
      errors.push(
        `account "${account.accountId}": agent "${agentId}" uses tools.profile="${activeToolsProfile}" and plugin-only tools.allow; use tools.alsoAllow for telegram_* tools to avoid policy stripping`
      );
      continue;
    }

    const hasTelegramTool = hasTelegramInAllow || hasTelegramInAlsoAllow;
    if (hasTelegramTool) continue;
    const expected = expectedToolPrefixes
      .slice(0, 3)
      .map((prefix) => `${prefix}_...`)
      .join(", ");
    errors.push(
      `account "${account.accountId}": agent "${agentId}" tools.allow excludes telegram-user-bridge tools (${expected}); inbound DM sessions will expose only core tools`
    );
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

function nextPollBackoffMs(currentMs: number, baseMs: number, maxMs = 30000): number {
  const normalizedBaseMs = Math.max(250, baseMs);
  const normalizedCurrentMs = Math.max(normalizedBaseMs, currentMs);
  return Math.min(maxMs, normalizedCurrentMs * 2);
}

function resolvePollFailureDelayMs(response: BridgeResponse, currentMs: number, baseMs: number): number {
  const fallbackDelayMs = Math.max(Math.max(250, baseMs), currentMs);
  if (typeof response.retryAfter !== "number" || !Number.isFinite(response.retryAfter) || response.retryAfter <= 0) {
    return fallbackDelayMs;
  }
  return Math.max(fallbackDelayMs, Math.ceil(response.retryAfter * 1000));
}

function resolveChannelRuntime(ctx: GatewayStartContext): ChannelRuntimeCore {
  const runtime = ctx.channelRuntime ?? ctx.runtime?.channel;
  if (!runtime) {
    throw new Error("OpenClaw channel runtime is not available.");
  }
  return runtime;
}

function updateGatewayStatus(ctx: GatewayStartContext, patch: Record<string, unknown>): void {
  if (typeof ctx.setStatus !== "function") {
    return;
  }
  const current = typeof ctx.getStatus === "function" ? ctx.getStatus() : undefined;
  const currentStatus =
    current && typeof current === "object" && !Array.isArray(current) ? (current as Record<string, unknown>) : {};
  ctx.setStatus({
    ...currentStatus,
    accountId: ctx.account.accountId,
    ...patch,
  });
}

function buildDmTarget(senderId: string | number): string {
  return `${CHANNEL_ID}:${String(senderId)}`;
}

function stripDmTargetPrefix(target: unknown): string {
  if (typeof target !== "string") {
    return "";
  }
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

async function markInboundDmRead(account: ChannelAccountConfig, event: DmInboxEvent): Promise<void> {
  const response = await fetchBridgeWithConfig(account, account, "/dm/read", {
    method: "POST",
    body: JSON.stringify({
      sender_id: event.sender_id,
      sender_username: event.sender_username ?? null,
      message_id: event.id,
    }),
  });
  if (!response.ok) {
    throw new Error(formatBridgeError(response));
  }
}

async function sendInboundDmTyping(account: ChannelAccountConfig, event: DmInboxEvent): Promise<void> {
  const response = await fetchBridgeWithConfig(account, account, "/dm/typing", {
    method: "POST",
    body: JSON.stringify({
      sender_id: event.sender_id,
      sender_username: event.sender_username ?? null,
    }),
  });
  if (!response.ok) {
    throw new Error(formatBridgeError(response));
  }
}

async function startInboundDmTypingLoop(params: {
  account: ChannelAccountConfig;
  event: DmInboxEvent;
  logger?: { warn: (message: string) => void };
}): Promise<{ stop: () => Promise<void> }> {
  let stopped = false;
  let inFlight: Promise<void> | null = null;
  const tick = async () => {
    if (stopped || inFlight) {
      return;
    }
    inFlight = sendInboundDmTyping(params.account, params.event)
      .catch((error) => {
        params.logger?.warn(`telegram-user-bridge DM typing failed: ${String(error)}`);
      })
      .finally(() => {
        inFlight = null;
      });
    await inFlight;
  };

  await tick();
  const timer = setInterval(() => {
    void tick();
  }, 4000);

  return {
    stop: async () => {
      stopped = true;
      clearInterval(timer);
      if (inFlight) {
        await inFlight;
      }
    },
  };
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
  channelRuntime: ChannelRuntimeCore;
}): Promise<"processed" | "skipped"> {
  const core = params.channelRuntime;
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
  const rawBody = buildInboundDmBody(params.event);
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
  const mediaPaths = collectInboundMediaPaths(params.event);
  const primaryMediaPath = mediaPaths[0];
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
    HasMedia: params.event.has_media === true || mediaPaths.length > 0 || undefined,
    MediaType: params.event.media_type ?? undefined,
    MediaTypes: params.event.media_type ? [params.event.media_type] : undefined,
    MediaFileName: params.event.file_name ?? undefined,
    MediaMimeType: params.event.mime_type ?? undefined,
    MediaFileSize: params.event.file_size ?? undefined,
    MediaPath: primaryMediaPath,
    MediaPaths: mediaPaths.length > 0 ? mediaPaths : undefined,
    Latitude:
      typeof params.event.latitude === "number" && Number.isFinite(params.event.latitude)
        ? params.event.latitude
        : undefined,
    Longitude:
      typeof params.event.longitude === "number" && Number.isFinite(params.event.longitude)
        ? params.event.longitude
        : undefined,
    VenueTitle: params.event.venue_title ?? undefined,
    VenueAddress: params.event.venue_address ?? undefined,
    VenueProvider: params.event.venue_provider ?? undefined,
    VenueId: params.event.venue_id ?? undefined,
    ContactPhone: params.event.contact_phone ?? undefined,
    ContactFirstName: params.event.contact_first_name ?? undefined,
    ContactLastName: params.event.contact_last_name ?? undefined,
    ContactUserId:
      params.event.contact_user_id !== null && params.event.contact_user_id !== undefined
        ? String(params.event.contact_user_id)
        : undefined,
    ContactVCard: params.event.contact_vcard ?? undefined,
    MessageEntities: Array.isArray(params.event.entities) ? params.event.entities : undefined,
    CanTranscribe: params.event.can_transcribe === true ? true : undefined,
    CommandAuthorized: true,
    Provider: CHANNEL_ID,
    Surface: CHANNEL_ID,
    MessageSid: String(params.event.id),
    ReplyToMessageId:
      typeof params.event.reply_to_message_id === "number" && params.event.reply_to_message_id > 0
        ? params.event.reply_to_message_id
        : undefined,
    OriginatingChannel: CHANNEL_ID,
    OriginatingTo: normalizedTarget,
  });
  await (core.session.recordInboundSession as (...args: unknown[]) => Promise<void>)({
    storePath,
    sessionKey,
    ctx: ctxPayload,
    createIfMissing: true,
    updateLastRoute: {
      sessionKey,
      channel: CHANNEL_ID,
      to: normalizedTarget,
      accountId: route.accountId,
    },
    onRecordError: (error: unknown) => {
      params.api.logger?.warn(`telegram-user-bridge session meta update failed: ${String(error)}`);
    },
  });
  let typingHandle: { stop: () => Promise<void> } | null = null;
  try {
    if (params.account.markReadOnInbound) {
      try {
        await markInboundDmRead(params.account, params.event);
      } catch (error) {
        params.api.logger?.warn(`telegram-user-bridge DM read receipt failed: ${String(error)}`);
      }
    }
    if (params.account.typingWhileReplying) {
      typingHandle = await startInboundDmTypingLoop({
        account: params.account,
        event: params.event,
        logger: params.api.logger,
      });
    }
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
  } finally {
    await typingHandle?.stop();
  }
  return "processed";
}

async function startDmChannelMonitor(params: {
  api: PluginApi;
  cfg: Record<string, unknown>;
  account: ChannelAccountConfig;
  channelRuntime: ChannelRuntimeCore;
  abortSignal?: AbortSignal;
  statusContext?: GatewayStartContext;
}): Promise<{ stop: () => Promise<void> }> {
  let stopped = false;
  const basePollDelayMs = Math.max(250, params.account.pollIntervalMs);
  let failureDelayMs = basePollDelayMs;
  const loop = (async () => {
    while (!stopped && !params.abortSignal?.aborted) {
      const response = await fetchBridgeWithConfig(
        params.account,
        params.account,
        `/dm/inbox/poll?timeout_ms=${Math.max(1000, params.account.pollTimeoutMs)}&limit=10`
      );
      if (!response.ok) {
        const retryDelayMs = resolvePollFailureDelayMs(response, failureDelayMs, basePollDelayMs);
        updateGatewayStatus(params.statusContext ?? { account: params.account, cfg: params.cfg }, {
          mode: "degraded",
          connected: false,
          lastError: formatBridgeError(response),
          retryInMs: retryDelayMs,
        });
        params.api.logger?.warn(`telegram-user-bridge DM poll failed: ${formatBridgeError(response)}`);
        await sleepWithAbort(retryDelayMs, params.abortSignal);
        failureDelayMs = nextPollBackoffMs(retryDelayMs, basePollDelayMs);
        continue;
      }
      failureDelayMs = basePollDelayMs;
      updateGatewayStatus(params.statusContext ?? { account: params.account, cfg: params.cfg }, {
        mode: "polling",
        connected: true,
        lastError: null,
        retryInMs: 0,
      });
      const rawEvents = (response.data as { events?: unknown[] } | undefined)?.events ?? [];
      const events = rawEvents.filter(isValidDmInboxEvent);
      if (events.length !== rawEvents.length) {
        params.api.logger?.warn("telegram-user-bridge DM poll returned malformed events; skipping invalid entries");
      }
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
            channelRuntime: params.channelRuntime,
          });
          if (result === "processed" || result === "skipped") {
            await ackInboundDmEvent(params.account, event);
          }
        } catch (error) {
          updateGatewayStatus(params.statusContext ?? { account: params.account, cfg: params.cfg }, {
            mode: "degraded",
            connected: false,
            lastError: String(error),
            retryInMs: failureDelayMs,
          });
          params.api.logger?.warn(`telegram-user-bridge DM event failed: ${String(error)}`);
          await sleepWithAbort(failureDelayMs, params.abortSignal);
          failureDelayMs = nextPollBackoffMs(failureDelayMs, basePollDelayMs);
          break;
        }
      }
    }
  })();

  return {
    stop: async () => {
      stopped = true;
      updateGatewayStatus(params.statusContext ?? { account: params.account, cfg: params.cfg }, {
        mode: "stopped",
        connected: false,
      });
      await Promise.race([loop, sleepWithAbort(params.account.timeoutMs)]);
    },
  };
}

function registerDmChannel(api: PluginApi): void {
  if (typeof api.registerChannel !== "function") {
    return;
  }
  const pluginConfig = getConfig(api);
  const runningAccounts = new Map<string, { stop: () => Promise<void> }>();
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
    reload: { configPrefixes: [`channels.${CHANNEL_ID}`, `plugins.entries.${CHANNEL_ID}`] },
    config: {
      listAccountIds: (cfg: Record<string, unknown>) =>
        getDmChannelConfigFromConfig(cfg, pluginConfig)?.accounts.map((account) => account.accountId) ?? [],
      resolveAccount: (cfg: Record<string, unknown>, accountId?: string) =>
        resolveDmChannelAccountFromConfig(cfg, pluginConfig, accountId),
      defaultAccountId: (cfg: Record<string, unknown>) =>
        getDmChannelConfigFromConfig(cfg, pluginConfig)?.defaultAccountId ?? "default",
      inspectAccount: (cfg: Record<string, unknown>, accountId?: string) => {
        const channelConfig = getDmChannelConfigFromConfig(cfg, pluginConfig);
        const account = resolveDmChannelAccountFromConfig(cfg, pluginConfig, accountId);
        return {
          ok: true,
          accountId: account.accountId,
          defaultAccountId: account.defaultAccountId,
          configured: channelConfig !== null,
          enabled: account.enabled,
          label: account.label,
          baseUrl: account.baseUrl,
          timeoutMs: account.timeoutMs,
          pollTimeoutMs: account.pollTimeoutMs,
          pollIntervalMs: account.pollIntervalMs,
          strictPeerBindings: account.strictPeerBindings,
          markReadOnInbound: account.markReadOnInbound,
          typingWhileReplying: account.typingWhileReplying,
          policyProfile: account.policyProfile ?? null,
          allowFrom: account.allowFrom ?? [],
          writeTo: account.writeTo ?? [],
        };
      },
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
        const peer = stripDmTargetPrefix(to);
        if (!peer) {
          throw new Error("telegram-user-bridge outbound target is missing or invalid.");
        }
        const response = await fetchBridgeWithConfig(account, account, "/send_message", {
          method: "POST",
          body: JSON.stringify({ peer, text, reply_to: null }),
        });
        if (!response.ok) {
          throw new Error(formatBridgeError(response));
        }
        const messageId = (response.data as { message_id?: unknown } | undefined)?.message_id;
        return { ok: true, channel: CHANNEL_ID, messageId };
      },
    },
    gateway: {
      startAccount: async (ctx: GatewayStartContext) => {
        const validationErrors = validateStrictDmAccountConfig(
          ctx.cfg,
          ctx.account,
          collectProfileToolPrefixes(pluginConfig.profiles)
        );
        if (validationErrors.length > 0) {
          throw new Error(`telegram-user-bridge configuration error:\n- ${validationErrors.join("\n- ")}`);
        }
        const existing = runningAccounts.get(ctx.account.accountId);
        if (existing) {
          await existing.stop();
          runningAccounts.delete(ctx.account.accountId);
        }
        updateGatewayStatus(ctx, { mode: "starting", connected: false, lastError: null, retryInMs: 0 });
        const handle = await startDmChannelMonitor({
          api,
          cfg: ctx.cfg,
          account: ctx.account,
          channelRuntime: resolveChannelRuntime(ctx),
          abortSignal: ctx.abortSignal,
          statusContext: ctx,
        });
        runningAccounts.set(ctx.account.accountId, handle);
        return {
          stop: async () => {
            const current = runningAccounts.get(ctx.account.accountId);
            if (current) {
              runningAccounts.delete(ctx.account.accountId);
              await current.stop();
            }
          },
        };
      },
      stopAccount: async (ctx: GatewayStartContext) => {
        const current = runningAccounts.get(ctx.account.accountId);
        if (!current) {
          updateGatewayStatus(ctx, { mode: "stopped", connected: false });
          return;
        }
        runningAccounts.delete(ctx.account.accountId);
        await current.stop();
      },
    },
  };

  api.registerChannel({ plugin });
}

function registerProfileTools(api: PluginApi, profile: ProfileConfig, prefixOverride?: string): void {
  const prefix = prefixOverride ?? `telegram_${slugifyToolId(profile.id)}`;
  const profileLabel = profile.label;
  const optional = { optional: true };
  const isSourcesReadOnly = profile.mode === "sources_ro";
  const allowPrivilegedTools = !isSourcesReadOnly && profile.privilegedTools === true;
  const allowOwnerJoinTools = allowPrivilegedTools && isOwnerProfile(profile);
  const allowOwnerFolderTools = allowPrivilegedTools && isOwnerProfile(profile);

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
          text: Type.String({ minLength: 1, description: "Message text. Messages longer than 4096 chars are auto-split at logical boundaries (paragraph → sentence → word) up to 20 parts (~82K chars total). Larger texts must be trimmed or chunked manually before calling this tool." }),
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

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_send_file`,
          description:
            `Send a local file from the backend host to Telegram using the "${profileLabel}" context. ` +
            "Use only when the user explicitly asks. Writing is blocked by default unless this context is allowed to write.",
          parameters: Type.Object({
            peer: Type.Union([Type.String(), Type.Number()], {
              description: "Username (@name), chat id, or 'me'",
            }),
            file_path: Type.String({ minLength: 1, description: "Absolute or backend-local file path" }),
            caption: Type.Optional(Type.String({ description: "Optional caption" })),
            reply_to: Type.Optional(Type.Number({ description: "Message id to reply to" })),
          }),
          async execute(
            _id: string,
            params: { peer: string | number; file_path: string; caption?: string; reply_to?: number }
          ) {
            const res = await fetchBridge(api, profile, "/send_file", {
              method: "POST",
              body: JSON.stringify({
                peer: params.peer,
                file_path: params.file_path,
                caption: params.caption ?? null,
                reply_to: params.reply_to ?? null,
              }),
            });
            if (!res.ok) {
              return toolResult(formatBridgeError(res));
            }
            const messageId = (res.data as { message_id?: unknown } | undefined)?.message_id;
            return toolResult(messageId ? `File sent (id: ${messageId}).` : "File sent.");
          },
        },
        optional
      );
    }

    api.registerTool(
      {
        name: `${prefix}_send_location`,
        description:
          `Send a geolocation pin via Telegram using the "${profileLabel}" context. ` +
          "Use only when the user explicitly asks. Writing is blocked by default unless this context is allowed to write.",
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], {
            description: "Username (@name), chat id, or 'me'",
          }),
          latitude: Type.Number({ minimum: -90, maximum: 90 }),
          longitude: Type.Number({ minimum: -180, maximum: 180 }),
        }),
        async execute(_id: string, params: { peer: string | number; latitude: number; longitude: number }) {
          const res = await fetchBridge(api, profile, "/send_location", {
            method: "POST",
            body: JSON.stringify(params),
          });
          if (!res.ok) {
            return toolResult(formatBridgeError(res));
          }
          const messageId = (res.data as { message_id?: unknown } | undefined)?.message_id;
          return toolResult(messageId ? `Location sent (id: ${messageId}).` : "Location sent.");
        },
      },
      optional
    );

    api.registerTool(
      {
        name: `${prefix}_edit_message`,
        description:
          `Edit one of your Telegram messages using the "${profileLabel}" context. ` +
          "Use only when the user explicitly asks.",
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], {
            description: "Username (@name), chat id, or 'me'",
          }),
          message_id: Type.Number({ minimum: 1 }),
          text: Type.String({ minLength: 1 }),
        }),
        async execute(_id: string, params: { peer: string | number; message_id: number; text: string }) {
          const res = await fetchBridge(api, profile, "/edit_message", {
            method: "POST",
            body: JSON.stringify(params),
          });
          if (!res.ok) {
            return toolResult(formatBridgeError(res));
          }
          return toolResult(`Message ${params.message_id} edited.`);
        },
      },
      optional
    );

    api.registerTool(
      {
        name: `${prefix}_delete_message`,
        description:
          `Delete a Telegram message using the "${profileLabel}" context. ` +
          "Use only when the user explicitly asks.",
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], {
            description: "Username (@name), chat id, or 'me'",
          }),
          message_id: Type.Number({ minimum: 1 }),
          revoke: Type.Optional(Type.Boolean({ default: true })),
        }),
        async execute(_id: string, params: { peer: string | number; message_id: number; revoke?: boolean }) {
          const res = await fetchBridge(api, profile, "/delete_message", {
            method: "POST",
            body: JSON.stringify({
              peer: params.peer,
              message_id: params.message_id,
              revoke: params.revoke ?? true,
            }),
          });
          if (!res.ok) {
            return toolResult(formatBridgeError(res));
          }
          return toolResult(`Message ${params.message_id} deleted.`);
        },
      },
      optional
    );

    api.registerTool(
      {
        name: `${prefix}_forward_message`,
        description:
          `Forward a Telegram message between chats using the "${profileLabel}" context. ` +
          "The source must be readable and the destination writable for this profile.",
        parameters: Type.Object({
          from_peer: Type.Union([Type.String(), Type.Number()], { description: "Source username or chat id" }),
          to_peer: Type.Union([Type.String(), Type.Number()], { description: "Destination username or chat id" }),
          message_id: Type.Number({ minimum: 1 }),
        }),
        async execute(_id: string, params: { from_peer: string | number; to_peer: string | number; message_id: number }) {
          const res = await fetchBridge(api, profile, "/forward_message", {
            method: "POST",
            body: JSON.stringify(params),
          });
          if (!res.ok) {
            return toolResult(formatBridgeError(res));
          }
          const messageId = (res.data as { message_id?: unknown } | undefined)?.message_id;
          return toolResult(messageId ? `Message forwarded (id: ${messageId}).` : "Message forwarded.");
        },
      },
      optional
    );

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_leave_chat`,
          description:
            `Leave a Telegram group/channel/dialog using the "${profileLabel}" context. ` +
            "Use only when the user explicitly asks.",
          parameters: Type.Object({
            peer: Type.Union([Type.String(), Type.Number()], { description: "Chat username or chat id" }),
          }),
          async execute(_id: string, params: { peer: string | number }) {
            const res = await fetchBridge(api, profile, "/leave_chat", {
              method: "POST",
              body: JSON.stringify(params),
            });
            if (!res.ok) {
              return toolResult(formatBridgeError(res));
            }
            return toolResult("Chat left.");
          },
        },
        optional
      );
    }

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_send_voice`,
          description: `Send a local voice note file via Telegram using the "${profileLabel}" context.`,
          parameters: Type.Object({
            peer: Type.Union([Type.String(), Type.Number()], { description: "Username (@name), chat id, or 'me'" }),
            file_path: Type.String({ minLength: 1 }),
            caption: Type.Optional(Type.String()),
          }),
          async execute(_id: string, params: { peer: string | number; file_path: string; caption?: string }) {
            const res = await fetchBridge(api, profile, "/send_voice", {
              method: "POST",
              body: JSON.stringify({
                peer: params.peer,
                file_path: params.file_path,
                caption: params.caption ?? null,
              }),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            return toolResult("Voice note sent.");
          },
        },
        optional
      );
    }

    api.registerTool(
      {
        name: `${prefix}_transcribe_voice`,
        description:
          `Transcribe a voice note or video circle using Telegram's built-in speech recognition (Premium feature) ` +
          `via the "${profileLabel}" context. ` +
          "If the account does not have Telegram Premium, returns an error — in that case use download_media to get the audio file and process it with available STT tools.",
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], { description: "Username (@name), chat id, or 'me'" }),
          message_id: Type.Number({ minimum: 1, description: "ID of the voice note or video circle message" }),
        }),
        async execute(_id: string, params: { peer: string | number; message_id: number }) {
          const res = await fetchBridge(api, profile, "/transcribe_voice", {
            method: "POST",
            body: JSON.stringify({ peer: params.peer, message_id: params.message_id }),
          });
          if (!res.ok) return toolResult(formatBridgeError(res));
          const data = res.data as { ok?: boolean; text?: string; error?: string; pending?: boolean } | undefined;
          if (data?.ok === false || data?.error) {
            return toolResult(
              "Transcription unavailable (Telegram Premium required). Use download_media to retrieve the audio file for external STT processing."
            );
          }
          if (data?.pending === true) {
            return toolResult(
              "Transcription is still processing on Telegram's side (long voice message). " +
              "You can retry transcribe_voice in 10-15 seconds, or use download_media to retrieve the audio file for external STT processing."
            );
          }
          const text = typeof data?.text === "string" ? data.text : "";
          return toolResult(text ? `Transcription: ${text}` : "Transcription returned empty text.");
        },
      },
      optional
    );

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_send_sticker`,
          description: `Send a local sticker file via Telegram using the "${profileLabel}" context.`,
          parameters: Type.Object({
            peer: Type.Union([Type.String(), Type.Number()], { description: "Username (@name), chat id, or 'me'" }),
            file_path: Type.String({ minLength: 1 }),
          }),
          async execute(_id: string, params: { peer: string | number; file_path: string }) {
            const res = await fetchBridge(api, profile, "/send_sticker", {
              method: "POST",
              body: JSON.stringify(params),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            return toolResult("Sticker sent.");
          },
        },
        optional
      );
    }

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_add_contact`,
          description: `Add a Telegram contact using the "${profileLabel}" context.`,
          parameters: Type.Object({
            phone: Type.String({ minLength: 1 }),
            first_name: Type.String({ minLength: 1 }),
            last_name: Type.Optional(Type.String()),
          }),
          async execute(_id: string, params: { phone: string; first_name: string; last_name?: string }) {
            const res = await fetchBridge(api, profile, "/contacts/add", {
              method: "POST",
              body: JSON.stringify({
                phone: params.phone,
                first_name: params.first_name,
                last_name: params.last_name ?? null,
              }),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            const contact = (res.data as { contact?: Record<string, unknown> } | undefined)?.contact ?? {};
            return toolResult(`Contact added: ${contact.title || contact.username || contact.id || params.phone}.`);
          },
        },
        optional
      );
    }

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_delete_contact`,
          description: `Delete a Telegram contact using the "${profileLabel}" context.`,
          parameters: Type.Object({
            peer: Type.Union([Type.String(), Type.Number()], { description: "User username or id" }),
          }),
          async execute(_id: string, params: { peer: string | number }) {
            const res = await fetchBridge(api, profile, "/contacts/delete", {
              method: "POST",
              body: JSON.stringify(params),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            return toolResult("Contact deleted.");
          },
        },
        optional
      );
    }

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_block_user`,
          description: `Block a Telegram user using the "${profileLabel}" context.`,
          parameters: Type.Object({
            peer: Type.Union([Type.String(), Type.Number()], { description: "User username or id" }),
          }),
          async execute(_id: string, params: { peer: string | number }) {
            const res = await fetchBridge(api, profile, "/block_user", {
              method: "POST",
              body: JSON.stringify(params),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            return toolResult("User blocked.");
          },
        },
        optional
      );
    }

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_unblock_user`,
          description: `Unblock a Telegram user using the "${profileLabel}" context.`,
          parameters: Type.Object({
            peer: Type.Union([Type.String(), Type.Number()], { description: "User username or id" }),
          }),
          async execute(_id: string, params: { peer: string | number }) {
            const res = await fetchBridge(api, profile, "/unblock_user", {
              method: "POST",
              body: JSON.stringify(params),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            return toolResult("User unblocked.");
          },
        },
        optional
      );
    }

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_create_group`,
          description: `Create a Telegram group using the "${profileLabel}" context.`,
          parameters: Type.Object({
            title: Type.String({ minLength: 1 }),
            users: Type.Array(Type.Union([Type.String(), Type.Number()]), { default: [] }),
          }),
          async execute(_id: string, params: { title: string; users: Array<string | number> }) {
            const res = await fetchBridge(api, profile, "/create_group", {
              method: "POST",
              body: JSON.stringify(params),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            const chatId = (res.data as { chat_id?: unknown } | undefined)?.chat_id;
            return toolResult(chatId ? `Group created (id: ${chatId}).` : "Group created.");
          },
        },
        optional
      );
    }

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_create_channel`,
          description: `Create a Telegram channel or supergroup using the "${profileLabel}" context.`,
          parameters: Type.Object({
            title: Type.String({ minLength: 1 }),
            about: Type.Optional(Type.String()),
            megagroup: Type.Optional(Type.Boolean({ default: false })),
          }),
          async execute(_id: string, params: { title: string; about?: string; megagroup?: boolean }) {
            const res = await fetchBridge(api, profile, "/create_channel", {
              method: "POST",
              body: JSON.stringify({
                title: params.title,
                about: params.about ?? null,
                megagroup: params.megagroup ?? false,
              }),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            const chatId = (res.data as { chat_id?: unknown } | undefined)?.chat_id;
            return toolResult(chatId ? `Channel created (id: ${chatId}).` : "Channel created.");
          },
        },
        optional
      );
    }

    if (allowPrivilegedTools) {
      api.registerTool(
        {
          name: `${prefix}_invite_to_group`,
          description: `Invite users to a Telegram group/channel using the "${profileLabel}" context.`,
          parameters: Type.Object({
            peer: Type.Union([Type.String(), Type.Number()], { description: "Group/channel username or id" }),
            users: Type.Array(Type.Union([Type.String(), Type.Number()]), { minItems: 1 }),
          }),
          async execute(_id: string, params: { peer: string | number; users: Array<string | number> }) {
            const res = await fetchBridge(api, profile, "/invite_to_group", {
              method: "POST",
              body: JSON.stringify(params),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            const count = (res.data as { invited_count?: unknown } | undefined)?.invited_count;
            return toolResult(typeof count === "number" ? `Invited ${count} users.` : "Users invited.");
          },
        },
        optional
      );
    }

    if (allowOwnerJoinTools) {
      api.registerTool(
        {
          name: `${prefix}_join_chat_by_link`,
          description:
            `Join a Telegram channel/supergroup by invite or public link using the "${profileLabel}" context. ` +
            "Supports links like https://t.me/+hash and https://t.me/channel_username.",
          parameters: Type.Object({
            link: Type.String({ minLength: 1 }),
          }),
          async execute(_id: string, params: { link: string }) {
            const res = await fetchBridge(api, profile, "/join_chat_by_link", {
              method: "POST",
              body: JSON.stringify(params),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            const chatId = (res.data as { chat_id?: unknown } | undefined)?.chat_id;
            return toolResult(chatId ? `Joined chat (id: ${chatId}).` : "Joined chat.");
          },
        },
        optional
      );
    }

    if (allowOwnerFolderTools) {
      api.registerTool(
        {
          name: `${prefix}_list_dialog_folders`,
          description:
            `List Telegram dialog folders using the "${profileLabel}" context. ` +
            "Owner-only tool for managing personal channel/chat folders.",
          parameters: Type.Object({}),
          async execute() {
            const res = await fetchBridge(api, profile, "/dialog_folders");
            if (!res.ok) return toolResult(formatBridgeError(res));
            const folders =
              (res.data as {
                folders?: Array<{
                  id?: unknown;
                  title?: string;
                  emoticon?: string;
                  include_peers?: Array<unknown>;
                  exclude_peers?: Array<unknown>;
                  pinned_peers?: Array<unknown>;
                }>;
              } | undefined)?.folders ?? [];
            const lines = folders.map((folder) => {
              const parts = [
                `id:${folder.id ?? "?"}`,
                folder.title ? `title:${folder.title}` : null,
                folder.emoticon ? `emoji:${folder.emoticon}` : null,
                Array.isArray(folder.include_peers) ? `include:${folder.include_peers.length}` : null,
                Array.isArray(folder.exclude_peers) ? `exclude:${folder.exclude_peers.length}` : null,
                Array.isArray(folder.pinned_peers) ? `pinned:${folder.pinned_peers.length}` : null,
              ].filter(Boolean);
              return `- ${parts.join(" | ")}`;
            });
            return toolResult(lines.length ? lines.join("\n") : "No dialog folders.");
          },
        },
        optional
      );
    }

    if (allowOwnerFolderTools) {
      api.registerTool(
        {
          name: `${prefix}_upsert_dialog_folder`,
          description:
            `Create or update one Telegram dialog folder using the "${profileLabel}" context. ` +
            "Owner-only tool for managing personal channel/chat folders.",
          parameters: Type.Object({
            folder_id: Type.Number({ minimum: 2, maximum: 255 }),
            title: Type.String({ minLength: 1, maxLength: 64 }),
            emoticon: Type.Optional(Type.String()),
            contacts: Type.Optional(Type.Boolean({ default: false })),
            non_contacts: Type.Optional(Type.Boolean({ default: false })),
            groups: Type.Optional(Type.Boolean({ default: false })),
            broadcasts: Type.Optional(Type.Boolean({ default: false })),
            bots: Type.Optional(Type.Boolean({ default: false })),
            exclude_muted: Type.Optional(Type.Boolean({ default: false })),
            exclude_read: Type.Optional(Type.Boolean({ default: false })),
            exclude_archived: Type.Optional(Type.Boolean({ default: false })),
            pinned_peers: Type.Optional(Type.Array(Type.Union([Type.String(), Type.Number()]))),
            include_peers: Type.Optional(Type.Array(Type.Union([Type.String(), Type.Number()]))),
            exclude_peers: Type.Optional(Type.Array(Type.Union([Type.String(), Type.Number()]))),
          }),
          async execute(
            _id: string,
            params: {
              folder_id: number;
              title: string;
              emoticon?: string;
              contacts?: boolean;
              non_contacts?: boolean;
              groups?: boolean;
              broadcasts?: boolean;
              bots?: boolean;
              exclude_muted?: boolean;
              exclude_read?: boolean;
              exclude_archived?: boolean;
              pinned_peers?: Array<string | number>;
              include_peers?: Array<string | number>;
              exclude_peers?: Array<string | number>;
            }
          ) {
            const res = await fetchBridge(api, profile, "/dialog_folders/upsert", {
              method: "POST",
              body: JSON.stringify({
                folder_id: Math.max(2, Math.min(255, Math.trunc(params.folder_id))),
                title: params.title,
                emoticon: params.emoticon ?? null,
                contacts: params.contacts ?? false,
                non_contacts: params.non_contacts ?? false,
                groups: params.groups ?? false,
                broadcasts: params.broadcasts ?? false,
                bots: params.bots ?? false,
                exclude_muted: params.exclude_muted ?? false,
                exclude_read: params.exclude_read ?? false,
                exclude_archived: params.exclude_archived ?? false,
                pinned_peers: Array.isArray(params.pinned_peers) ? params.pinned_peers : [],
                include_peers: Array.isArray(params.include_peers) ? params.include_peers : [],
                exclude_peers: Array.isArray(params.exclude_peers) ? params.exclude_peers : [],
              }),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            const folderId = (res.data as { folder_id?: unknown } | undefined)?.folder_id;
            return toolResult(
              typeof folderId === "number"
                ? `Dialog folder ${folderId} updated.`
                : "Dialog folder updated."
            );
          },
        },
        optional
      );
    }

    if (allowOwnerFolderTools) {
      api.registerTool(
        {
          name: `${prefix}_delete_dialog_folder`,
          description:
            `Delete one Telegram dialog folder using the "${profileLabel}" context. ` +
            "Owner-only tool for managing personal channel/chat folders.",
          parameters: Type.Object({
            folder_id: Type.Number({ minimum: 2, maximum: 255 }),
          }),
          async execute(_id: string, params: { folder_id: number }) {
            const res = await fetchBridge(api, profile, "/dialog_folders/delete", {
              method: "POST",
              body: JSON.stringify({
                folder_id: Math.max(2, Math.min(255, Math.trunc(params.folder_id))),
              }),
            });
            if (!res.ok) return toolResult(formatBridgeError(res));
            const folderId = (res.data as { folder_id?: unknown } | undefined)?.folder_id;
            return toolResult(
              typeof folderId === "number"
                ? `Dialog folder ${folderId} deleted.`
                : "Dialog folder deleted."
            );
          },
        },
        optional
      );
    }

    api.registerTool(
      {
        name: `${prefix}_promote_admin`,
        description:
          `Promote a Telegram user to admin in a group, supergroup, or channel using the "${profileLabel}" context.`,
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], {
            description: "Group, supergroup, or channel username or id",
          }),
          user_peer: Type.Union([Type.String(), Type.Number()], { description: "User username or id" }),
          title: Type.Optional(Type.String()),
        }),
        async execute(_id: string, params: { peer: string | number; user_peer: string | number; title?: string }) {
          const res = await fetchBridge(api, profile, "/promote_admin", {
            method: "POST",
            body: JSON.stringify({
              peer: params.peer,
              user_peer: params.user_peer,
              title: params.title ?? null,
            }),
          });
          if (!res.ok) return toolResult(formatBridgeError(res));
          return toolResult("Admin promoted.");
        },
      },
      optional
    );

    api.registerTool(
      {
        name: `${prefix}_demote_admin`,
        description:
          `Demote a Telegram admin in a group, supergroup, or channel using the "${profileLabel}" context.`,
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], {
            description: "Group, supergroup, or channel username or id",
          }),
          user_peer: Type.Union([Type.String(), Type.Number()], { description: "User username or id" }),
        }),
        async execute(_id: string, params: { peer: string | number; user_peer: string | number }) {
          const res = await fetchBridge(api, profile, "/demote_admin", {
            method: "POST",
            body: JSON.stringify(params),
          });
          if (!res.ok) return toolResult(formatBridgeError(res));
          return toolResult("Admin demoted.");
        },
      },
      optional
    );

    api.registerTool(
      {
        name: `${prefix}_ban_user`,
        description:
          `Ban a Telegram user from a supergroup or channel using the "${profileLabel}" context. ` +
          "Basic groups are not supported for this flow.",
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], { description: "Supergroup or channel username or id" }),
          user_peer: Type.Union([Type.String(), Type.Number()], { description: "User username or id" }),
          until_date: Type.Optional(Type.Number({ description: "Optional unix timestamp until which the ban applies" })),
        }),
        async execute(_id: string, params: { peer: string | number; user_peer: string | number; until_date?: number }) {
          const res = await fetchBridge(api, profile, "/ban_user", {
            method: "POST",
            body: JSON.stringify({
              peer: params.peer,
              user_peer: params.user_peer,
              until_date: params.until_date ?? null,
            }),
          });
          if (!res.ok) return toolResult(formatBridgeError(res));
          return toolResult("User banned.");
        },
      },
      optional
    );

    api.registerTool(
      {
        name: `${prefix}_unban_user`,
        description:
          `Unban a Telegram user from a supergroup or channel using the "${profileLabel}" context. ` +
          "Basic groups are not supported for this flow.",
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], { description: "Supergroup or channel username or id" }),
          user_peer: Type.Union([Type.String(), Type.Number()], { description: "User username or id" }),
        }),
        async execute(_id: string, params: { peer: string | number; user_peer: string | number }) {
          const res = await fetchBridge(api, profile, "/unban_user", {
            method: "POST",
            body: JSON.stringify(params),
          });
          if (!res.ok) return toolResult(formatBridgeError(res));
          return toolResult("User unbanned.");
        },
      },
      optional
    );

    api.registerTool(
      {
        name: `${prefix}_send_reaction`,
        description: `Send a Telegram reaction using the "${profileLabel}" context.`,
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], { description: "Chat username or id" }),
          message_id: Type.Number({ minimum: 1 }),
          emoji: Type.String({ minLength: 1 }),
          big: Type.Optional(Type.Boolean({ default: false })),
        }),
        async execute(_id: string, params: { peer: string | number; message_id: number; emoji: string; big?: boolean }) {
          const res = await fetchBridge(api, profile, "/send_reaction", {
            method: "POST",
            body: JSON.stringify({
              peer: params.peer,
              message_id: params.message_id,
              emoji: params.emoji,
              big: params.big ?? false,
            }),
          });
          if (!res.ok) return toolResult(formatBridgeError(res));
          return toolResult("Reaction sent.");
        },
      },
      optional
    );

    api.registerTool(
      {
        name: `${prefix}_remove_reaction`,
        description: `Remove your Telegram reaction using the "${profileLabel}" context.`,
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], { description: "Chat username or id" }),
          message_id: Type.Number({ minimum: 1 }),
        }),
        async execute(_id: string, params: { peer: string | number; message_id: number }) {
          const res = await fetchBridge(api, profile, "/remove_reaction", {
            method: "POST",
            body: JSON.stringify(params),
          });
          if (!res.ok) return toolResult(formatBridgeError(res));
          return toolResult("Reaction removed.");
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
      name: `${prefix}_get_message`,
      description:
        `Get one Telegram message by id using the "${profileLabel}" context. ` +
        "Use this when the user asks about one concrete message or when you need full media/location metadata.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Username, chat id, or 'me'" }),
        message_id: Type.Number({ minimum: 1 }),
      }),
      async execute(_id: string, params: { peer: string | number; message_id: number }) {
        const peer = encodeURIComponent(String(params.peer));
        const res = await fetchBridge(api, profile, `/message?peer=${peer}&message_id=${Math.max(1, params.message_id)}`);
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const message = (res.data as Record<string, unknown> | undefined) ?? {};
        const parts = [
          typeof message.id === "number" ? `id:${message.id}` : null,
          typeof message.sender_name === "string" && message.sender_name ? `${message.sender_name}` : null,
          typeof message.date === "string" && message.date ? message.date : null,
          message.topic_id ? `topic:${message.topic_id}` : null,
          message.reply_to_message_id ? `reply_to:${message.reply_to_message_id}` : null,
          message.has_media ? `media:${message.media_type ?? "yes"}` : null,
          message.latitude && message.longitude ? `geo:${message.latitude},${message.longitude}` : null,
        ].filter(Boolean);
        const body = typeof message.text === "string" && message.text ? message.text : "(no text)";
        return toolResult(`${parts.join(" | ")}${parts.length ? " | " : ""}${body}`);
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_search_messages`,
      description:
        `Search Telegram messages in one chat using the "${profileLabel}" context. ` +
        "Use this when the user asks to find mentions, files, links, or past discussions by text.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Username, chat id, or 'me'" }),
        query: Type.String({ minLength: 1 }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 50, default: 20 })),
        from_user: Type.Optional(Type.Union([Type.String(), Type.Number()], { description: "Optional sender filter" })),
      }),
      async execute(
        _id: string,
        params: { peer: string | number; query: string; limit?: number; from_user?: string | number }
      ) {
        const res = await fetchBridge(api, profile, "/search_messages", {
          method: "POST",
          body: JSON.stringify({
            peer: params.peer,
            query: params.query,
            limit: Math.min(50, Math.max(1, params.limit ?? 20)),
            from_user: params.from_user ?? null,
          }),
        });
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const messages =
          (res.data as {
            messages?: Array<{ id: unknown; text?: string; sender_name?: string; date?: string; topic_id?: unknown }>;
          })?.messages ?? [];
        const lines = messages.map((message) => {
          const parts = [
            typeof message.id !== "undefined" ? `id:${message.id}` : null,
            message.sender_name ?? null,
            message.topic_id ? `topic:${message.topic_id}` : null,
            message.date ?? null,
          ].filter(Boolean);
          return `${parts.join(" | ")}${parts.length ? " | " : ""}${message.text || "(no text)"}`;
        });
        return toolResult(lines.length ? lines.join("\n") : "No messages found.");
      },
    },
    optional
  );

  if (allowPrivilegedTools) {
    api.registerTool(
      {
        name: `${prefix}_download_media`,
        description:
          `Download media from one Telegram message using the "${profileLabel}" context. ` +
          "Use this when the user explicitly asks to save an attachment from a message.",
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], { description: "Username, chat id, or 'me'" }),
          message_id: Type.Number({ minimum: 1 }),
          output_path: Type.Optional(Type.String({ description: "Optional target path on the backend host" })),
        }),
        async execute(_id: string, params: { peer: string | number; message_id: number; output_path?: string }) {
          const peer = encodeURIComponent(String(params.peer));
          const output =
            typeof params.output_path === "string" && params.output_path.trim()
              ? `&output_path=${encodeURIComponent(params.output_path)}`
              : "";
          const res = await fetchBridge(
            api,
            profile,
            `/download_media?peer=${peer}&message_id=${Math.max(1, params.message_id)}${output}`
          );
          if (!res.ok) {
            return toolResult(formatBridgeError(res));
          }
          const path = (res.data as { path?: unknown } | undefined)?.path;
          return toolResult(path ? `Media downloaded to ${path}.` : "Media downloaded.");
        },
      },
      optional
    );
  }

  api.registerTool(
    {
      name: `${prefix}_resolve_username`,
      description: `Resolve a Telegram username to id/type using the "${profileLabel}" context.`,
      parameters: Type.Object({
        username: Type.String({ minLength: 1, description: "Telegram username, with or without @" }),
      }),
      async execute(_id: string, params: { username: string }) {
        const username = encodeURIComponent(params.username);
        const res = await fetchBridge(api, profile, `/resolve_username?username=${username}`);
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const data = (res.data as Record<string, unknown> | undefined) ?? {};
        return toolResult(
          `${data.title || data.username || data.id || params.username} (id: ${data.id ?? "?"}${data.username ? `, @${data.username}` : ""}${
            data.type ? `, ${data.type}` : ""
          })`
        );
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_get_user_status`,
      description: `Get Telegram user online/offline status using the "${profileLabel}" context.`,
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "User username or id" }),
      }),
      async execute(_id: string, params: { peer: string | number }) {
        const peer = encodeURIComponent(String(params.peer));
        const res = await fetchBridge(api, profile, `/user_status?peer=${peer}`);
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const data = (res.data as Record<string, unknown> | undefined) ?? {};
        return toolResult(
          `${data.username || data.id || params.peer} | status:${data.status_type || "unknown"}${
            data.was_online ? ` | was_online:${data.was_online}` : ""
          }`
        );
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_get_participants`,
      description:
        `List Telegram group/channel participants visible to the "${profileLabel}" context. ` +
        "Use this only when the user explicitly asks about members.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Group/channel username or id" }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 200, default: 100 })),
        offset: Type.Optional(Type.Number({ minimum: 0, default: 0 })),
      }),
      async execute(_id: string, params: { peer: string | number; limit?: number; offset?: number }) {
        const peer = encodeURIComponent(String(params.peer));
        const limit = Math.min(200, Math.max(1, params.limit ?? 100));
        const offset = Math.max(0, params.offset ?? 0);
        const res = await fetchBridge(api, profile, `/participants?peer=${peer}&limit=${limit}&offset=${offset}`);
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const participants =
          (res.data as { participants?: Array<{ id?: unknown; title?: string; username?: string }> } | undefined)
            ?.participants ?? [];
        const lines = participants.map((participant) =>
          `- ${participant.title || participant.username || participant.id} (id: ${participant.id}${
            participant.username ? `, @${participant.username}` : ""
          })`
        );
        return toolResult(lines.length ? lines.join("\n") : "No participants.");
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_get_admins`,
      description:
        `List Telegram group, supergroup, or channel admins visible to the "${profileLabel}" context. ` +
        "Use this only when the user explicitly asks about administrators.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], {
          description: "Group, supergroup, or channel username or id",
        }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 200, default: 100 })),
      }),
      async execute(_id: string, params: { peer: string | number; limit?: number }) {
        const peer = encodeURIComponent(String(params.peer));
        const limit = Math.min(200, Math.max(1, params.limit ?? 100));
        const res = await fetchBridge(api, profile, `/admins?peer=${peer}&limit=${limit}`);
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const admins =
          (res.data as { admins?: Array<{ id?: unknown; title?: string; username?: string }> } | undefined)?.admins ??
          [];
        const lines = admins.map((admin) =>
          `- ${admin.title || admin.username || admin.id} (id: ${admin.id}${admin.username ? `, @${admin.username}` : ""})`
        );
        return toolResult(lines.length ? lines.join("\n") : "No admins.");
      },
    },
    optional
  );

  if (allowPrivilegedTools) {
    api.registerTool(
      {
        name: `${prefix}_list_contacts`,
        description: `List Telegram contacts visible to the "${profileLabel}" context.`,
        parameters: Type.Object({}),
        async execute() {
          const res = await fetchBridge(api, profile, "/contacts");
          if (!res.ok) return toolResult(formatBridgeError(res));
          const contacts =
            (res.data as { contacts?: Array<{ id?: unknown; title?: string; username?: string; phone?: string }> } | undefined)
              ?.contacts ?? [];
          const lines = contacts.map((contact) =>
            `- ${contact.title || contact.username || contact.id} (id: ${contact.id}${contact.username ? `, @${contact.username}` : ""}${
              contact.phone ? `, ${contact.phone}` : ""
            })`
          );
          return toolResult(lines.length ? lines.join("\n") : "No contacts.");
        },
      },
      optional
    );
  }

  if (allowPrivilegedTools) {
    api.registerTool(
      {
        name: `${prefix}_search_contacts`,
        description: `Search Telegram contacts visible to the "${profileLabel}" context.`,
        parameters: Type.Object({
          query: Type.String({ minLength: 1 }),
          limit: Type.Optional(Type.Number({ minimum: 1, maximum: 50, default: 10 })),
        }),
        async execute(_id: string, params: { query: string; limit?: number }) {
          const res = await fetchBridge(api, profile, "/search_contacts", {
            method: "POST",
            body: JSON.stringify({ query: params.query, limit: Math.min(50, Math.max(1, params.limit ?? 10)) }),
          });
          if (!res.ok) return toolResult(formatBridgeError(res));
          const contacts =
            (res.data as { contacts?: Array<{ id?: unknown; title?: string; username?: string; phone?: string }> } | undefined)
              ?.contacts ?? [];
          const lines = contacts.map((contact) =>
            `- ${contact.title || contact.username || contact.id} (id: ${contact.id}${contact.username ? `, @${contact.username}` : ""}${
              contact.phone ? `, ${contact.phone}` : ""
            })`
          );
          return toolResult(lines.length ? lines.join("\n") : "No contacts found.");
        },
      },
      optional
    );
  }

  if (allowPrivilegedTools) {
    api.registerTool(
      {
        name: `${prefix}_get_blocked_users`,
        description: `List blocked Telegram users visible to the "${profileLabel}" context.`,
        parameters: Type.Object({
          limit: Type.Optional(Type.Number({ minimum: 1, maximum: 200, default: 100 })),
        }),
        async execute(_id: string, params: { limit?: number }) {
          const limit = Math.min(200, Math.max(1, params.limit ?? 100));
          const res = await fetchBridge(api, profile, `/blocked_users?limit=${limit}`);
          if (!res.ok) return toolResult(formatBridgeError(res));
          const users =
            (res.data as { users?: Array<{ id?: unknown; title?: string; username?: string }> } | undefined)?.users ?? [];
          const lines = users.map((user) =>
            `- ${user.title || user.username || user.id} (id: ${user.id}${user.username ? `, @${user.username}` : ""})`
          );
          return toolResult(lines.length ? lines.join("\n") : "No blocked users.");
        },
      },
      optional
    );
  }

  api.registerTool(
    {
      name: `${prefix}_get_banned_users`,
      description:
        `List banned users in a supergroup or channel using the "${profileLabel}" context. ` +
        "Basic groups are not supported for this flow.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Supergroup or channel username or id" }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 200, default: 100 })),
        offset: Type.Optional(Type.Number({ minimum: 0, default: 0 })),
      }),
      async execute(_id: string, params: { peer: string | number; limit?: number; offset?: number }) {
        const peer = encodeURIComponent(String(params.peer));
        const limit = Math.min(200, Math.max(1, params.limit ?? 100));
        const offset = Math.max(0, params.offset ?? 0);
        const res = await fetchBridge(api, profile, `/banned_users?peer=${peer}&limit=${limit}&offset=${offset}`);
        if (!res.ok) return toolResult(formatBridgeError(res));
        const users =
          (res.data as { users?: Array<{ id?: unknown; title?: string; username?: string }> } | undefined)?.users ?? [];
        const lines = users.map((user) =>
          `- ${user.title || user.username || user.id} (id: ${user.id}${user.username ? `, @${user.username}` : ""})`
        );
        return toolResult(lines.length ? lines.join("\n") : "No banned users.");
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_get_chat`,
      description: `Get detailed chat metadata using the "${profileLabel}" context.`,
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Username or chat id" }),
      }),
      async execute(_id: string, params: { peer: string | number }) {
        const peer = encodeURIComponent(String(params.peer));
        const res = await fetchBridge(api, profile, `/chat?peer=${peer}`);
        if (!res.ok) return toolResult(formatBridgeError(res));
        const data = (res.data as Record<string, unknown> | undefined) ?? {};
        return toolResult(
          `${data.title || data.username || data.id || params.peer} (id: ${data.id}${data.username ? `, @${data.username}` : ""}${
            data.type ? `, ${data.type}` : ""
          }${data.participants_count ? `, participants:${data.participants_count}` : ""})${data.about ? `\n${data.about}` : ""}`
        );
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_get_history`,
      description: `Get recent message history using the "${profileLabel}" context.`,
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Username or chat id" }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 100, default: 50 })),
      }),
      async execute(_id: string, params: { peer: string | number; limit?: number }) {
        const peer = encodeURIComponent(String(params.peer));
        const limit = Math.min(100, Math.max(1, params.limit ?? 50));
        const res = await fetchBridge(api, profile, `/history?peer=${peer}&limit=${limit}`);
        if (!res.ok) return toolResult(formatBridgeError(res));
        const messages =
          (res.data as { messages?: Array<{ id?: unknown; text?: string; sender_name?: string; date?: string }> } | undefined)
            ?.messages ?? [];
        const lines = messages.map((message) =>
          `id:${message.id}${message.sender_name ? ` | ${message.sender_name}` : ""}${message.date ? ` | ${message.date}` : ""}${
            message.text ? ` | ${message.text}` : ""
          }`
        );
        return toolResult(lines.length ? lines.join("\n") : "No history.");
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_search_public_chats`,
      description: `Search public Telegram chats visible to the "${profileLabel}" context.`,
      parameters: Type.Object({
        query: Type.String({ minLength: 1 }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 50, default: 20 })),
      }),
      async execute(_id: string, params: { query: string; limit?: number }) {
        const q = encodeURIComponent(params.query);
        const limit = Math.min(50, Math.max(1, params.limit ?? 20));
        const res = await fetchBridge(api, profile, `/search_public_chats?query=${q}&limit=${limit}`);
        if (!res.ok) return toolResult(formatBridgeError(res));
        const results =
          (res.data as { results?: Array<{ id?: unknown; title?: string; username?: string; type?: string }> } | undefined)
            ?.results ?? [];
        const lines = results.map((item) =>
          `- ${item.title || item.username || item.id} (id: ${item.id}${item.username ? `, @${item.username}` : ""}${
            item.type ? `, ${item.type}` : ""
          })`
        );
        return toolResult(lines.length ? lines.join("\n") : "No public chats found.");
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_get_media_info`,
      description: `Get detailed media metadata for one Telegram message using the "${profileLabel}" context.`,
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Username or chat id" }),
        message_id: Type.Number({ minimum: 1 }),
      }),
      async execute(_id: string, params: { peer: string | number; message_id: number }) {
        const peer = encodeURIComponent(String(params.peer));
        const res = await fetchBridge(api, profile, `/media_info?peer=${peer}&message_id=${Math.max(1, params.message_id)}`);
        if (!res.ok) return toolResult(formatBridgeError(res));
        const data = (res.data as Record<string, unknown> | undefined) ?? {};
        const parts = [
          data.media_type ? `media:${data.media_type}` : null,
          data.file_name ? `file:${data.file_name}` : null,
          data.mime_type ? `mime:${data.mime_type}` : null,
          data.file_size ? `size:${data.file_size}` : null,
          data.latitude && data.longitude ? `geo:${data.latitude},${data.longitude}` : null,
        ].filter(Boolean);
        return toolResult(parts.length ? parts.join(" | ") : "No media metadata.");
      },
    },
    optional
  );

  if (allowPrivilegedTools) {
    api.registerTool(
      {
        name: `${prefix}_get_invite_link`,
        description: `Get a Telegram invite link using the "${profileLabel}" context.`,
        parameters: Type.Object({
          peer: Type.Union([Type.String(), Type.Number()], { description: "Group/channel username or id" }),
        }),
        async execute(_id: string, params: { peer: string | number }) {
          const peer = encodeURIComponent(String(params.peer));
          const res = await fetchBridge(api, profile, `/invite_link?peer=${peer}`);
          if (!res.ok) return toolResult(formatBridgeError(res));
          const link = (res.data as { link?: unknown } | undefined)?.link;
          return toolResult(link ? String(link) : "No invite link.");
        },
      },
      optional
    );
  }

  api.registerTool(
    {
      name: `${prefix}_get_recent_actions`,
      description:
        `Get recent admin actions in a Telegram supergroup or channel using the "${profileLabel}" context. ` +
        "Basic groups are not supported for this flow.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Supergroup or channel username or id" }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 50, default: 20 })),
      }),
      async execute(_id: string, params: { peer: string | number; limit?: number }) {
        const peer = encodeURIComponent(String(params.peer));
        const limit = Math.min(50, Math.max(1, params.limit ?? 20));
        const res = await fetchBridge(api, profile, `/recent_actions?peer=${peer}&limit=${limit}`);
        if (!res.ok) return toolResult(formatBridgeError(res));
        const events =
          (res.data as { events?: Array<{ id?: unknown; date?: string; user_id?: unknown; action?: string }> } | undefined)
            ?.events ?? [];
        const lines = events.map((event) =>
          `- id:${event.id}${event.user_id ? ` | user:${event.user_id}` : ""}${event.action ? ` | ${event.action}` : ""}${
            event.date ? ` | ${event.date}` : ""
          }`
        );
        return toolResult(lines.length ? lines.join("\n") : "No recent actions.");
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_get_pinned_messages`,
      description: `Get pinned Telegram messages using the "${profileLabel}" context.`,
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Group/channel username or id" }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 50, default: 20 })),
      }),
      async execute(_id: string, params: { peer: string | number; limit?: number }) {
        const peer = encodeURIComponent(String(params.peer));
        const limit = Math.min(50, Math.max(1, params.limit ?? 20));
        const res = await fetchBridge(api, profile, `/pinned_messages?peer=${peer}&limit=${limit}`);
        if (!res.ok) return toolResult(formatBridgeError(res));
        const messages =
          (res.data as { messages?: Array<{ id?: unknown; text?: string; sender_name?: string; date?: string }> } | undefined)
            ?.messages ?? [];
        const lines = messages.map((message) =>
          `id:${message.id}${message.sender_name ? ` | ${message.sender_name}` : ""}${message.date ? ` | ${message.date}` : ""}${
            message.text ? ` | ${message.text}` : ""
          }`
        );
        return toolResult(lines.length ? lines.join("\n") : "No pinned messages.");
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_get_message_reactions`,
      description: `Get Telegram reactions on one message using the "${profileLabel}" context.`,
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Chat username or id" }),
        message_id: Type.Number({ minimum: 1 }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 100, default: 50 })),
      }),
      async execute(_id: string, params: { peer: string | number; message_id: number; limit?: number }) {
        const peer = encodeURIComponent(String(params.peer));
        const limit = Math.min(100, Math.max(1, params.limit ?? 50));
        const res = await fetchBridge(
          api,
          profile,
          `/message_reactions?peer=${peer}&message_id=${Math.max(1, params.message_id)}&limit=${limit}`
        );
        if (!res.ok) return toolResult(formatBridgeError(res));
        const reactions =
          (res.data as { reactions?: Array<{ emoji?: string; count?: unknown }> } | undefined)?.reactions ?? [];
        const lines = reactions.map((reaction) => `- ${reaction.emoji || "?"}: ${reaction.count ?? 0}`);
        return toolResult(lines.length ? lines.join("\n") : "No reactions.");
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_list_topics`,
      description:
        `List forum topics for a Telegram forum chat using the "${profileLabel}" context. ` +
        "Use this before topic-specific polling. The returned topic_id is the thread root message id to pass into get_messages.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Forum chat username or chat id" }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 100, default: 20 })),
      }),
      async execute(_id: string, params: { peer: string | number; limit?: number }) {
        const limit = Math.min(100, Math.max(1, params.limit ?? 20));
        const peer = encodeURIComponent(String(params.peer));
        const res = await fetchBridge(api, profile, `/topics?peer=${peer}&limit=${limit}`);
        if (!res.ok) {
          return toolResult(formatBridgeError(res));
        }
        const topics =
          (res.data as {
            topics?: Array<{
              topic_id?: unknown;
              title?: string;
              unread_count?: unknown;
              closed?: boolean;
              pinned?: boolean;
              hidden?: boolean;
            }>;
          })?.topics ?? [];
        const lines = topics.map((topic) => {
          const flags = [
            typeof topic.unread_count === "number" ? `unread:${topic.unread_count}` : null,
            topic.pinned ? "pinned" : null,
            topic.closed ? "closed" : null,
            topic.hidden ? "hidden" : null,
          ].filter(Boolean);
          return `- ${topic.title || `Topic ${topic.topic_id}`} (topic_id: ${topic.topic_id}${
            flags.length ? `, ${flags.join(", ")}` : ""
          })`;
        });
        return toolResult(lines.length ? lines.join("\n") : "No forum topics.");
      },
    },
    optional
  );

  api.registerTool(
    {
      name: `${prefix}_get_messages`,
      description:
        isSourcesReadOnly
          ? `Get recent messages from an auto-discovered read-only Telegram source using the "${profileLabel}" context. ` +
            "Use this in scheduled polling jobs. Prefer min_id to fetch only new messages and save tokens. " +
            "Use since_unix for strict time windows such as the last 24 hours. " +
            "For forum threads, pass topic_id from list_topics or from earlier message metadata."
          : `Get recent messages from a Telegram chat using the "${profileLabel}" context. ` +
            "Use when the user explicitly asks to read Telegram messages or when a scheduled workflow polls new messages. " +
            "Use min_id to fetch only newer messages and reduce token usage. " +
            "Use since_unix for strict time windows such as the last 24 hours. " +
            "For forum threads, pass topic_id from list_topics or from earlier message metadata.",
      parameters: Type.Object({
        peer: Type.Union([Type.String(), Type.Number()], { description: "Username, chat id, or 'me'" }),
        limit: Type.Optional(Type.Number({ minimum: 1, maximum: 50, default: 20 })),
        min_id: Type.Optional(Type.Number({ minimum: 1, description: "Only return messages newer than this id" })),
        since_unix: Type.Optional(
          Type.Number({
            minimum: 1,
            description: "Only return messages with Unix timestamp >= this value; use for exact time windows",
          })
        ),
        topic_id: Type.Optional(
          Type.Number({
            minimum: 1,
            description: "Optional forum topic root message id returned by list_topics or message metadata",
          })
        ),
      }),
      async execute(
        _id: string,
        params: { peer: string | number; limit?: number; min_id?: number; since_unix?: number; topic_id?: number }
      ) {
        const limit = Math.min(50, Math.max(1, params.limit ?? 20));
        const peer = encodeURIComponent(String(params.peer));
        const minId = typeof params.min_id === "number" ? `&min_id=${Math.max(1, params.min_id)}` : "";
        const sinceUnix =
          typeof params.since_unix === "number" ? `&since_unix=${Math.max(1, Math.floor(params.since_unix))}` : "";
        const topicId = typeof params.topic_id === "number" ? `&topic_id=${Math.max(1, params.topic_id)}` : "";
        const res = await fetchBridge(
          api,
          profile,
          `/messages?peer=${peer}&limit=${limit}${minId}${sinceUnix}${topicId}`
        );
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
              reply_to_message_id?: unknown;
            }>;
          })?.messages ?? [];
        const lines = messages.map((message) => {
          const parts = [
            message.id ? `id:${message.id}` : null,
            `[${message.out ? "out" : "in"}]`,
            message.sender_name ? `${message.sender_name}` : message.sender_id ? `sender:${message.sender_id}` : null,
            message.topic_id ? `topic:${message.topic_id}` : null,
            message.reply_to_message_id ? `reply_to:${message.reply_to_message_id}` : null,
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
  buildInboundDmBody,
  collectRelevantDirectBindings,
  formatBridgeError,
  resolvePollFailureDelayMs,
  normalizePeerKey,
  nextPollBackoffMs,
  startInboundDmTypingLoop,
  resolveConfiguredDmBinding,
  validateStrictDmAccountConfig,
};

export default function register(api: PluginApi) {
  const config = getConfig(api);
  registerDmChannel(api);
  const profileSlugs = new Set(config.profiles.map((profile) => slugifyToolId(profile.id)));
  const registeredPrefixes = new Set<string>();
  const registerPrefix = (profile: ProfileConfig, prefix: string): void => {
    if (registeredPrefixes.has(prefix)) return;
    registerProfileTools(api, profile, prefix);
    registeredPrefixes.add(prefix);
  };
  for (const profile of config.profiles) {
    const primaryPrefix = `telegram_${slugifyToolId(profile.id)}`;
    registerPrefix(profile, primaryPrefix);
    const ownerCompatAlias = resolveOwnerCompatAliasSlug(profile, profileSlugs);
    if (ownerCompatAlias) {
      registerPrefix(profile, `telegram_${ownerCompatAlias}`);
    }
  }
}
