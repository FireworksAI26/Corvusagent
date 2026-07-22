import { afterEach, describe, expect, it, vi } from "vitest";

import { ConversationApiError, createConversationApi } from "./conversationApi";

describe("conversation API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses paired-session mutation proofs without claiming remote storage", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      run_id: "run-1", handle_id: "handle-1", state: "running", provider: "codex",
      model: "Codex default", mode: "build", storage: "this_device", created_at: "2026-07-17T02:00:00Z"
    }), { status: 202, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const api = createConversationApi("csrf-local");

    await api.startRun("First task", {
      provider: "codex",
      model: null,
      effort: "high",
      mode: "build",
      mcp_enabled: true,
      safety_digest: "a".repeat(64)
    }, "run-key");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/local-chat/runs",
      expect.objectContaining({
        headers: expect.objectContaining({
          "Idempotency-Key": "run-key",
          "X-CSRF-Token": "csrf-local"
        }),
        body: JSON.stringify({
          prompt: "First task",
          provider: "codex",
          model: null,
          effort: "high",
          mode: "build",
          mcp_enabled: true,
          safety_digest: "a".repeat(64)
        })
      })
    );
  });

  it("surfaces a safe correlation-bearing API failure", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      detail: { code: "codex_unavailable", correlation_id: "corr-1" }
    }), { status: 503, headers: { "Content-Type": "application/json" } })));

    await expect(createConversationApi("csrf").startRun("test", {
      provider: "codex", model: null, effort: "medium", mode: "chat", mcp_enabled: false
    }, "key")).rejects.toEqual(
      new ConversationApiError(503, "codex_unavailable", "corr-1", {
        code: "codex_unavailable", correlation_id: "corr-1"
      })
    );
  });

  it("preserves a safe FastAPI string detail as the failure code", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      detail: "provider_unavailable"
    }), { status: 503, headers: { "Content-Type": "application/json" } })));

    await expect(createConversationApi("csrf").startRun("test", {
      provider: "codex", model: null, effort: "medium", mode: "chat", mcp_enabled: false
    }, "key")).rejects.toEqual(
      new ConversationApiError(503, "provider_unavailable", null)
    );
  });

  it("exposes provider discovery and an owner-authenticated artifact URL", async () => {
    const providers = [{ id: "codex", label: "Codex", status: "ready", runtime: "local", models: null }];
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(providers), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);
    const api = createConversationApi("csrf", "http://127.0.0.1:8765");

    await expect(api.listProviders()).resolves.toEqual([{
      ...providers[0], models: [], thinking_levels: []
    }]);
    expect(api.artifactUrl("run-1")).toBe("http://127.0.0.1:8765/api/local-chat/runs/run-1/artifact");
  });

  it("loads every repository page for the conversation project picker", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const offset = url.searchParams.get("offset");
      const repositories = offset === "100"
        ? [{ id: "repo-101", display_name: "Later repository", path: "C:\\later" }]
        : Array.from({ length: 100 }, (_, index) => ({
            id: `repo-${index}`,
            display_name: `Repository ${index}`,
            path: `C:\\repo-${index}`
          }));
      return new Response(JSON.stringify(repositories), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const repositories = await createConversationApi("csrf").listRepositories!();

    expect(repositories).toHaveLength(101);
    expect(fetchMock.mock.calls.map(([input]) => new URL(String(input), "http://127.0.0.1").search))
      .toEqual(["?limit=100&offset=0", "?limit=100&offset=100"]);
  });

  it("rejects a cross-origin non-loopback runtime before making a request", () => {
    expect(() => createConversationApi("csrf", "https://runtime.example.com"))
      .toThrow(new ConversationApiError(400, "runtime_base_url_untrusted", null));
    expect(() => createConversationApi("csrf", "http://user:password@127.0.0.1:8765"))
      .toThrow(new ConversationApiError(400, "runtime_base_url_untrusted", null));
  });

  it("retains the safe current preference snapshot on a version conflict", async () => {
    const current = {
      version: 3, default_provider: "codex" as const, default_model: "gpt-5.6-sol",
      default_effort: "high" as const, default_mode: "build" as const, mcp_enabled: true,
      response_tone: "balanced" as const, custom_rules: "Verify first.", updated_at: "2026-07-18T00:00:00Z"
    };
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      detail: { code: "preferences_version_conflict", current }
    }), { status: 409, headers: { "Content-Type": "application/json" } })));

    await expect(createConversationApi("csrf").updatePreferences({
      expected_version: 2,
      default_provider: "codex",
      default_model: null,
      default_effort: "medium",
      default_mode: "chat",
      mcp_enabled: false,
      response_tone: "balanced",
      custom_rules: ""
    })).rejects.toEqual(new ConversationApiError(
      409,
      "preferences_version_conflict",
      null,
      { code: "preferences_version_conflict", current }
    ));
  });

  it("loads server-authored safety previews and owner-scoped receipts", async () => {
    const preview = { policy_digest: "a".repeat(64), level: "protected", label: "Protected build" };
    const receipt = { run_id: "run-1", status: "completed", safety: preview };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify(preview), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(receipt), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const api = createConversationApi("csrf");

    await expect(api.getSafetyPreview("codex", "build", true)).resolves.toEqual(preview);
    await expect(api.getSafetyReceipt("run-1")).resolves.toEqual(receipt);
    expect(fetchMock).toHaveBeenNthCalledWith(1,
      "/api/local-chat/safety-preview?provider=codex&mode=build&mcp_enabled=true",
      expect.objectContaining({ credentials: "include", method: "GET" })
    );
    expect(fetchMock).toHaveBeenNthCalledWith(2,
      "/api/local-chat/runs/run-1/safety-receipt",
      expect.objectContaining({ credentials: "include", method: "GET" })
    );
  });

  it("surfaces only a permanently closed EventSource as terminal loss", () => {
    let source: FakeEventSource | null = null;
    class FakeEventSource {
      readonly CLOSED = 2;
      readyState = 0;
      private readonly listeners = new Map<string, Array<() => void>>();

      constructor(readonly url: string, readonly options?: EventSourceInit) { source = this; }
      addEventListener(type: string, listener: () => void) {
        this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
      }
      close = vi.fn();
      emitError() { for (const listener of this.listeners.get("error") ?? []) listener(); }
    }
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
    const stream = createConversationApi("csrf", "http://127.0.0.1:8765").openRunEvents("run/1");
    const onTerminalError = vi.fn();

    stream.onTerminalError(onTerminalError);
    expect(source!.url).toBe("http://127.0.0.1:8765/api/local-chat/runs/run%2F1/events?follow=true");
    expect(source!.options).toEqual({ withCredentials: true });
    source!.emitError();
    expect(onTerminalError).not.toHaveBeenCalled();
    source!.readyState = source!.CLOSED;
    source!.emitError();
    expect(onTerminalError).toHaveBeenCalledTimes(1);
  });
});
