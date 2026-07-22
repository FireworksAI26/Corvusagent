import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import type {
  LocalProviderCatalogEntry,
  LocalRepository,
  LocalRun,
  LocalSafetyPreview,
  LocalSchedule,
  PortableSkill
} from "../api";
import { featureErrorMessage } from "./featureFeedback";

export interface SchedulesApi {
  listRepositories(): Promise<LocalRepository[]>;
  listLocalProviders(): Promise<LocalProviderCatalogEntry[]>;
  listPortableSkills(): Promise<PortableSkill[]>;
  getLocalSafetyPreview(mode: "chat" | "build"): Promise<LocalSafetyPreview>;
  listLocalSchedules(): Promise<LocalSchedule[]>;
  createLocalSchedule(input: {
    name: string;
    repositoryId: string;
    task: string;
    recurrence: { kind: "once" | "hourly" | "daily" | "weekdays" | "weekly"; local_time?: string; weekdays: number[]; once_at?: string };
    timezone: string;
    model?: string;
    effort: "low" | "medium" | "high" | "xhigh";
    mode: "chat" | "build";
    safetyDigest: string;
    skillVersionId?: string;
    outputPolicy: "report_only" | "prepare_changes" | "prepare_contribution";
  }): Promise<LocalSchedule>;
  runLocalScheduleNow(scheduleId: string): Promise<LocalRun>;
  pauseLocalSchedule(scheduleId: string): Promise<LocalSchedule>;
  resumeLocalSchedule(scheduleId: string): Promise<LocalSchedule>;
  archiveLocalSchedule(scheduleId: string): Promise<LocalSchedule>;
}

type Effort = "low" | "medium" | "high" | "xhigh";

function effortLabel(effort: string): string {
  return effort === "xhigh" ? "Extra high" : effort[0].toUpperCase() + effort.slice(1);
}

function scheduleOutcomeLabel(schedule: LocalSchedule): string | null {
  if (schedule.last_run_status === "started") return "Started successfully";
  if (schedule.last_run_status !== "skipped") return null;
  const reason = schedule.last_run_reason;
  if (reason === "repository_not_healthy" || reason === "repository_unhealthy") {
    return "Skipped: repository needs attention";
  }
  if (reason === "schedule_run_already_active") return "Skipped: another run is still active";
  if (reason === "provider_unavailable" || reason === "codex_unavailable") {
    return "Skipped: Codex was unavailable";
  }
  if (reason === "scheduled_run_recovery_failed") return "Skipped: previous run recovery failed";
  return "Skipped: prerequisites changed";
}

type SchedulePrerequisite = {
  kind: "loading" | "project" | "provider" | "model";
  title: string;
  detail: string;
};

