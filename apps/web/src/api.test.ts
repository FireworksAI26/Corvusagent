import { afterEach, describe, expect, it, vi } from "vitest";

import { createCorvusApi } from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status
  });
}

describe("legacy Corvus transport authority", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses credentialed legacy session truth and its CSRF token for mutations", async () => {
    const requests: Request[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const request = input as Request;
      requests.push(request);
      if (request.url.endsWith("/api/auth/session")) {
        return jsonResponse({
          csrf_token: "transport-csrf",
          username: "operator",
          user_id: "operator-1",
          tenant_id: "local",
          expires_at: "2026-07-18T00:00:00Z"
        });
      }
      return jsonResponse({
        id: "project-1",
        name: "Launch control",
        tenant_id: "local",
        created_at: "2026-07-17T00:00:00Z"
      });
    }));
    const api = createCorvusApi("http://127.0.0.1:8080");

    await api.session();
    await api.createProject("Launch control");

    expect(requests[0].url).toBe("http://127.0.0.1:8080/api/auth/session");
    expect(requests[0].credentials).toBe("include");
    expect(requests[1].headers.get("X-CSRF-Token")).toBe("transport-csrf");
  });

  it("exchanges a pairing value, reloads the real session, and consumes its CSRF token", async () => {
    const requests: Request[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const request = input as Request;
      requests.push(request);
      if (request.url.endsWith("/api/auth/pair")) return new Response(null, { status: 204 });
      if (request.url.endsWith("/api/auth/session")) {
        return jsonResponse({
          csrf_token: "paired-transport-csrf",
          username: "operator",
          user_id: "operator-1",
          tenant_id: "local",
          expires_at: "2026-07-18T00:00:00Z"
        });
      }
      return jsonResponse({
        id: "project-1",
        name: "Launch control",
        tenant_id: "local",
        created_at: "2026-07-17T00:00:00Z"
      });
    }));
    const api = createCorvusApi("http://127.0.0.1:8080");

    await api.pair("one-time-pairing-value");
    await api.createProject("Launch control");

    expect(requests.map((request) => new URL(request.url).pathname)).toEqual([
      "/api/auth/pair",
      "/api/auth/session",
      "/api/projects"
    ]);
    expect(requests.every((request) => request.credentials === "include")).toBe(true);
    expect(requests[2].headers.get("X-CSRF-Token")).toBe("paired-transport-csrf");
  });

  it("surfaces the safe message from a Corvus error envelope", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      error: { code: "conflict", message: "contribution_not_found" }
    }, 409)));

    await expect(createCorvusApi("http://127.0.0.1:8080").getContribution("run-1"))
      .rejects.toThrow("contribution_not_found");
  });

  it("loads bounded local collections page by page without hiding later records", async () => {
    const requests: Request[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const request = input as Request;
      requests.push(request);
      const offset = new URL(request.url).searchParams.get("offset");
      if (offset === "100") return jsonResponse([{ id: "repo-101" }]);
      return jsonResponse(Array.from({ length: 100 }, (_, index) => ({ id: `repo-${index}` })));
    }));

    const repositories = await createCorvusApi("http://127.0.0.1:8080").listRepositories();

    expect(repositories).toHaveLength(101);
    expect(requests.map((request) => new URL(request.url).searchParams.get("offset")))
      .toEqual(["0", "100"]);
  });

  it("loads bounded run history and evidence page by page", async () => {
    const requests: Request[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const request = input as Request;
      requests.push(request);
      const url = new URL(request.url);
      const offset = url.searchParams.get("offset");
      if (url.pathname.endsWith("/evidence")) {
        return jsonResponse(offset === "100" ? [{ id: "evidence-101" }] : Array.from(
          { length: 100 }, (_, index) => ({ id: `evidence-${index}` })
        ));
      }
      return jsonResponse(offset === "100" ? [{ id: "run-101" }] : Array.from(
        { length: 100 }, (_, index) => ({ id: `run-${index}` })
      ));
    }));
    const api = createCorvusApi("http://127.0.0.1:8080");

    const runs = await api.listLocalRuns();
    const evidence = await api.listLocalRunEvidence("run-1");

    expect(runs).toHaveLength(101);
    expect(evidence).toHaveLength(101);
    expect(requests.map((request) => new URL(request.url).searchParams.get("offset")))
      .toEqual(["0", "100", "0", "100"]);
  });

  it("sends the explicit event page size used by the Runs workspace", async () => {
    const requests: Request[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      requests.push(input as Request);
      return jsonResponse([]);
    }));

    await createCorvusApi("http://127.0.0.1:8080").listLocalRunEvents("run-1", 500, 250);

    const query = new URL(requests[0].url).searchParams;
    expect(query.get("after")).toBe("500");
    expect(query.get("limit")).toBe("250");
  });
});
