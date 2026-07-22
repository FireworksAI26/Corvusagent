import { useState } from "react";

import { BrandLockup } from "../components/Brand";
import type { ExperienceMode, WorkspaceKind } from "./preferences";

type Step = "welcome" | "profile";

export function LocalFirstRunFlow({ onComplete }: {
  onComplete(experience: ExperienceMode, workspaceKind: WorkspaceKind): void;
}) {
  const [step, setStep] = useState<Step>("welcome");
  const [experience, setExperience] = useState<ExperienceMode>("developer");
  const [workspaceKind, setWorkspaceKind] = useState<WorkspaceKind>("individual");

  return <main className="local-first-run" id="main-content"><section className="local-first-run__card">
    <BrandLockup className="onboarding-wordmark" />
    {step === "welcome" ? <>
      <h1>Welcome to Corvus</h1>
      <p>Plan, build, and review agent work with clear safety boundaries.</p>
      <button className="button button--primary" onClick={() => setStep("profile")} type="button">Continue</button>
    </> : null}
    {step === "profile" ? <>
      <p className="eyebrow">Local setup</p>
      <h1>Make Corvus yours</h1>
      <fieldset><legend>Experience</legend><label><input checked={experience === "everyday"} onChange={() => setExperience("everyday")} type="radio" />Everyday</label><label><input checked={experience === "developer"} onChange={() => setExperience("developer")} type="radio" />Developer</label></fieldset>
      <fieldset><legend>Workspace</legend><label><input checked={workspaceKind === "individual"} onChange={() => setWorkspaceKind("individual")} type="radio" />Individual</label><label><input checked={workspaceKind === "team"} onChange={() => setWorkspaceKind("team")} type="radio" />Team</label></fieldset>
      <button className="button button--primary" onClick={() => onComplete(experience, workspaceKind)} type="button">Open Corvus</button>
    </> : null}
  </section></main>;
}
