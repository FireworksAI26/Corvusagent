import { isLoopbackRuntimeHost } from "../runtime/localRuntime";
import { downloadArtifact } from "./artifactDownload";

export interface LocalChatRun {
  run_id: string;
  handle_id: string;
  state: "running" | "completed" | "failed";
  provider: RunnableProviderId;
  model: string;
  mode: "chat" | "build";
  storage: "this_device";
  created_at: string;
  working_directory: string;
  safety: SafetyPreview;
}

export interface LocalChatCancel {
  run_id: string;
  state: "running" | "cancelled" | "completed" | "failed";
  accepted: boolean;
  reason_code: string | null;
}

export interface RunEventMessage { data: string; }
export type RunEventListener = (event: RunEventMessage) => void;
export interface RunEventStream {
  addEventListener(type: string, listener: RunEventListener): void;
  onTerminalError(listener: () => void): void;
  close(): void;
}

class BrowserRunEventStream implements RunEventStream {
  constructor(private readonly source: EventSource) {}

  addEventListener(type: string, listener: RunEventListener): void {
    this.source.addEventListener(type, listener as unknown as EventListener);
  }

  onTerminalError(listener: () => void): void {
    this.source.addEventListener("error", () => {
      if (this.source.readyState === this.source.CLOSED) listener();
    });
  }

  close(): void {
    this.source.close();
  }
}

export type ProviderId = "codex" | "claude" | "openai" | "anthropic" | "gemini" | "xai" | "openrouter" | "together" | "fireworks" | "groq" | "mistral" | "deepseek" | "cursor" | "grok";
export type RunnableProviderId = "codex" | "claude" | "openai" | "anthropic" | "gemini" | "xai" | "openrouter" | "together" | "fireworks" | "groq" | "mistral" | "deepseek";
export type ThinkingLevel = "low" | "medium" | "high" | "xhigh" | "max";
export type RunMode = "chat" | "build";
export type SafetyLevel = "read_only" | "protected" | "elevated";

export interface SafetyPreview {
  policy_digest: string;
  level: SafetyLevel;
  label: string;
  summary: string;
  execution: string;
  filesystem: string;
  network: string;
  mcp: string;
  approvals: string;
  output: string;
  requires_confirmation: boolean;
}

export interface SafetyReceipt {
  run_id: string;
  status: "completed" | "failed" | "cancelled";
  safety: SafetyPreview;
  activities: string[];
  mcp_used: boolean;
  approval: string;
  original_project_modified: boolean;
  artifact: {
    download_name: string;
    sha256_digest: string;
    size_bytes: number;
    secret_screening: "passed" | "not_scanned";
  } | null;
}

export interface ProviderModel {
  id: string;
  label: string;
  recommended: boolean;
}

export interface ProviderCatalogEntry {
  id: ProviderId;
  label: string;
  status: "ready" | "preview" | "unavailable";
  runtime: "local" | "api";
  models: ProviderModel[];
  status_label: string;
  thinking_levels: ThinkingLevel[];
  supports_mcp: boolean;
}

export interface LocalChatRequest {
  provider: RunnableProviderId;
  model: string | null;
  effort: ThinkingLevel;
  mode: RunMode;
  mcp_enabled: boolean;
  safety_digest?: string | null;
  repository_id?: string | null;
  context?: Array<{ role: "user" | "assistant"; content: string }>;
}

export interface ConversationRepository {
  id: string;
  display_name: string;
  path: string;
}

export type ResponseTone = "concise" | "balanced" | "detailed";
export type ProviderCredentialId = "openai" | "anthropic" | "gemini" | "xai" | "openrouter" | "together" | "fireworks" | "groq" | "mistral" | "deepseek";

export interface ProviderCredentialStatus {
  provider: ProviderCredentialId;
  configured: boolean;
  source: "keyring" | "environment" | "none";
}

export interface ProviderCredentialVerification {
  provider: ProviderCredentialId;
  configured: boolean;
  verified: boolean;
  models: string[];
}

export interface McpServerConfiguration {
  name: string;
  enabled: boolean;
  transport: string;
  endpoint: string;
  auth_status: string;
}

export interface RuntimePreferences {
  version: number;
  default_provider: RunnableProviderId;
  default_model: string | null;
  model_labels?: Record<string, string>;
  default_effort: ThinkingLevel;
  default_mode: RunMode;
  mcp_enabled: boolean;
  response_tone: ResponseTone;
  custom_rules: string;
  updated_at: string | null;
}

export interface RuntimePreferencesUpdate extends Omit<RuntimePreferences, "version" | "updated_at"> {
  expected_version: number;
}

