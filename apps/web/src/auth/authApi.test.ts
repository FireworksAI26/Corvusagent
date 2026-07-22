import { describe, expect, it, vi } from "vitest";

import { createAuthApi } from "./authApi";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

describe("hosted identity API boundary", () => {
  it("uses credentialed same-origin requests without constructing a loopback URL", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse([]));
    const api = createAuthApi({ fetchImpl });

    await api.listWorkspaces();

    expect(fetchImpl).toHaveBeenCalledWith("/api/v2/workspaces", {
      credentials: "include"
    });
    expect(fetchImpl.mock.calls[0][0]).not.toContain("127.0.0.1");
  });

  it("adds CSRF, Origin, and a stable idempotency key only to the hosted mutation", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      id: "33333333-3333-4333-8333-333333333333",
      name: "Field desk",
      workspace_kind: "individual",
      status: "active",
      created_at: "2026-07-17T00:00:00Z",
      updated_at: "2026-07-17T00:00:00Z",
      version: 1
    }));
    const api = createAuthApi({ fetchImpl, origin: "https://corvus.example" });

    await api.createWorkspace(
      { name: "Field desk", workspace_kind: "individual" },
      "csrf-memory-only",
      "attempt-7"
    );

    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(init.credentials).toBe("include");
    expect(init.headers).toMatchObject({
      Origin: "https://corvus.example",
      "X-CSRF-Token": "csrf-memory-only",
      "Idempotency-Key": "attempt-7"
    });
  });

  it("decodes a typed stable error with correlation and conflict detail", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      detail: {
        code: "sync_version_conflict",
        correlation_id: "correlation-7",
        submitted_expected_version: 2,
        current_version: 3
      }
    }, 409));
    const api = createAuthApi({ fetchImpl });

    await expect(api.getSession()).rejects.toMatchObject({
      status: 409,
      code: "sync_version_conflict",
      correlationId: "correlation-7",
      detail: expect.objectContaining({ current_version: 3 })
    });
  });

  it("navigates to the same-origin Google start path", () => {
    const navigate = vi.fn();
    const api = createAuthApi({ navigate });

    api.startGoogle();

    expect(navigate).toHaveBeenCalledWith("/api/v2/auth/google/start");
  });
});
