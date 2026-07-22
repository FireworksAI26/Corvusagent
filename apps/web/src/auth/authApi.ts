import type { components } from "../generated/api";

export type SessionResponse = components["schemas"]["SessionResponse"];
export type SessionRefreshResponse = components["schemas"]["SessionRefreshResponse"];
export type OnboardingResponse = components["schemas"]["OnboardingResponse"];
export type OnboardingUpdate = components["schemas"]["OnboardingUpdate"];
export type SyncApplyResult = components["schemas"]["SyncApplyResult"];
export type SyncMutationBatch = components["schemas"]["SyncMutationBatch"];
export type SyncPage = components["schemas"]["SyncPage"];
export type Workspace = components["schemas"]["Workspace"];
export type WorkspaceCreate = components["schemas"]["WorkspaceCreate"];
export interface CloudRuntime {
  workspace_id: string;
  provider: "e2b";
  configured: boolean;
  sandbox_id: string | null;
  template_ref: string;
  generation: number;
  state: "unprovisioned" | "provisioning" | "ready" | "pausing" | "paused" | "resuming" | "failed" | "lost";
  updated_at: string;
  error_code: string | null;
}

export class AuthApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;
  readonly detail: Readonly<Record<string, unknown>>;

  constructor(
    status: number,
    code: string,
    correlationId: string | null = null,
    detail: Readonly<Record<string, unknown>> = {}
  ) {
    super(code);
    this.name = "AuthApiError";
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
    this.detail = detail;
  }
}

export interface AuthApi {
  getSession(): Promise<SessionResponse>;
  logout(csrfToken: string): Promise<void>;
  refreshSession(csrfToken: string): Promise<SessionRefreshResponse>;
  startGoogle(): void;
}

export interface PlatformApi extends AuthApi {
  applySync(
    workspaceId: string,
    body: SyncMutationBatch,
    csrfToken: string
  ): Promise<SyncApplyResult>;
  createWorkspace(
    body: WorkspaceCreate,
    csrfToken: string,
    idempotencyKey: string
  ): Promise<Workspace>;
  getSyncPage(workspaceId: string, cursor: number): Promise<SyncPage>;
  getWorkspace(workspaceId: string): Promise<Workspace>;
  listWorkspaces(): Promise<Workspace[]>;
  updateOnboarding(body: OnboardingUpdate, csrfToken: string): Promise<OnboardingResponse>;
  getCloudRuntime(workspaceId: string): Promise<CloudRuntime>;
  provisionCloudRuntime(workspaceId: string, csrfToken: string, idempotencyKey: string): Promise<CloudRuntime>;
  resumeCloudRuntime(workspaceId: string, csrfToken: string): Promise<CloudRuntime>;
  pauseCloudRuntime(workspaceId: string, csrfToken: string): Promise<CloudRuntime>;
  revokeCloudRuntime(workspaceId: string, csrfToken: string): Promise<CloudRuntime>;
}

interface AuthApiOptions {
  fetchImpl?: typeof fetch;
  navigate?: (path: string) => void;
  origin?: string;
}

interface ErrorEnvelope {
  detail?: {
    code?: unknown;
    correlation_id?: unknown;
    [key: string]: unknown;
  };
}

const JSON_CONTENT_TYPE = "application/json";
const GOOGLE_START_PATH = "/api/v2/auth/google/start";

async function decodeError(response: Response): Promise<AuthApiError> {
  let envelope: ErrorEnvelope = {};
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    // A stable fallback prevents an invalid upstream body from escaping this boundary.
  }
  const detail = envelope.detail ?? {};
  const code = typeof detail.code === "string" ? detail.code : "platform_request_failed";
  const correlationId =
    typeof detail.correlation_id === "string" ? detail.correlation_id : null;
  return new AuthApiError(response.status, code, correlationId, detail);
}

