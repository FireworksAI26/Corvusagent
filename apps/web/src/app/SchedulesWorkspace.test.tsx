import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { LocalProviderCatalogEntry, LocalRepository, LocalRun, LocalSafetyPreview, LocalSchedule } from "../api";
import { SchedulesWorkspace, type SchedulesApi } from "./SchedulesWorkspace";

const repository = { id: "repo-1", display_name: "Corvus", snapshot: { health: "healthy" } } as LocalRepository;
const codex = {
  id: "codex", label: "OpenAI Codex", runtime: "local", status: "ready",
  status_label: "CLI and login verified",
  models: [{ id: "gpt-5.6-sol", label: "GPT-5.6 Sol", recommended: true }],
  thinking_levels: ["low", "medium", "high", "xhigh"], supports_mcp: true
} as LocalProviderCatalogEntry;
const schedule = {
  id: "schedule-1", repository_id: repository.id, name: "Weekday review", task: "Review changes",
  status: "active", version: 1, recurrence: { kind: "weekdays", local_time: "09:00:00", weekdays: [] },
  next_run_at: "2026-07-20T09:00:00Z", model: "gpt-5.6-sol", effort: "high", mode: "build",
  output_policy: "prepare_changes", skill_version_id: null, timezone: "UTC"
} as unknown as LocalSchedule;

function api(): SchedulesApi {
  return {
    listRepositories: vi.fn().mockResolvedValue([repository]),
    listLocalProviders: vi.fn().mockResolvedValue([codex]),
    listPortableSkills: vi.fn().mockResolvedValue([]),
    getLocalSafetyPreview: vi.fn().mockResolvedValue({ policy_digest: "a".repeat(64) } as LocalSafetyPreview),
    listLocalSchedules: vi.fn().mockResolvedValue([]),
    createLocalSchedule: vi.fn().mockResolvedValue(schedule),
    runLocalScheduleNow: vi.fn(),
    pauseLocalSchedule: vi.fn(),
    resumeLocalSchedule: vi.fn(),
    archiveLocalSchedule: vi.fn()
  };
}

