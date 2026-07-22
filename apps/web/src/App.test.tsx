import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CorvusApi, Outcome, Project, Workflow } from "./api";
import type { PlatformApi } from "./auth/authApi";
import { PlatformApp } from "./PlatformApp";
import { MemoryStorage } from "./test/memoryStorage";

const PROJECT: Project = {
  id: "project-1",
  name: "Launch control",
  tenant_id: "local",
  created_at: "2026-07-14T00:00:00Z"
};
const OUTCOME: Outcome = {
  id: "outcome-1",
  project_id: PROJECT.id,
  version: 1,
  title: "Browser delivery",
  acceptance_criteria: ["No reconnect storm"],
  created_at: "2026-07-14T00:00:00Z"
};
const WORKFLOW: Workflow = {
  id: "workflow-1",
  outcome_id: OUTCOME.id,
  name: "Browser flow",
  status: "running",
  created_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:00:00Z"
};

function hostedApi(): PlatformApi {
  const workspace = {
    id: "33333333-3333-4333-8333-333333333333",
    name: "Local workspace",
    workspace_kind: "individual" as const,
    status: "active" as const,
    created_at: "2026-07-17T00:00:00Z",
    updated_at: "2026-07-17T00:00:00Z",
    version: 1
  };
  return {
    applySync: vi.fn().mockResolvedValue({ acknowledged_cursor: 0, results: [] }),
    createWorkspace: vi.fn(),
    getSession: vi.fn().mockResolvedValue({
      account_id: "11111111-1111-4111-8111-111111111111",
      principal_id: "22222222-2222-4222-8222-222222222222",
      email: "operator@example.com",
      experience_kind: "developer",
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
    getWorkspace: vi.fn().mockResolvedValue(workspace),
    listWorkspaces: vi.fn().mockResolvedValue([workspace]),
    logout: vi.fn(),
    refreshSession: vi.fn(),
    startGoogle: vi.fn(),
    updateOnboarding: vi.fn()
  };
}

function renderApp(api: CorvusApi, preferenceStorage: Storage) {
  return render(
    <PlatformApp
      hostedApi={hostedApi()}
      locationHostname="localhost"
      loopbackApi={api}
      preferenceStorage={preferenceStorage}
    />
  );
}

function fakeApi(projects: Project[] = []): CorvusApi {
  return {
    session: vi.fn().mockRejectedValue(new Error("authentication_required")),
    pair: vi.fn().mockResolvedValue(undefined),
    listProjects: vi.fn().mockResolvedValue(projects),
    listRepositories: vi.fn().mockResolvedValue(projects.map((project) => ({
      id: project.id,
      tenant_id: project.tenant_id,
      display_name: project.name,
      path: `C:\\work\\${project.id}`,
      remote_slug: null,
      default_branch: "main",
      created_at: project.created_at,
      updated_at: project.created_at,
      snapshot: {
        branch: "main",
        head_sha: "a".repeat(40),
        clean: true,
        ahead: 0,
        behind: 0,
        health: "healthy",
        refreshed_at: project.created_at
      }
    }))),
    getGitHubAuthStatus: vi.fn().mockResolvedValue({ hostname: "github.com", authenticated: false }),
    authenticateGitHub: vi.fn().mockResolvedValue({ hostname: "github.com", authenticated: true }),
    listGitHubRepositories: vi.fn().mockResolvedValue([]),
    connectGitHubRepository: vi.fn(),
    createEmptyRepository: vi.fn(),
    registerRepository: vi.fn(),
    refreshRepository: vi.fn(),
    removeRepository: vi.fn(),
    createRepositoryRun: vi.fn(),
    discardRepositoryRun: vi.fn(),
    listLocalProviders: vi.fn().mockResolvedValue([{
      id: "codex",
      label: "OpenAI Codex",
      runtime: "local",
      status: "ready",
      status_label: "CLI and login verified",
      models: [{ id: "gpt-5.6-sol", label: "GPT-5.6 Sol", recommended: true }],
      thinking_levels: ["low", "medium", "high", "xhigh"],
      supports_mcp: true
    }]),
    getLocalSafetyPreview: vi.fn(),
    listLocalRuns: vi.fn().mockResolvedValue([]),
    startLocalRun: vi.fn(),
    getLocalRun: vi.fn(),
    listLocalRunEvents: vi.fn().mockResolvedValue([]),
    listLocalRunEvidence: vi.fn().mockResolvedValue([]),
    cancelLocalRun: vi.fn(),
    retryLocalRun: vi.fn(),
    discardLocalRun: vi.fn(),
    listPortableSkills: vi.fn().mockResolvedValue([]),
    listSkillImportSources: vi.fn().mockResolvedValue([]),
    previewSkillImport: vi.fn(),
    importPortableSkill: vi.fn(),
    activatePortableSkill: vi.fn(),
    archivePortableSkill: vi.fn(),
    listLocalSchedules: vi.fn().mockResolvedValue([]),
    createLocalSchedule: vi.fn(),
    runLocalScheduleNow: vi.fn(),
    pauseLocalSchedule: vi.fn(),
    resumeLocalSchedule: vi.fn(),
    archiveLocalSchedule: vi.fn(),
    getRunChanges: vi.fn(),
    getContribution: vi.fn(),
    prepareContribution: vi.fn(),
    publishContribution: vi.fn(),
    createProject: vi.fn().mockResolvedValue(PROJECT),
    listOutcomes: vi.fn().mockResolvedValue([]),
    createOutcome: vi.fn(),
    listWorkflows: vi.fn().mockResolvedValue([]),
    createWorkflow: vi.fn(),
    getWorkflow: vi.fn(),
    listWorkItems: vi.fn().mockResolvedValue([]),
    listEffects: vi.fn().mockResolvedValue([]),
    getBudget: vi.fn(),
    setBudget: vi.fn(),
    listArtifacts: vi.fn().mockResolvedValue([]),
    listConversation: vi.fn().mockResolvedValue([]),
    startWorkflow: vi.fn(),
    pauseWorkflow: vi.fn(),
    resumeWorkflow: vi.fn(),
    cancelWorkflow: vi.fn(),
    setWorkflowKillSwitch: vi.fn(),
    runNext: vi.fn(),
    approveEffect: vi.fn(),
    rejectEffect: vi.fn(),
    listTeams: vi.fn().mockResolvedValue([]),
    createTeam: vi.fn().mockResolvedValue({
      id: "team-1",
      project_id: PROJECT.id,
      name: "Operators",
      created_at: "2026-07-14T00:00:00Z"
    }),
    listProviders: vi.fn().mockResolvedValue([]),
    createProvider: vi.fn(),
    evaluateAutonomy: vi.fn(),
    listMemories: vi.fn().mockResolvedValue([]),
    storeMemory: vi.fn().mockResolvedValue({
      id: "memory-1",
      project_id: PROJECT.id,
      scope: "project",
      version: 1,
      content: "Remember the durable boundary.",
      provenance: "user:operator-1",
      status: "active",
      created_at: "2026-07-14T00:00:00Z"
    }),
    retrieveMemory: vi.fn().mockResolvedValue([]),
    listSkills: vi.fn().mockResolvedValue([]),
    createSkill: vi.fn(),
    activateSkill: vi.fn(),
    listRoutines: vi.fn().mockResolvedValue([]),
    createRoutine: vi.fn(),
    runRoutine: vi.fn(),
    listOfflineIntents: vi.fn().mockResolvedValue([]),
    listChannelEvents: vi.fn().mockResolvedValue([])
  };
}

describe("Corvus operator console", () => {
  let preferenceStorage: MemoryStorage;

  beforeEach(() => {
    preferenceStorage = new MemoryStorage();
    preferenceStorage.setItem(
      "corvus.workspace-preference",
      JSON.stringify({
        version: 1,
        experience: "developer",
        scope: "personal",
        runtime: "local",
        onboardingComplete: true
      })
    );
  });

  it("pairs once and reveals the repository workspace without a project rail", async () => {
    const api = fakeApi([PROJECT]);
    api.session = vi.fn()
      .mockRejectedValueOnce(new Error("authentication_required"))
      .mockResolvedValue({
        csrf_token: "paired-csrf",
        username: "paired-operator",
        user_id: "operator-1",
        tenant_id: "local",
        expires_at: "2026-07-15T00:00:00Z"
      });
    const user = userEvent.setup();
    renderApp(api, preferenceStorage);

    await user.type(await screen.findByLabelText("One-time pairing value"), "ephemeral-pairing-value");
    await user.click(screen.getByRole("button", { name: "Pair this browser" }));
    await user.click(await screen.findByRole("link", { name: "Projects" }));

    expect(api.pair).toHaveBeenCalledWith("ephemeral-pairing-value");
    expect(api.session).toHaveBeenCalledTimes(2);
    expect(screen.getByText("paired-operator")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Projects" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Launch control" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Add project" })).toBeVisible();
  });

  it("uses one local left navigation and omits permanent secondary rails", async () => {
    const api = fakeApi([PROJECT]);
    api.session = vi.fn().mockResolvedValue({
      csrf_token: "csrf",
      username: "operator",
      user_id: "operator-1",
      tenant_id: "local",
      expires_at: "2026-07-15T00:00:00Z"
    });
    const { container } = renderApp(api, preferenceStorage);

    const sidebar = await screen.findByRole("complementary", { name: "Local workspace" });
    expect(sidebar).toContainElement(screen.getByRole("navigation", { name: "Local runtime navigation" }));
    expect(sidebar.querySelectorAll("nav svg[aria-hidden='true']")).toHaveLength(6);
    expect(container.querySelector(".project-rail")).toBeNull();
    expect(screen.queryByLabelText("Work item details")).not.toBeInTheDocument();
    expect(screen.getByText("Developer / Individual")).toBeVisible();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute("href", "#main-content");
    expect(container.querySelector("main#main-content")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("main", { name: "Threads" })).toBeVisible();
    expect(container.querySelector(".local-topbar")).toBeNull();
  });

  it.each([
    ["everyday", "personal", ["Conversations", "Projects", "Activity", "Schedule", "Skills", "Settings"]],
    ["developer", "personal", ["Projects", "Runs", "Schedule", "Skills", "Threads", "Settings"]],
    ["everyday", "team", ["Conversations", "Projects", "Activity", "Schedule", "Skills", "Settings"]],
    ["developer", "team", ["Projects", "Runs", "Schedule", "Skills", "Threads", "Settings"]]
  ] as const)("provides accessible local routes for the %s %s profile", async (experience, scope, labels) => {
    preferenceStorage.setItem("corvus.workspace-preference", JSON.stringify({
      version: 1,
      experience,
      scope,
      runtime: "local",
      onboardingComplete: true
    }));
    const api = fakeApi([PROJECT]);
    api.session = vi.fn().mockResolvedValue({
      csrf_token: "csrf",
      username: "operator",
      user_id: "operator-1",
      tenant_id: "local",
      expires_at: "2026-07-15T00:00:00Z"
    });
    renderApp(api, preferenceStorage);
    const user = userEvent.setup();

    const navigation = await screen.findByRole("navigation", { name: "Local runtime navigation" });
    expect(screen.getByText(new RegExp(`${experience}.*${scope === "personal" ? "individual" : "team"}`, "i"))).toBeVisible();
    expect(within(navigation).getAllByRole("link").map((link) => link.textContent)).toEqual(labels);
    for (const label of labels) {
      await user.click(within(navigation).getByRole("link", { name: label }));
      expect(screen.getByRole("main", { name: label })).toBeVisible();
    }
  });

  it("does not expose unfinished everyday work routes", async () => {
    preferenceStorage.setItem("corvus.workspace-preference", JSON.stringify({
      version: 1,
      experience: "everyday",
      scope: "personal",
      runtime: "local",
      onboardingComplete: true
    }));
    const api = fakeApi([PROJECT]);
    api.session = vi.fn().mockResolvedValue({
      csrf_token: "csrf",
      username: "operator",
      user_id: "operator-1",
      tenant_id: "local",
      expires_at: "2026-07-15T00:00:00Z"
    });
    renderApp(api, preferenceStorage);

    const navigation = await screen.findByRole("navigation", { name: "Local runtime navigation" });
    expect(within(navigation).queryByRole("link", { name: "My Work" })).not.toBeInTheDocument();
    expect(within(navigation).queryByRole("link", { name: "Files" })).not.toBeInTheDocument();
    expect(screen.getByRole("main", { name: "Conversations" })).toBeVisible();
  });

  it("consumes an ephemeral desktop pairing fragment without rendering the secret", async () => {
    const api = fakeApi([PROJECT]);
    api.session = vi.fn()
      .mockRejectedValueOnce(new Error("authentication_required"))
      .mockResolvedValue({
        csrf_token: "desktop-csrf",
        username: "desktop-operator",
        user_id: "operator-1",
        tenant_id: "local",
        expires_at: "2026-07-15T00:00:00Z"
      });
    window.history.replaceState(null, "", "/#pair=desktop-ephemeral-token");

    renderApp(api, preferenceStorage);

    await waitFor(() => expect(api.pair).toHaveBeenCalledWith("desktop-ephemeral-token"));
    expect(await screen.findByRole("heading", { name: "What would you like to start?" })).toBeVisible();
    expect(screen.queryByDisplayValue("desktop-ephemeral-token")).not.toBeInTheDocument();
    expect(window.location.hash).toBe("");
  });

  it("does not expose project creation as a second local navigation rail", async () => {
    const api = fakeApi();
    api.session = vi.fn().mockResolvedValue({
      csrf_token: "csrf",
      username: "operator",
      user_id: "operator-1",
      tenant_id: "local",
      expires_at: "2026-07-15T00:00:00Z"
    });
    const { container } = renderApp(api, preferenceStorage);

    await screen.findByRole("navigation", { name: "Local runtime navigation" });
    expect(screen.queryByRole("button", { name: "New project" })).not.toBeInTheDocument();
    expect(container.querySelector(".project-rail")).toBeNull();
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("replaces the app sidebar in Settings and returns to the main workspace", async () => {
    const api = fakeApi([PROJECT]);
    api.session = vi.fn().mockResolvedValue({
      csrf_token: "csrf",
      username: "operator",
      user_id: "operator-1",
      tenant_id: "local",
      expires_at: "2026-07-15T00:00:00Z"
    });
    const user = userEvent.setup();
    renderApp(api, preferenceStorage);

    await user.click(await screen.findByRole("link", { name: "Settings" }));

    expect(screen.queryByRole("complementary", { name: "Local workspace" })).not.toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Settings categories" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Back to app/i }));
    expect(await screen.findByRole("complementary", { name: "Local workspace" })).toBeVisible();
    expect(screen.getByRole("main", { name: "Threads" })).toBeVisible();
  });

  it("starts a supervised repository run from the Runs workspace", async () => {
    const api = fakeApi([PROJECT]);
    api.session = vi.fn().mockResolvedValue({
      csrf_token: "csrf",
      username: "operator",
      user_id: "operator-1",
      tenant_id: "local",
      expires_at: "2026-07-15T00:00:00Z"
    });
    api.getLocalSafetyPreview = vi.fn().mockResolvedValue({ policy_digest: "b".repeat(64) });
    const run = {
      id: "run-1",
      tenant_id: "local",
      repository_id: PROJECT.id,
      base_sha: "a".repeat(40),
      task: "Add the demo feature",
      provider: "codex",
      model: null,
      effort: "high",
      mode: "build",
      safety_digest: "b".repeat(64),
      skill_version_id: null,
      schedule_id: null,
      occurrence_key: null,
      output_policy: "prepare_contribution",
      retry_of_run_id: null,
      status: "running",
      created_at: "2026-07-18T00:00:00Z",
      updated_at: "2026-07-18T00:00:00Z",
      started_at: "2026-07-18T00:00:00Z",
      finished_at: null
    };
    api.startLocalRun = vi.fn().mockResolvedValue(run);
    api.getLocalRun = vi.fn().mockResolvedValue(run);
    const user = userEvent.setup();
    renderApp(api, preferenceStorage);
    await user.click(await screen.findByRole("link", { name: "Runs" }));

    await user.click(await screen.findByRole("button", { name: "New run" }));
    await user.type(screen.getByLabelText("Task"), "Add the demo feature");
    await user.click(screen.getByRole("button", { name: "Start supervised run" }));

    await waitFor(() => expect(api.startLocalRun).toHaveBeenCalledWith(expect.objectContaining({
      repositoryId: PROJECT.id,
      task: "Add the demo feature",
      safetyDigest: "b".repeat(64)
    })));
  });

  it("creates a timezone-aware schedule from the Schedule workspace", async () => {
    const api = fakeApi([PROJECT]);
    api.session = vi.fn().mockResolvedValue({
      csrf_token: "csrf",
      username: "operator",
      user_id: "operator-1",
      tenant_id: "local",
      expires_at: "2026-07-15T00:00:00Z"
    });
    api.getLocalSafetyPreview = vi.fn().mockResolvedValue({ policy_digest: "c".repeat(64) });
    api.createLocalSchedule = vi.fn().mockResolvedValue({
      id: "schedule-1",
      name: "Weekday review",
      task: "Review changes",
      status: "active",
      version: 1,
      recurrence: { kind: "weekdays", local_time: "09:00:00", weekdays: [] },
      next_run_at: "2026-07-20T09:00:00Z",
      mode: "chat",
      timezone: "UTC"
    });

    const user = userEvent.setup();
    renderApp(api, preferenceStorage);
    await user.click(await screen.findByRole("link", { name: "Schedule" }));
    await user.click(await screen.findByRole("button", { name: "New schedule" }));
    await user.type(screen.getByLabelText("Name"), "Weekday review");
    await user.type(screen.getByLabelText("Task"), "Review changes");
    await user.selectOptions(screen.getByLabelText("Cadence"), "weekdays");
    await user.click(screen.getByRole("button", { name: "Advanced options" }));
    await user.clear(screen.getByLabelText("Timezone"));
    await user.type(screen.getByLabelText("Timezone"), "UTC");
    await user.click(screen.getByRole("button", { name: "Create schedule" }));

    await waitFor(() => expect(api.createLocalSchedule).toHaveBeenCalledWith(expect.objectContaining({
      repositoryId: PROJECT.id,
      timezone: "UTC",
      recurrence: expect.objectContaining({ kind: "weekdays" })
    })));
  });

  it("reviews and imports a digest-pinned cross-agent skill", async () => {
    const api = fakeApi([PROJECT]);
    api.session = vi.fn().mockResolvedValue({
      csrf_token: "csrf",
      username: "operator",
      user_id: "operator-1",
      tenant_id: "local",
      expires_at: "2026-07-15T00:00:00Z"
    });
    const candidate = {
      id: "a".repeat(64),
      source: "claude",
      name: "release-checklist",
      path: "C:\\Users\\me\\.claude\\skills\\release-checklist",
      kind: "package"
    };
    const preview = {
      candidate,
      name: "release-checklist",
      description: "Verify tests and summarize risks.",
      digest: "d".repeat(64),
      compatibility: "ready",
      findings: [],
      files: ["SKILL.md"],
      duplicate: "none"
    };
    api.listSkillImportSources = vi.fn().mockResolvedValue([candidate]);
    api.previewSkillImport = vi.fn().mockResolvedValue(preview);
    api.importPortableSkill = vi.fn().mockResolvedValue({
      id: "skill-1",
      tenant_id: "local",
      name: preview.name,
      description: preview.description,
      version: 1,
      digest: preview.digest,
      source: "claude",
      source_path: candidate.path,
      package_path: "skills/skill-1",
      status: "draft",
      findings: [],
      created_at: "2026-07-18T00:00:00Z"
    });
    const user = userEvent.setup();
    renderApp(api, preferenceStorage);

    await user.click(await screen.findByRole("link", { name: "Skills" }));
    await user.click(await screen.findByRole("button", { name: /release-checklist/i }));
    expect(await screen.findByRole("dialog", { name: "release-checklist" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Import as draft" }));

    await waitFor(() => expect(api.importPortableSkill).toHaveBeenCalledWith(candidate.id, preview.digest));
    expect(await screen.findByText("Verify tests and summarize risks.")).toBeVisible();
  });
});