export interface ConversationApi {
  listProviders(): Promise<ProviderCatalogEntry[]>;
  listRepositories?(): Promise<ConversationRepository[]>;
  getPreferences(): Promise<RuntimePreferences>;
  updatePreferences(preferences: RuntimePreferencesUpdate): Promise<RuntimePreferences>;
  getSafetyPreview(
    provider: RunnableProviderId,
    mode: RunMode,
    mcpEnabled: boolean
  ): Promise<SafetyPreview>;
  getSafetyReceipt(runId: string): Promise<SafetyReceipt>;
  startRun(
    prompt: string,
    request: LocalChatRequest,
    idempotencyKey: string
  ): Promise<LocalChatRun>;
  cancelRun(runId: string): Promise<LocalChatCancel>;
  openRunEvents(runId: string): RunEventStream;
  artifactUrl(runId: string): string;
  downloadArtifact(runId: string, suggestedName: string): Promise<string | null>;
  listProviderCredentials?(): Promise<ProviderCredentialStatus[]>;
  connectProviderCredential?(provider: ProviderCredentialId, credential: string): Promise<ProviderCredentialStatus>;
  verifyProviderCredential?(provider: ProviderCredentialId): Promise<ProviderCredentialVerification>;
  removeProviderCredential?(provider: ProviderCredentialId): Promise<ProviderCredentialStatus>;
  listMcpServers?(): Promise<McpServerConfiguration[]>;
  addMcpServer?(name: string, url: string): Promise<McpServerConfiguration>;
  removeMcpServer?(name: string): Promise<void>;
  loginMcpServer?(name: string): Promise<void>;
}

interface ApiErrorDetail {
  code?: string;
  correlation_id?: string;
  current?: RuntimePreferences;
}
interface ApiErrorBody { detail?: string | ApiErrorDetail; }

const LOCAL_REPOSITORY_PAGE_SIZE = 100;
const MAX_LOCAL_REPOSITORY_PAGES = 20;

export class ConversationApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly correlationId: string | null,
    readonly detail: ApiErrorDetail | null = null
  ) {
    super(code);
    this.name = "ConversationApiError";
  }
}

