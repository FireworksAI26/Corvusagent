import { useState, type FormEvent } from "react";

import type { Routine, SkillVersion } from "../api";

export function RoutinesWorkspace({
  busy,
  onCreate,
  onRun,
  projectName,
  routines,
  skills
}: {
  busy: boolean;
  onCreate(name: string, skillVersionId: string): Promise<void>;
  onRun(routineId: string): Promise<void>;
  projectName: string | null;
  routines: readonly Routine[];
  skills: readonly SkillVersion[];
}) {
  const [name, setName] = useState("");
  const [skillId, setSkillId] = useState("");
  const [lastRun, setLastRun] = useState<string | null>(null);
  const activeSkills = skills.filter((skill) => skill.status === "active");

  async function create(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (name.trim() === "" || skillId === "") return;
    await onCreate(name.trim(), skillId);
    setName("");
  }

  async function run(routine: Routine): Promise<void> {
    await onRun(routine.id);
    setLastRun(routine.id);
  }

  if (projectName === null) {
    return <section className="schedule-workspace"><p className="eyebrow">Schedule</p><h1>Choose or create a project first.</h1><p>Routines remain bound to an authorized project and active skill.</p></section>;
  }

  return (
    <section className="schedule-workspace">
      <header className="settings-heading"><p className="eyebrow">{projectName}</p><h1>Repeat useful work.</h1><p>Create a governed routine, then run it now. Timed recurrence is coming soon.</p></header>
      <div className="schedule-grid">
        <form className="settings-card" onSubmit={(event) => void create(event)}>
          <div className="section-heading"><h2>New routine</h2><span>Manual runs</span></div>
          <label htmlFor="routine-workspace-name">Routine name</label>
          <input id="routine-workspace-name" onChange={(event) => setName(event.target.value)} placeholder="Morning brief" required value={name} />
          <label htmlFor="routine-workspace-skill">Skill</label>
          <select id="routine-workspace-skill" onChange={(event) => setSkillId(event.target.value)} required value={skillId}>
            <option value="">Choose an active skill</option>
            {activeSkills.map((skill) => <option key={skill.id} value={skill.id}>{skill.name} · v{skill.version}</option>)}
          </select>
          {activeSkills.length === 0 ? <p className="field-note">Create and activate a skill before adding a routine.</p> : null}
          <button className="button button--primary" disabled={busy || name.trim() === "" || skillId === ""} type="submit">Create routine</button>
        </form>
        <section className="settings-card schedule-list">
          <div className="section-heading"><h2>Ready to run</h2><span>{routines.length} routines</span></div>
          {routines.length === 0 ? <p className="quiet-copy">No routines yet.</p> : routines.map((routine) => (
            <article className="schedule-row" key={routine.id}>
              <div><strong>{routine.name}</strong><span>Authorized skill · {routine.skill_version_id}</span></div>
              <button className="button" disabled={busy} onClick={() => void run(routine)} type="button" aria-label={`Run ${routine.name} now`}>Run now</button>
              {lastRun === routine.id ? <small role="status">Run requested</small> : null}
            </article>
          ))}
        </section>
      </div>
    </section>
  );
}
