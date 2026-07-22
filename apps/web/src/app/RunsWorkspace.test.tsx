import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { LocalProviderCatalogEntry, LocalRepository, LocalRun, LocalSafetyPreview, PortableSkill } from "../api";
import { RunsWorkspace, type RunsApi } from "./RunsWorkspace";

const repository = {
  id: "repo-1",
  tenant_id: "local",
  display_name: "Corvus",
  path: "C:\\work\\corvus",
  remote_slug: "team/corvus",
  default_branch: "main",
  created_at: "2026-07-18T00:00:00Z",
  updated_at: "2026-07-18T00:00:00Z",
  snapshot: { branch: "main", head_sha: "a".repeat(40), clean: true, ahead: 0, behind: 0, health: "healthy", refreshed_at: "2026-07-18T00:00:00Z" }
} as LocalRepository;

const run = {
  id: "run-1",
  tenant_id: "local",
  repository_id: repository.id,
  base_sha: "a".repeat(40),
  task: "Add the demo feature",
  provider: "codex",
  model: "gpt-5.6-sol",
  effort: "high",
  mode: "build",
  safety_digest: "b".repeat(64),
  skill_version_id: "skill-1",
  schedule_id: null,
  occurrence_key: null,
  output_policy: "prepare_contribution",
  retry_of_run_id: null,
  status: "running",
  created_at: "2026-07-18T00:00:00Z",
  updated_at: "2026-07-18T00:00:00Z",
  started_at: "2026-07-18T00:00:00Z",
  finished_at: null
} as LocalRun;

const codex = {
  id: "codex",
  label: "OpenAI Codex",
  runtime: "local",
  status: "ready",
  status_label: "CLI and login verified",
  models: [
    { id: "gpt-5.6-sol", label: "GPT-5.6 Sol", recommended: true },
    { id: "gpt-5.6-terra", label: "GPT-5.6 Terra", recommended: false }
  ],
  thinking_levels: ["low", "medium", "high", "xhigh"],
  supports_mcp: true
} as LocalProviderCatalogEntry;

const skill = {
  id: "skill-1",
  name: "safe-review",
  version: 1,
  status: "active"
} as PortableSkill;

function api(): RunsApi {
  return {
    listRepositories: vi.fn().mockResolvedValue([repository]),
    listLocalProviders: vi.fn().mockResolvedValue([codex]),
    listPortableSkills: vi.fn().mockResolvedValue([skill]),
    getLocalSafetyPreview: vi.fn().mockResolvedValue({ policy_digest: "b".repeat(64) } as LocalSafetyPreview),
    listLocalRuns: vi.fn().mockResolvedValue([]),
    startLocalRun: vi.fn().mockResolvedValue(run),
    getLocalRun: vi.fn().mockResolvedValue(run),
    listLocalRunEvents: vi.fn().mockResolvedValue([]),
    listLocalRunEvidence: vi.fn().mockResolvedValue([]),
    cancelLocalRun: vi.fn().mockResolvedValue({ ...run, status: "cancelled" }),
    retryLocalRun: vi.fn(),
    discardLocalRun: vi.fn(),
    getRunChanges: vi.fn(),
    getContribution: vi.fn().mockRejectedValue(new Error("contribution_not_found")),
    prepareContribution: vi.fn(),
    publishContribution: vi.fn()
  };
}