export function createConversationApi(csrfToken: string, baseUrl = ""): ConversationApi {
  const runtimeBaseUrl = trustedRuntimeBaseUrl(baseUrl);

  function runtimeUrl(path: string): string {
    return `${runtimeBaseUrl}${path}`;
  }

  async function requestJson<T>(path: string, init: RequestInit): Promise<T> {
    const response = await fetch(runtimeUrl(path), {
      ...init,
      credentials: "include",
      headers: { Accept: "application/json", ...init.headers }
    });
    if (!response.ok) {
      let body: ApiErrorBody = {};
      try { body = await response.json() as ApiErrorBody; } catch { /* Never expose raw bodies. */ }
      const detail = body.detail;
      const code = typeof detail === "string"
        ? detail
        : detail?.code ?? `request_failed_${response.status}`;
      const correlationId = typeof detail === "object" && detail !== null
        ? detail.correlation_id ?? null
        : null;
      throw new ConversationApiError(
        response.status,
        code,
        correlationId,
        typeof detail === "object" && detail !== null ? detail : null
      );
    }
    return await response.json() as T;
  }

  async function requestEmpty(path: string, init: RequestInit): Promise<void> {
    const response = await fetch(runtimeUrl(path), {
      ...init,
      credentials: "include",
      headers: { Accept: "application/json", ...init.headers }
    });
    if (!response.ok) throw new ConversationApiError(response.status, `request_failed_${response.status}`, null);
  }

  function mutationHeaders(idempotencyKey?: string): Record<string, string> {
    if (csrfToken === "") throw new ConversationApiError(401, "paired_session_required", null);
    return {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {})
    };
  }

  async function listAllRepositories(): Promise<ConversationRepository[]> {
    const repositories: ConversationRepository[] = [];
    for (let page = 0; page < MAX_LOCAL_REPOSITORY_PAGES; page += 1) {
      const query = new URLSearchParams({
        limit: String(LOCAL_REPOSITORY_PAGE_SIZE),
        offset: String(page * LOCAL_REPOSITORY_PAGE_SIZE)
      });
      const items = await requestJson<ConversationRepository[]>(
        `/api/local/repositories?${query.toString()}`,
        { method: "GET", headers: { Accept: "application/json" } }
      );
      repositories.push(...items);
      if (items.length < LOCAL_REPOSITORY_PAGE_SIZE) return repositories;
    }
    throw new ConversationApiError(413, "local_collection_limit_exceeded", null);
  }

  return {
    listProviders: async () => {
      const providers = await requestJson<ProviderCatalogEntry[]>("/api/local-chat/providers", {
        method: "GET",
        headers: { Accept: "application/json" }
      });
      return providers.map((provider) => ({
        ...provider,
        models: Array.isArray(provider.models) ? provider.models : [],
        thinking_levels: Array.isArray(provider.thinking_levels) ? provider.thinking_levels : []
      }));
    },
    listRepositories: listAllRepositories,
    getPreferences: () => requestJson("/api/local-chat/preferences", {
      method: "GET",
      headers: { Accept: "application/json" }
    }),
    updatePreferences: (preferences) => requestJson("/api/local-chat/preferences", {
      method: "PUT",
      headers: mutationHeaders(),
      body: JSON.stringify(preferences)
    }),
    getSafetyPreview: (provider, mode, mcpEnabled) => {
      const query = new URLSearchParams({
        provider,
        mode,
        mcp_enabled: String(mcpEnabled)
      });
      return requestJson(`/api/local-chat/safety-preview?${query.toString()}`, {
        method: "GET",
        headers: { Accept: "application/json" }
      });
    },
    getSafetyReceipt: (runId) => requestJson(
      `/api/local-chat/runs/${encodeURIComponent(runId)}/safety-receipt`,
      { method: "GET", headers: { Accept: "application/json" } }
    ),
    startRun: (prompt, request, idempotencyKey) => requestJson("/api/local-chat/runs", {
      method: "POST",
      headers: mutationHeaders(idempotencyKey),
      body: JSON.stringify({ prompt, ...request })
    }),
    cancelRun: (runId) => requestJson(`/api/local-chat/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      headers: mutationHeaders()
    }),
    openRunEvents(runId) {
      return new BrowserRunEventStream(new EventSource(runtimeUrl(`/api/local-chat/runs/${encodeURIComponent(runId)}/events?follow=true`), {
        withCredentials: true
      }));
    },
    artifactUrl: (runId) => runtimeUrl(`/api/local-chat/runs/${encodeURIComponent(runId)}/artifact`),
    downloadArtifact: (runId, suggestedName) => downloadArtifact(
      runtimeUrl(`/api/local-chat/runs/${encodeURIComponent(runId)}/artifact`),
      suggestedName
    ),
    listProviderCredentials: () => requestJson("/api/provider-credentials", {
      method: "GET",
      headers: { Accept: "application/json" }
    }),
    connectProviderCredential: (provider, credential) => requestJson(`/api/provider-credentials/${provider}`, {
      method: "PUT",
      headers: mutationHeaders(),
      body: JSON.stringify({ credential })
    }),
    verifyProviderCredential: (provider) => requestJson(`/api/provider-credentials/${provider}/verify`, {
      method: "POST",
      headers: mutationHeaders()
    }),
    removeProviderCredential: (provider) => requestJson(`/api/provider-credentials/${provider}`, {
      method: "DELETE",
      headers: mutationHeaders()
    }),
    listMcpServers: () => requestJson("/api/local-chat/mcp", {
      method: "GET",
      headers: { Accept: "application/json" }
    }),
    addMcpServer: (name, url) => requestJson("/api/local-chat/mcp", {
      method: "POST",
      headers: mutationHeaders(),
      body: JSON.stringify({ name, url })
    }),
    removeMcpServer: (name) => requestEmpty(`/api/local-chat/mcp/${encodeURIComponent(name)}`, {
      method: "DELETE",
      headers: mutationHeaders()
    }),
    loginMcpServer: (name) => requestEmpty(`/api/local-chat/mcp/${encodeURIComponent(name)}/login`, {
      method: "POST",
      headers: mutationHeaders()
    })
  };
}

function trustedRuntimeBaseUrl(baseUrl: string): string {
  if (baseUrl === "") return "";
  let parsed: URL;
  try {
    parsed = new URL(baseUrl);
  } catch {
    throw new ConversationApiError(400, "runtime_base_url_invalid", null);
  }
  const sameOrigin = typeof window !== "undefined" && parsed.origin === window.location.origin;
  const cleanRoot = parsed.username === "" && parsed.password === "" && parsed.pathname === "/"
    && parsed.search === "" && parsed.hash === "";
  const allowedProtocol = parsed.protocol === "http:" || parsed.protocol === "https:";
  if (!cleanRoot || !allowedProtocol || (!sameOrigin && !isLoopbackRuntimeHost(parsed.hostname))) {
    throw new ConversationApiError(400, "runtime_base_url_untrusted", null);
  }
  return parsed.origin;
}