export function createAuthApi(options: AuthApiOptions = {}): PlatformApi {
  const fetchImpl = options.fetchImpl ?? fetch;
  const origin = options.origin ?? window.location.origin;
  const navigate = options.navigate ?? ((path: string) => window.location.assign(path));

  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    let response: Response;
    try {
      response = await fetchImpl(path, { credentials: "include", ...init });
    } catch (error) {
      throw new AuthApiError(0, "network_unavailable", null, {
        cause: error instanceof Error ? error.name : "unknown"
      });
    }
    if (!response.ok) throw await decodeError(response);
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  function mutationHeaders(csrfToken: string): HeadersInit {
    return {
      "Content-Type": JSON_CONTENT_TYPE,
      Origin: origin,
      "X-CSRF-Token": csrfToken
    };
  }

  function idempotentHeaders(csrfToken: string, idempotencyKey: string): HeadersInit {
    return { ...mutationHeaders(csrfToken), "Idempotency-Key": idempotencyKey };
  }

  return {

    getCloudRuntime: (workspaceId) =>
      request<CloudRuntime>(`/api/v2/workspaces/${encodeURIComponent(workspaceId)}/runtime`),
    provisionCloudRuntime: (workspaceId, csrfToken, idempotencyKey) =>
      request<CloudRuntime>(`/api/v2/workspaces/${encodeURIComponent(workspaceId)}/runtime/provision`, {
        method: "POST", headers: idempotentHeaders(csrfToken, idempotencyKey)
      }),
    resumeCloudRuntime: (workspaceId, csrfToken) =>
      request<CloudRuntime>(`/api/v2/workspaces/${encodeURIComponent(workspaceId)}/runtime/resume`, {
        method: "POST", headers: mutationHeaders(csrfToken)
      }),
    pauseCloudRuntime: (workspaceId, csrfToken) =>
      request<CloudRuntime>(`/api/v2/workspaces/${encodeURIComponent(workspaceId)}/runtime/pause`, {
        method: "POST", headers: mutationHeaders(csrfToken)
      }),
    revokeCloudRuntime: (workspaceId, csrfToken) =>
      request<CloudRuntime>(`/api/v2/workspaces/${encodeURIComponent(workspaceId)}/runtime`, {
        method: "DELETE", headers: mutationHeaders(csrfToken)
      }),
    applySync: (workspaceId, body, csrfToken) =>
      request<SyncApplyResult>(
        `/api/v2/workspaces/${encodeURIComponent(workspaceId)}/sync/mutations`,
        {
          method: "POST",
          headers: mutationHeaders(csrfToken),
          body: JSON.stringify(body)
        }
      ),
    createWorkspace: (body, csrfToken, idempotencyKey) =>
      request<Workspace>("/api/v2/workspaces", {
        method: "POST",
        headers: idempotentHeaders(csrfToken, idempotencyKey),
        body: JSON.stringify(body)
      }),
    getSession: () => request<SessionResponse>("/api/v2/session"),
    getSyncPage: (workspaceId, cursor) =>
      request<SyncPage>(
        `/api/v2/workspaces/${encodeURIComponent(workspaceId)}/sync?cursor=${cursor}&limit=100`
      ),
    getWorkspace: (workspaceId) =>
      request<Workspace>(`/api/v2/workspaces/${encodeURIComponent(workspaceId)}`),
    listWorkspaces: () => request<Workspace[]>("/api/v2/workspaces"),
    logout: (csrfToken) =>
      request<void>("/api/v2/logout", {
        method: "POST",
        headers: mutationHeaders(csrfToken)
      }),
    refreshSession: (csrfToken) =>
      request<SessionRefreshResponse>("/api/v2/session/refresh", {
        method: "POST",
        headers: mutationHeaders(csrfToken)
      }),
    startGoogle: () => navigate(GOOGLE_START_PATH),
    updateOnboarding: (body, csrfToken) =>
      request<OnboardingResponse>("/api/v2/onboarding", {
        method: "PUT",
        headers: mutationHeaders(csrfToken),
        body: JSON.stringify(body)
      })
  };
}
