import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CorvusApi, Project } from "./api";
import { AuthApiError, type PlatformApi, type Workspace } from "./auth/authApi";
import { PlatformApp } from "./PlatformApp";
import { MemoryStorage } from "./test/memoryStorage";

const SESSION = {
  account_id: "11111111-1111-4111-8111-111111111111",
  principal_id: "22222222-2222-4222-8222-222222222222",
  email: "person@example.com",
  experience_kind: "developer" as const,
  account_version: 1,
  session_version: 1,
  csrf_token: "csrf-opaque"
};
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
const LOCAL_PROJECT: Project = {
  id: "local-project",
  name: "Local launch control",
  tenant_id: "local",
  created_at: "2026-07-17T00:00:00Z"
};

function readyLoopbackApi(): CorvusApi {
  return {
    session: vi.fn().mockResolvedValue({
      csrf_token: "legacy-csrf",
      username: "real-operator",
      user_id: "operator-1",
      tenant_id: "local",
      expires_at: "2026-07-18T00:00:00Z"
    }),
    listProjects: vi.fn().mockResolvedValue([LOCAL_PROJECT]),
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

function platformApi(overrides: Partial<PlatformApi> = {}): PlatformApi {
  return {
    applySync: vi.fn().mockResolvedValue({ acknowledged_cursor: 0, results: [] }),
    createWorkspace: vi.fn(),
    getSession: vi.fn().mockResolvedValue(SESSION),
    getSyncPage: vi.fn().mockResolvedValue({
      requested_cursor: 0,
      next_cursor: 0,
      high_watermark: 0,
      earliest_retained_sequence: 1,
      changes: [],
      has_more: false
    }),
    getWorkspace: vi.fn().mockImplementation(async (id: string) => WORKSPACES.find((workspace) => workspace.id === id)!),
    listWorkspaces: vi.fn().mockResolvedValue(WORKSPACES),
    logout: vi.fn(),
    refreshSession: vi.fn(),
    startGoogle: vi.fn(),
    updateOnboarding: vi.fn(),
    ...overrides
  };
}

describe("PlatformApp composition", () => {
  it("does not contact the loopback API before hosted authentication succeeds", async () => {
    const hostedApi = platformApi({
      getSession: vi.fn().mockRejectedValue(new AuthApiError(401, "session_required"))
    });
    const loopbackApi = { session: vi.fn() } as unknown as CorvusApi;

    render(<PlatformApp hostedApi={hostedApi} locationHostname="corvus.example" loopbackApi={loopbackApi} preferenceStorage={new MemoryStorage()} />);

    expect(await screen.findByRole("button", { name: "Continue with Google" })).toBeVisible();
    expect(loopbackApi.session).not.toHaveBeenCalled();
    expect(hostedApi.listWorkspaces).not.toHaveBeenCalled();
  });

  it("uses the real non-injected loopback composition and never boots hosted identity", async () => {
    const hostedApi = platformApi();

    render(<PlatformApp hostedApi={hostedApi} preferenceStorage={new MemoryStorage()} />);

    expect(await screen.findByLabelText("One-time pairing value")).toBeVisible();
    expect(hostedApi.getSession).not.toHaveBeenCalled();
    expect(hostedApi.listWorkspaces).not.toHaveBeenCalled();
  });

  it("uses only the successful legacy session on loopback and preserves hosted migration input", async () => {
    const hostedApi = platformApi();
    const loopbackApi = readyLoopbackApi();
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
        hostedApi={hostedApi}
        locationHostname="127.0.0.1"
        loopbackApi={loopbackApi}
        preferenceStorage={storage}
      />
    );

    expect(await screen.findByRole("heading", { name: "New thread" })).toBeVisible();
    expect(screen.getByRole("complementary", { name: "Local workspace" })).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "Run options" }));
    expect(screen.getByRole("combobox", { name: "Agent provider" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Local launch control/ })).not.toBeInTheDocument();
    expect(screen.queryByText("local-runtime@corvus.invalid")).not.toBeInTheDocument();
    expect(storage.getItem("corvus.workspace-preference")).not.toBeNull();
    expect(hostedApi.getSession).not.toHaveBeenCalled();
    expect(hostedApi.listWorkspaces).not.toHaveBeenCalled();
  });

  it("shows the selected everyday team profile in the local desktop shell", async () => {
    const storage = new MemoryStorage();
    storage.setItem("corvus.workspace-preference", JSON.stringify({
      version: 1,
      experience: "everyday",
      scope: "team",
      runtime: "local",
      onboardingComplete: true
    }));

    render(
      <PlatformApp
        hostedApi={platformApi()}
        locationHostname="127.0.0.1"
        loopbackApi={readyLoopbackApi()}
        preferenceStorage={storage}
      />
    );

    expect(await screen.findByRole("heading", { name: "What can Corvus help you finish?" })).toBeVisible();
    expect(screen.getByRole("complementary", { name: "Local workspace" })).toBeVisible();
    expect(screen.getByRole("button", { name: "New conversation" })).toBeVisible();
  });

  it("refreshes onboarding conflict truth and retries with the new exact version", async () => {
    const getSession = vi.fn()
      .mockResolvedValueOnce({ ...SESSION, experience_kind: null, account_version: 1 })
      .mockResolvedValue({ ...SESSION, experience_kind: null, account_version: 2 });
    const updateOnboarding = vi.fn()
      .mockRejectedValueOnce(new AuthApiError(409, "account_version_conflict"))
      .mockResolvedValue({ experience_kind: "everyday", version: 3 });
    const api = platformApi({ getSession, listWorkspaces: vi.fn().mockResolvedValue([]), updateOnboarding });
    const user = userEvent.setup();
    render(
      <PlatformApp
        hostedApi={api}
        locationHostname="corvus.example"
        loopbackApi={{} as CorvusApi}
        preferenceStorage={new MemoryStorage()}
      />
    );
    await user.click(await screen.findByRole("radio", { name: /Everyday/ }));
    await user.click(screen.getByRole("radio", { name: /Individual/ }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/conflict/i);
    await waitFor(() => expect(getSession).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("radio", { name: /Everyday/ })).toBeChecked();

    await user.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(updateOnboarding).toHaveBeenLastCalledWith(
      { experience_kind: "everyday", expected_version: 2 },
      "csrf-opaque"
    ));
  });

  it("refreshes hosted account truth when a Settings experience save conflicts", async () => {
    const getSession = vi.fn()
      .mockResolvedValueOnce(SESSION)
      .mockResolvedValue({ ...SESSION, account_version: 2 });
    const updateOnboarding = vi.fn().mockRejectedValue(
      new AuthApiError(409, "account_version_conflict"),
    );
    const hostedApi = platformApi({ getSession, updateOnboarding });
    const user = userEvent.setup();
    render(
      <PlatformApp
        hostedApi={hostedApi}
        locationHostname="corvus.example"
        loopbackApi={readyLoopbackApi()}
        preferenceStorage={new MemoryStorage()}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /Field desk/ }));
    await user.click(await screen.findByRole("link", { name: "Settings" }));
    await user.selectOptions(screen.getByLabelText("Experience"), "everyday");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(getSession).toHaveBeenCalledTimes(2));
    expect(updateOnboarding).toHaveBeenCalledWith(
      { experience_kind: "everyday", expected_version: 1 },
      "csrf-opaque",
    );
  });
});
