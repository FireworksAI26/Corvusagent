import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { CorvusApi, Project } from "./api";
import { AuthProvider } from "./auth/AuthProvider";
import type { PlatformApi, Workspace } from "./auth/authApi";
import { PlatformApp } from "./PlatformApp";
import { SyncProvider } from "./sync/SyncProvider";
import { MemoryStorage } from "./test/memoryStorage";

const WORKSPACES: Workspace[] = [
  {
    id: "33333333-3333-4333-8333-333333333333",
    name: "Field desk",
    workspace_kind: "individual",
    status: "active",
    created_at: "2026-07-17T00:00:00Z",
    updated_at: "2026-07-17T00:00:00Z",
    version: 1
  },
  {
    id: "44444444-4444-4444-8444-444444444444",
    name: "Operations",
    workspace_kind: "team",
    status: "active",
    created_at: "2026-07-17T00:00:00Z",
    updated_at: "2026-07-17T00:00:00Z",
    version: 1
  }
];

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function hostedApi(
  workspaces: Workspace[],
  experienceKind: "everyday" | "developer" | null = "developer"
): PlatformApi {
  return {
    applySync: vi.fn().mockResolvedValue({ acknowledged_cursor: 0, results: [] }),
    createWorkspace: vi.fn(),
    getSession: vi.fn().mockResolvedValue({
      account_id: "11111111-1111-4111-8111-111111111111",
      principal_id: "22222222-2222-4222-8222-222222222222",
      email: "person@example.com",
      experience_kind: experienceKind,
      account_version: 1,
      session_version: 1,
      csrf_token: "hosted-csrf"
    }),
    getSyncPage: vi.fn().mockResolvedValue({
      requested_cursor: 0,
      next_cursor: 0,
      high_watermark: 0,
      earliest_retained_sequence: 1,
      changes: [],
      has_more: false
    }),
    getWorkspace: vi.fn().mockImplementation(async (id: string) => workspaces.find((workspace) => workspace.id === id)!),
    listWorkspaces: vi.fn().mockResolvedValue(workspaces),
    logout: vi.fn(),
    refreshSession: vi.fn(),
    startGoogle: vi.fn(),
    updateOnboarding: vi.fn()
  };
}

function readyLoopback(): CorvusApi {
  return {
    session: vi.fn().mockResolvedValue({
      csrf_token: "local-csrf",
      username: "operator",
      user_id: "operator-1",
      tenant_id: "local",
      expires_at: "2026-07-18T00:00:00Z"
    }),
    listProjects: vi.fn().mockResolvedValue([]),
    listOutcomes: vi.fn().mockResolvedValue([]),
    listTeams: vi.fn().mockResolvedValue([]),
    listProviders: vi.fn().mockResolvedValue([]),
    listMemories: vi.fn().mockResolvedValue([]),
    listSkills: vi.fn().mockResolvedValue([]),
    listRoutines: vi.fn().mockResolvedValue([]),
    listOfflineIntents: vi.fn().mockResolvedValue([]),
    listChannelEvents: vi.fn().mockResolvedValue([])
  } as unknown as CorvusApi;
}