export function SchedulesWorkspace({
  api,
  onOpenProjects,
  onOpenRun
}: {
  api: SchedulesApi;
  onOpenProjects?: () => void;
  onOpenRun(runId: string): void;
}) {
  const [schedules, setSchedules] = useState<LocalSchedule[]>([]);
  const [repositories, setRepositories] = useState<LocalRepository[]>([]);
  const [providers, setProviders] = useState<LocalProviderCatalogEntry[]>([]);
  const [skills, setSkills] = useState<PortableSkill[]>([]);
  const [creating, setCreating] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [name, setName] = useState("");
  const [repositoryId, setRepositoryId] = useState("");
  const [task, setTask] = useState("");
  const [cadence, setCadence] = useState<"hourly" | "daily" | "weekdays" | "weekly">("daily");
  const [localTime, setLocalTime] = useState("09:00");
  const [timezone, setTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [mode, setMode] = useState<"chat" | "build">("build");
  const [effort, setEffort] = useState<Effort>("high");
  const [model, setModel] = useState("");
  const [skillId, setSkillId] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [providerError, setProviderError] = useState("");
  const [providerRefresh, setProviderRefresh] = useState(0);
  const [providerLoading, setProviderLoading] = useState(true);
  const [prerequisiteNotice, setPrerequisiteNotice] = useState<SchedulePrerequisite | null>(null);

  const codex = useMemo(
    () => providers.find((provider) => provider.id === "codex") ?? null,
    [providers]
  );
  const codexReady = codex?.status === "ready"
    && codex.models.length > 0
    && codex.thinking_levels.some((level) => level !== "max");
  const prerequisite: SchedulePrerequisite | null = loading
    ? { kind: "loading", title: "Checking schedule setup", detail: "Wait while Corvus checks your projects and local agent." }
    : repositories.length === 0
      ? { kind: "project", title: "Project required", detail: "Connect or create a healthy project before scheduling work." }
      : providerLoading
        ? { kind: "provider", title: "Checking Codex", detail: "Wait while Corvus verifies Codex and your local sign-in." }
        : codex === null || codex.status !== "ready"
          ? { kind: "provider", title: "Codex sign-in required", detail: "Verify Codex and sign in before scheduling work." }
          : !model || codex.models.length === 0 || !codex.thinking_levels.some((level) => level !== "max")
            ? { kind: "model", title: "Model required", detail: "A supported Codex model is required before scheduling work." }
            : null;

  const load = useCallback(async () => {
    setLoading(true);
    setLoadFailed(false);
    setError("");
    try {
      const [loadedSchedules, loadedRepositories, loadedSkills] = await Promise.all([
        api.listLocalSchedules(), api.listRepositories(), api.listPortableSkills()
      ]);
      setSchedules(loadedSchedules);
      setRepositories(loadedRepositories.filter((repository) => repository.snapshot.health === "healthy"));
      setSkills(loadedSkills.filter((skill) => skill.status === "active"));
      setRepositoryId((current) => current || loadedRepositories.find((repository) => repository.snapshot.health === "healthy")?.id || "");
    } catch (reason) {
      setLoadFailed(true);
      setError(featureErrorMessage(reason, "schedule"));
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    let active = true;
    setProviderError("");
    setProviderLoading(true);
    void api.listLocalProviders().then((catalog) => {
      if (!active) return;
      setProviders(catalog);
      const verifiedCodex = catalog.find((provider) => provider.id === "codex" && provider.status === "ready");
      if (verifiedCodex === undefined) {
        setModel("");
        setProviderError("Codex is not verified. Scheduled runs remain disabled.");
        return;
      }
      const supportedEfforts = verifiedCodex.thinking_levels.filter((level) => level !== "max");
      if (verifiedCodex.models.length === 0 || supportedEfforts.length === 0) {
        setModel("");
        setProviderError("Codex was verified, but discovery returned no supported models or thinking levels. Retry discovery before creating a schedule.");
        return;
      }
      setModel((current) => verifiedCodex.models.some((candidate) => candidate.id === current)
        ? current
        : verifiedCodex.models[0]?.id ?? "");
      setEffort((current) => supportedEfforts.includes(current)
        ? current
        : supportedEfforts.includes("medium") ? "medium" : supportedEfforts[0] as Effort);
    }).catch(() => {
      if (!active) return;
      setProviders([]);
      setModel("");
      setProviderError("Provider discovery failed. Retry before creating a schedule.");
    }).finally(() => {
      if (active) setProviderLoading(false);
    });
    return () => { active = false; };
  }, [api, providerRefresh]);

  async function create(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!name.trim() || !task.trim() || !repositoryId || !model || !codexReady) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const preview = await api.getLocalSafetyPreview(mode);
      const recurrence = cadence === "hourly"
        ? { kind: cadence, weekdays: [] }
        : cadence === "weekly"
          ? { kind: cadence, local_time: `${localTime}:00`, weekdays: [0] }
          : { kind: cadence, local_time: `${localTime}:00`, weekdays: [] };
      const record = await api.createLocalSchedule({
        name: name.trim(),
        repositoryId,
        task: task.trim(),
        recurrence,
        timezone,
        model,
        effort,
        mode,
        safetyDigest: preview.policy_digest,
        skillVersionId: skillId || undefined,
        outputPolicy: mode === "build" ? "prepare_changes" : "report_only"
      });
      setSchedules((current) => [record, ...current]);
      setName("");
      setTask("");
      setCreating(false);
      setNotice(`${record.name} is active. Every run will stop at report or review before external publication.`);
    } catch (reason) {
      setError(featureErrorMessage(reason, "schedule"));
    } finally {
      setBusy(false);
    }
  }

  async function action(schedule: LocalSchedule, kind: "run" | "pause" | "resume" | "archive"): Promise<void> {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (kind === "run") {
        const run = await api.runLocalScheduleNow(schedule.id);
        onOpenRun(run.id);
        return;
      }
      const updated = kind === "pause" ? await api.pauseLocalSchedule(schedule.id)
        : kind === "resume" ? await api.resumeLocalSchedule(schedule.id)
          : await api.archiveLocalSchedule(schedule.id);
      setSchedules((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice(kind === "pause" ? `${schedule.name} is paused.` : kind === "resume" ? `${schedule.name} is active again.` : `${schedule.name} was archived. Its history remains available.`);
    } catch (reason) {
      setError(featureErrorMessage(reason, "schedule"));
    } finally {
      setBusy(false);
    }
  }

  function openScheduleEditor(): void {
    if (prerequisite !== null) {
      setCreating(false);
      setPrerequisiteNotice(prerequisite);
      return;
    }
    setPrerequisiteNotice(null);
    setAdvancedOpen(false);
    setCreating(true);
  }

  return <section aria-labelledby="schedules-title" className="schedules-workspace">
    <header className="resource-heading"><div><p className="eyebrow">Local automation</p><h1 id="schedules-title">Schedule</h1><p>Repeat a supervised task while keeping every code change behind human review.</p></div><button className="button button--primary" onClick={openScheduleEditor} title={prerequisite?.detail} type="button">New schedule</button></header>
    <div className="schedule-notice"><strong>Review-only output</strong><span>Schedules can report or prepare changes. They never push, open a pull request, merge, or force-push.</span></div>
    {prerequisiteNotice ? <div className="schedule-notice" role="status"><strong>{prerequisiteNotice.title}</strong><span>{prerequisiteNotice.detail}</span>{prerequisiteNotice.kind === "project" && onOpenProjects ? <button className="button" onClick={onOpenProjects} type="button">Open Projects</button> : null}</div> : null}
    {providerError ? <p className="inline-error provider-recovery" role="alert">{providerError} <button className="text-button" onClick={() => setProviderRefresh((value) => value + 1)} type="button">Retry providers</button></p> : null}
    {creating ? <form className="schedule-editor" onSubmit={(event) => void create(event)}>
      <label>Name<input autoFocus onChange={(event) => setName(event.target.value)} placeholder="Weekday repository review" value={name} /></label>
      <label>Repository<select onChange={(event) => setRepositoryId(event.target.value)} value={repositoryId}>{repositories.map((repository) => <option key={repository.id} value={repository.id}>{repository.display_name}</option>)}</select></label>
      <label className="schedule-editor__task">Task<textarea onChange={(event) => setTask(event.target.value)} placeholder="Review recent changes and prepare a concise risk report..." rows={4} value={task} /></label>
      <label>Cadence<select onChange={(event) => setCadence(event.target.value as typeof cadence)} value={cadence}><option value="hourly">Every hour</option><option value="daily">Every day</option><option value="weekdays">Weekdays</option><option value="weekly">Every Monday</option></select></label>
      {cadence !== "hourly" ? <label>Local time<input onChange={(event) => setLocalTime(event.target.value)} type="time" value={localTime} /></label> : null}
      <button aria-expanded={advancedOpen} className="schedule-advanced-trigger" onClick={() => setAdvancedOpen((open) => !open)} type="button">Advanced options</button>
      {advancedOpen ? <div className="schedule-editor__advanced">
        <label>Provider<select disabled value="codex"><option value="codex">OpenAI Codex - verified local CLI</option></select></label>
        <label>Model<select aria-label="Model" onChange={(event) => setModel(event.target.value)} value={model}>{codex?.models.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.label}</option>)}</select></label>
        <label>Thinking<select aria-label="Thinking" onChange={(event) => setEffort(event.target.value as Effort)} value={effort}>{codex?.thinking_levels.filter((level) => level !== "max").map((level) => <option key={level} value={level}>{effortLabel(level)}</option>)}</select></label>
        <label>Skill<select onChange={(event) => setSkillId(event.target.value)} value={skillId}><option value="">No skill</option>{skills.map((skill) => <option key={skill.id} value={skill.id}>{skill.name} v{skill.version}</option>)}</select></label>
        <label>Timezone<input onChange={(event) => setTimezone(event.target.value)} value={timezone} /></label>
        <label>Mode<select onChange={(event) => setMode(event.target.value as "chat" | "build")} value={mode}><option value="chat">Report only</option><option value="build">Prepare changes for review</option></select></label>
        <label>Output policy<input aria-label="Output policy" disabled value={mode === "build" ? "Prepare changes - stop for review" : "Report only - no repository mutation"} /></label>
      </div> : null}
      <div className="row-actions schedule-editor__actions"><button className="button" onClick={() => setCreating(false)} type="button">Cancel</button><button className="button button--primary" disabled={busy || !name.trim() || !task.trim() || !repositoryId || !model || !codexReady} type="submit">{busy ? "Saving..." : "Create schedule"}</button></div>
    </form> : null}
    {notice ? <p className="inline-success" role="status">{notice}</p> : null}
    {error ? <p className="inline-error" role="alert">{error} {loadFailed ? <button className="text-button" disabled={loading} onClick={() => void load()} type="button">Retry schedules</button> : null}</p> : null}
    {loading ? <div className="resource-empty"><strong>Loading schedules and prerequisites…</strong></div> : null}
    <div className="schedule-card-grid">{schedules.length === 0 ? !loading ? <div className="resource-empty"><strong>No schedules yet</strong><span>Create a report-only cadence or a change task that stops at review.</span></div> : null : schedules.map((schedule) => {
      const outcome = scheduleOutcomeLabel(schedule);
      return <article className="schedule-card" key={schedule.id}><header><span className="run-status" data-status={schedule.status}>{schedule.status}</span><small>v{schedule.version}</small></header><h2>{schedule.name}</h2><p>{schedule.task}</p>{outcome ? <div className="schedule-card__outcome"><strong>{outcome}</strong>{schedule.last_run_at ? <small>{new Date(schedule.last_run_at).toLocaleString()}</small> : null}</div> : null}<dl><div><dt>Repository</dt><dd>{repositories.find((repository) => repository.id === schedule.repository_id)?.display_name ?? schedule.repository_id}</dd></div><div><dt>Skill</dt><dd>{skills.find((skill) => skill.id === schedule.skill_version_id)?.name ?? (schedule.skill_version_id ? "Selected skill" : "None")}</dd></div><div><dt>Model</dt><dd>{schedule.model ?? "Provider default"}</dd></div><div><dt>Thinking</dt><dd>{schedule.effort}</dd></div><div><dt>Cadence</dt><dd>{schedule.recurrence.kind}</dd></div><div><dt>Next run</dt><dd>{schedule.next_run_at ? new Date(schedule.next_run_at).toLocaleString() : "Finished"}</dd></div><div><dt>Output</dt><dd>{schedule.output_policy === "prepare_changes" ? "Prepare changes - review required" : "Report only"}</dd></div><div><dt>Timezone</dt><dd>{schedule.timezone}</dd></div></dl><footer><button className="button button--primary" disabled={busy || schedule.status === "archived"} onClick={() => void action(schedule, "run")} type="button">Run now</button>{schedule.status === "active" ? <button className="button" disabled={busy} onClick={() => void action(schedule, "pause")} type="button">Pause</button> : schedule.status === "paused" ? <button className="button" disabled={busy} onClick={() => void action(schedule, "resume")} type="button">Resume</button> : null}{schedule.status !== "archived" ? <button className="button" disabled={busy} onClick={() => void action(schedule, "archive")} type="button">Archive</button> : null}</footer></article>;
    })}</div>
  </section>;
}