describe("RunsWorkspace", () => {
  it("keeps demo instructions out of the product and progressively reveals run overrides", async () => {
    const user = userEvent.setup();
    render(<RunsWorkspace api={api()} />);

    expect(screen.queryByText("Recordable demo path")).not.toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "New run" }));
    expect(screen.getByRole("button", { name: "Run options" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Model")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Run options" }));
    expect(screen.getByLabelText("Model")).toBeVisible();
    expect(screen.getByLabelText("Thinking")).toBeVisible();
    expect(screen.getByLabelText("Skill")).toBeVisible();
  });

  it("starts a real supervised repository run", async () => {
    const user = userEvent.setup();
    const client = api();
    render(<RunsWorkspace api={client} />);

    await user.click(await screen.findByRole("button", { name: "New run" }));
    await user.type(screen.getByLabelText("Task"), "Add the demo feature");
    await user.click(screen.getByRole("button", { name: "Run options" }));
    await user.selectOptions(screen.getByLabelText("Model"), "gpt-5.6-terra");
    await user.selectOptions(screen.getByLabelText("Thinking"), "xhigh");
    await user.selectOptions(screen.getByLabelText("Skill"), "skill-1");
    await user.selectOptions(screen.getByLabelText("Output policy"), "prepare_changes");
    await user.click(screen.getByRole("button", { name: "Start supervised run" }));

    await waitFor(() => expect(client.startLocalRun).toHaveBeenCalledWith(expect.objectContaining({
      repositoryId: repository.id,
      task: "Add the demo feature",
      model: "gpt-5.6-terra",
      effort: "xhigh",
      skillVersionId: "skill-1",
      mode: "build",
      outputPolicy: "prepare_changes",
      safetyDigest: "b".repeat(64)
    })));
    expect(await screen.findByRole("heading", { name: "Add the demo feature" })).toBeVisible();
  });

  it("selects the first healthy repository instead of an unhealthy earlier record", async () => {
    const client = api();
    const unhealthy = {
      ...repository,
      id: "repo-unhealthy",
      display_name: "Broken checkout",
      snapshot: { ...repository.snapshot, health: "missing" }
    } as LocalRepository;
    vi.mocked(client.listRepositories).mockResolvedValue([unhealthy, repository]);
    const user = userEvent.setup();
    render(<RunsWorkspace api={client} />);

    await user.click(await screen.findByRole("button", { name: "New run" }));
    expect(screen.getByLabelText("Repository")).toHaveValue(repository.id);
    await user.type(screen.getByLabelText("Task"), "Use the healthy checkout");
    await user.click(screen.getByRole("button", { name: "Start supervised run" }));

    await waitFor(() => expect(client.startLocalRun).toHaveBeenCalledWith(
      expect.objectContaining({ repositoryId: repository.id })
    ));
  });

  it("selects the exact run requested by a cross-feature handoff", async () => {
    const client = api();
    const older = { ...run, id: "run-older", task: "Older run" } as LocalRun;
    const requested = { ...run, id: "run-requested", task: "Scheduled run" } as LocalRun;
    vi.mocked(client.listLocalRuns).mockResolvedValue([older, requested]);
    vi.mocked(client.getLocalRun).mockImplementation(async (runId) =>
      runId === requested.id ? requested : older
    );

    render(<RunsWorkspace api={client} initialRunId={requested.id} />);

    expect(await screen.findByRole("heading", { name: "Scheduled run" })).toBeVisible();
    expect(client.getLocalRun).toHaveBeenCalledWith(requested.id);
  });

  it("keeps runs disabled until provider discovery is verified and supports retry", async () => {
    const client = api();
    vi.mocked(client.listLocalProviders)
      .mockRejectedValueOnce(new Error("discovery failed"))
      .mockResolvedValueOnce([codex]);
    const user = userEvent.setup();
    render(<RunsWorkspace api={client} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/provider discovery failed/i);
    expect(screen.getByRole("button", { name: "New run" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry providers" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "New run" })).toBeEnabled());
    expect(client.listLocalProviders).toHaveBeenCalledTimes(2);
  });

  it("does not treat a ready provider with an empty capability catalog as runnable", async () => {
    const client = api();
    vi.mocked(client.listLocalProviders).mockResolvedValue([{
      ...codex,
      models: [],
      thinking_levels: []
    }]);

    render(<RunsWorkspace api={client} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/returned no supported models or thinking levels/i);
    expect(screen.getByRole("button", { name: "New run" })).toBeDisabled();
  });

  it("preserves a clear recovery path when run prerequisites fail to load", async () => {
    const client = api();
    vi.mocked(client.listRepositories)
      .mockRejectedValueOnce(new Error("request_failed_503"))
      .mockResolvedValueOnce([repository]);
    const user = userEvent.setup();
    render(<RunsWorkspace api={client} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/runtime is temporarily unavailable/i);
    await user.click(screen.getByRole("button", { name: "Retry run data" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "New run" })).toBeEnabled());
    expect(client.listRepositories).toHaveBeenCalledTimes(2);
  });

  it("shows the durable agent result and exact provider token usage", async () => {
    const client = api();
    const completed = { ...run, status: "completed", finished_at: "2026-07-18T00:01:00Z" } as LocalRun;
    vi.mocked(client.listLocalRuns).mockResolvedValue([completed]);
    vi.mocked(client.getLocalRun).mockResolvedValue(completed);
    vi.mocked(client.listLocalRunEvents).mockResolvedValue([
      { run_id: run.id, sequence: 1, event_type: "provider.message_delta", payload: { text: "Implemented the focused change." }, created_at: "2026-07-18T00:00:10Z" },
      { run_id: run.id, sequence: 2, event_type: "provider.message_delta", payload: { text: "Review the prepared diff next." }, created_at: "2026-07-18T00:00:20Z" },
      { run_id: run.id, sequence: 3, event_type: "provider.usage", payload: { input_tokens: 1400, cached_input_tokens: 400, output_tokens: 120 }, created_at: "2026-07-18T00:00:30Z" }
    ]);

    render(<RunsWorkspace api={client} />);

    expect(await screen.findByRole("region", { name: "Agent result" })).toHaveTextContent(
      "Implemented the focused change. Review the prepared diff next."
    );
    expect(screen.getByRole("status", { name: "Model usage" })).toHaveTextContent(
      "1,400 input · 400 cached · 120 output"
    );
  });

  it("pages long durable event histories from the last received sequence", async () => {
    const client = api();
    const completed = { ...run, status: "completed", finished_at: "2026-07-18T00:01:00Z" } as LocalRun;
    const durableEvents = Array.from({ length: 501 }, (_, index) => ({
      run_id: run.id,
      sequence: index + 1,
      event_type: "provider.output",
      payload: { message: `Step ${index + 1}` },
      created_at: "2026-07-18T00:00:10Z"
    }));
    vi.mocked(client.listLocalRuns).mockResolvedValue([completed]);
    vi.mocked(client.getLocalRun).mockResolvedValue(completed);
    vi.mocked(client.listLocalRunEvents).mockImplementation(async (_runId, after = 0) =>
      durableEvents.filter((event) => event.sequence > after).slice(0, 500)
    );

    render(<RunsWorkspace api={client} />);

    expect(await screen.findByText("Step 501")).toBeVisible();
    expect(client.listLocalRunEvents).toHaveBeenNthCalledWith(1, run.id, 0, 500);
    expect(client.listLocalRunEvents).toHaveBeenNthCalledWith(2, run.id, 500, 500);
  });

  it("continues paging terminal runs after a full refresh window", async () => {
    const client = api();
    const completed = { ...run, status: "completed", finished_at: "2026-07-18T00:01:00Z" } as LocalRun;
    const durableEvent = (sequence: number) => ({
      run_id: run.id,
      sequence,
      event_type: "provider.output",
      payload: { message: `Step ${sequence}` },
      created_at: "2026-07-18T00:00:10Z"
    });
    vi.mocked(client.listLocalRuns).mockResolvedValue([completed]);
    vi.mocked(client.getLocalRun).mockResolvedValue(completed);
    vi.mocked(client.listLocalRunEvents).mockImplementation(async (_runId, after = 0) => {
      if (after === 10) return [durableEvent(11)];
      return [
        ...Array.from({ length: 499 }, () => durableEvent(after)),
        durableEvent(after + 1)
      ];
    });

    render(<RunsWorkspace api={client} />);

    expect(await screen.findByText("Step 11", {}, { timeout: 3_000 })).toBeVisible();
    expect(client.listLocalRunEvents).toHaveBeenLastCalledWith(run.id, 10, 500);
  });
});