describe("adaptive Corvus bootstrap", () => {
  it("treats a legacy preference only as onboarding preselection", async () => {
    const storage = new MemoryStorage();
    storage.setItem("corvus.workspace-preference", JSON.stringify({
      version: 1,
      experience: "developer",
      scope: "team",
      runtime: "local",
      onboardingComplete: true
    }));
    const loopback = readyLoopback();

    render(
      <PlatformApp
        hostedApi={hostedApi([WORKSPACES[0]], null)}
        locationHostname="corvus.example"
        loopbackApi={loopback}
        preferenceStorage={storage}
      />
    );

    expect(await screen.findByRole("heading", { name: "Set up your workspace" })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Developer/ })).toBeChecked();
    expect(loopback.session).not.toHaveBeenCalled();
  });

  it("hands hosted users to loopback without sending hosted authority to the local API", async () => {
    const loopback = readyLoopback();

    render(
      <PlatformApp
        hostedApi={hostedApi([WORKSPACES[0]])}
        locationHostname="corvus-platform.example"
        loopbackApi={loopback}
        preferenceStorage={new MemoryStorage()}
      />
    );

    expect(await screen.findByRole("heading", { name: "Open Corvus on this computer." })).toBeVisible();
    expect(screen.getByRole("link", { name: "Open local Corvus" })).toHaveAttribute("href", "http://127.0.0.1:8080/");
    expect(screen.getByText(/hosted page never receives your local session/i)).toBeVisible();
    expect(loopback.session).not.toHaveBeenCalled();
  });

  it("keeps experience and scope read-only after explicit workspace selection", async () => {
    const user = userEvent.setup();
    const loopback = readyLoopback();

    render(
      <PlatformApp
        hostedApi={hostedApi(WORKSPACES)}
        locationHostname="corvus.example"
        loopbackApi={loopback}
        preferenceStorage={new MemoryStorage()}
      />
    );

    await user.click(await screen.findByRole("button", { name: /Field desk/ }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Open Corvus on this computer." })).toBeVisible());
    expect(await screen.findByRole("link", { name: "Projects" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /work style/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /team workspace/i })).not.toBeInTheDocument();
    expect(screen.getAllByText("Field desk").length).toBeGreaterThan(0);
    expect(loopback.session).not.toHaveBeenCalled();

    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(screen.getByRole("heading", { name: "General" })).toBeVisible();
  });

  it("cleans up a matching legacy candidate once server-backed identity is ready", async () => {
    const storage = new MemoryStorage();
    storage.setItem("corvus.workspace-preference", JSON.stringify({
      version: 1,
      experience: "developer",
      scope: "personal",
      runtime: "local",
      onboardingComplete: true
    }));

    render(
      <PlatformApp
        hostedApi={hostedApi([WORKSPACES[0]])}
        locationHostname="corvus.example"
        preferenceStorage={storage}
      />
    );

    expect(await screen.findByRole("heading", { name: "Open Corvus on this computer." })).toBeVisible();
    await waitFor(() => expect(storage.getItem("corvus.workspace-preference")).toBeNull());
  });

  it("lets a returning user explicitly dismiss a mismatched legacy candidate", async () => {
    const storage = new MemoryStorage();
    storage.setItem("corvus.workspace-preference", JSON.stringify({
      version: 1,
      experience: "developer",
      scope: "team",
      runtime: "local",
      onboardingComplete: true
    }));
    const user = userEvent.setup();

    render(
      <PlatformApp
        hostedApi={hostedApi([WORKSPACES[0]])}
        locationHostname="corvus.example"
        preferenceStorage={storage}
      />
    );

    await user.click(await screen.findByRole("button", { name: "Open workspace identity" }));
    await user.click(screen.getByRole("button", { name: "Dismiss previous setup" }));
    expect(storage.getItem("corvus.workspace-preference")).toBeNull();
  });

  it("ignores a late project load from workspace A after workspace B is confirmed", async () => {
    const firstProjects = deferred<Project[]>();
    const secondProjects = deferred<Project[]>();
    const projectA: Project = { id: "project-a", name: "Project A", tenant_id: "local", created_at: "2026-07-17T00:00:00Z" };
    const projectB: Project = { id: "project-b", name: "Project B", tenant_id: "local", created_at: "2026-07-17T00:00:00Z" };
    const loopback = {
      ...readyLoopback(),
      listProjects: vi.fn()
        .mockReturnValueOnce(firstProjects.promise)
        .mockReturnValueOnce(secondProjects.promise)
    } as unknown as CorvusApi;
    const user = userEvent.setup();

    render(
      <AuthProvider api={hostedApi(WORKSPACES)}>
        <SyncProvider api={hostedApi(WORKSPACES)}>
          <App authorityMode="hosted" api={loopback} locationHostname="localhost" preferenceStorage={new MemoryStorage()} />
        </SyncProvider>
      </AuthProvider>
    );

    await user.click(await screen.findByRole("button", { name: /Field desk/ }));
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Authorized workspace" }),
      WORKSPACES[1].id
    );
    await act(async () => { secondProjects.resolve([projectB]); });
    await user.click(screen.getByRole("button", { name: "Projects" }));
    expect(await screen.findByRole("button", { name: /Project B/ })).toBeVisible();
    await act(async () => { firstProjects.resolve([projectA]); });

    await waitFor(() => expect(screen.queryByRole("button", { name: /Project A/ })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Project B/ })).toBeVisible();
  });
});