describe("SchedulesWorkspace", () => {
  it("inherits runtime defaults and keeps advanced schedule controls collapsed", async () => {
    const user = userEvent.setup();
    render(<SchedulesWorkspace api={api()} onOpenRun={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "New schedule" }));

    expect(screen.getByRole("button", { name: "Advanced options" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Model")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Cadence")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Advanced options" }));
    expect(screen.getByLabelText("Model")).toBeVisible();
    expect(screen.getByLabelText("Thinking")).toBeVisible();
    expect(screen.getByLabelText("Timezone")).toBeVisible();
  });

  it("creates a timezone-aware supervised schedule", async () => {
    const user = userEvent.setup();
    const client = api();
    render(<SchedulesWorkspace api={client} onOpenRun={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "New schedule" }));
    await user.type(screen.getByLabelText("Name"), "Weekday review");
    await user.type(screen.getByLabelText("Task"), "Review changes");
    await user.selectOptions(screen.getByLabelText("Cadence"), "weekdays");
    await user.click(screen.getByRole("button", { name: "Advanced options" }));
    await user.clear(screen.getByLabelText("Timezone"));
    await user.type(screen.getByLabelText("Timezone"), "UTC");
    await user.click(screen.getByRole("button", { name: "Create schedule" }));
    await waitFor(() => expect(client.createLocalSchedule).toHaveBeenCalledWith(expect.objectContaining({
      repositoryId: repository.id,
      model: "gpt-5.6-sol",
      effort: "high",
      mode: "build",
      outputPolicy: "prepare_changes",
      timezone: "UTC",
      recurrence: expect.objectContaining({ kind: "weekdays" })
    })));
    expect(await screen.findByRole("heading", { name: "Weekday review" })).toBeVisible();
  });

  it("uses the backend Monday index for weekly schedules", async () => {
    const user = userEvent.setup();
    const client = api();
    render(<SchedulesWorkspace api={client} onOpenRun={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "New schedule" }));
    await user.type(screen.getByLabelText("Name"), "Monday review");
    await user.type(screen.getByLabelText("Task"), "Review every Monday");
    await user.selectOptions(screen.getByLabelText("Cadence"), "weekly");
    await user.click(screen.getByRole("button", { name: "Create schedule" }));

    await waitFor(() => expect(client.createLocalSchedule).toHaveBeenCalledWith(
      expect.objectContaining({ recurrence: expect.objectContaining({ weekdays: [0] }) })
    ));
  });

  it("retries schedule data without losing provider readiness", async () => {
    const client = api();
    vi.mocked(client.listLocalSchedules)
      .mockRejectedValueOnce(new Error("request_failed_503"))
      .mockResolvedValueOnce([]);
    const user = userEvent.setup();
    render(<SchedulesWorkspace api={client} onOpenRun={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/runtime is temporarily unavailable/i);
    await user.click(screen.getByRole("button", { name: "Retry schedules" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "New schedule" })).toBeEnabled());
    expect(client.listLocalSchedules).toHaveBeenCalledTimes(2);
  });

  it("keeps New schedule actionable and routes a missing-project prerequisite", async () => {
    const client = api();
    vi.mocked(client.listRepositories).mockResolvedValue([]);
    const onOpenProjects = vi.fn();
    const user = userEvent.setup();

    render(<SchedulesWorkspace api={client} onOpenProjects={onOpenProjects} onOpenRun={vi.fn()} />);

    const button = await screen.findByRole("button", { name: "New schedule" });
    expect(button).toBeEnabled();
    await user.click(button);
    expect(await screen.findByText(/connect or create a healthy project before scheduling work/i)).toBeVisible();
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open Projects" }));
    expect(onOpenProjects).toHaveBeenCalledOnce();
  });

  it("explains a missing runnable model instead of disabling New schedule", async () => {
    const client = api();
    vi.mocked(client.listLocalProviders).mockResolvedValue([{
      ...codex,
      models: [],
      thinking_levels: []
    }]);
    const user = userEvent.setup();

    render(<SchedulesWorkspace api={client} onOpenRun={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/returned no supported models or thinking levels/i);
    const button = screen.getByRole("button", { name: "New schedule" });
    expect(button).toBeEnabled();
    await user.click(button);
    expect(await screen.findByText(/supported Codex model is required before scheduling work/i)).toBeVisible();
  });

  it("clears stale provider readiness and explains the provider prerequisite", async () => {
    const readyClient = api();
    const user = userEvent.setup();
    const { rerender } = render(
      <SchedulesWorkspace api={readyClient} onOpenRun={vi.fn()} />
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "New schedule" })).toBeEnabled());
    const failingClient = {
      ...readyClient,
      listLocalProviders: vi.fn().mockRejectedValue(new Error("provider discovery failed"))
    };

    rerender(<SchedulesWorkspace api={failingClient} onOpenRun={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/provider discovery failed/i);
    const button = screen.getByRole("button", { name: "New schedule" });
    expect(button).toBeEnabled();
    await user.click(button);
    expect(await screen.findByText(/verify Codex and sign in before scheduling work/i)).toBeVisible();
  });

  it("opens the exact durable run created by Run now", async () => {
    const client = api();
    const onOpenRun = vi.fn();
    vi.mocked(client.listLocalSchedules).mockResolvedValue([schedule]);
    vi.mocked(client.runLocalScheduleNow).mockResolvedValue({ id: "run-now-1" } as LocalRun);
    const user = userEvent.setup();

    render(<SchedulesWorkspace api={client} onOpenRun={onOpenRun} />);
    await user.click(await screen.findByRole("button", { name: "Run now" }));

    await waitFor(() => expect(onOpenRun).toHaveBeenCalledWith("run-now-1"));
  });

  it("explains the latest skipped occurrence instead of hiding it", async () => {
    const client = api();
    vi.mocked(client.listLocalSchedules).mockResolvedValue([{
      ...schedule,
      last_run_status: "skipped",
      last_run_reason: "repository_not_healthy",
      last_run_at: "2026-07-20T08:00:00Z"
    } as unknown as LocalSchedule]);

    render(<SchedulesWorkspace api={client} onOpenRun={vi.fn()} />);

    expect(await screen.findByText("Skipped: repository needs attention")).toBeVisible();
  });
});
