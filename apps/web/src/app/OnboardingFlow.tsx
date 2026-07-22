import { useEffect, useId, useRef, useState } from "react";

import type { AuthStatus } from "../auth/AuthProvider";
import type { components } from "../generated/api";
import { BrandLockup } from "../components/Brand";
import type { LegacyPreferenceCandidate } from "./preferences";
import { loadDevicePreferences, saveDevicePreferences, type SafetyGuidance } from "./devicePreferences";

type ExperienceKind = components["schemas"]["ExperienceKind"];
type OnboardingResponse = components["schemas"]["OnboardingResponse"];
type Workspace = components["schemas"]["Workspace"];
type WorkspaceCreate = components["schemas"]["WorkspaceCreate"];
type WorkspaceKind = components["schemas"]["WorkspaceKind"];

type AuthEntryStatus = Extract<AuthStatus, "unauthenticated" | "authenticated">;
type OnboardingStep = "profile" | "protection" | "create";

export interface OnboardingFlowProps {
  accountVersion: number;
  authStatus: AuthEntryStatus;
  experienceKind: ExperienceKind | null;
  onCreateWorkspace(body: WorkspaceCreate, idempotencyKey: string): Promise<Workspace>;
  onExperienceSaved(
    experienceKind: ExperienceKind,
    expectedVersion: number
  ): Promise<OnboardingResponse>;
  onGoogleStart(): void;
  onWorkspaceConfirmed(workspace: Workspace): void | Promise<void>;
  onDismissMigration?(): void;
  preselection?: LegacyPreferenceCandidate | null;
  storage?: Storage;
}

interface Choice<T extends string> {
  description: string;
  title: string;
  value: T;
}

const EXPERIENCE_CHOICES: readonly Choice<ExperienceKind>[] = [
  {
    value: "everyday",
    title: "Everyday",
    description: "Clear plans, progress, approvals, and results with deeper detail available."
  },
  {
    value: "developer",
    title: "Developer",
    description: "Repositories, runs, diffs, logs, policy, and precise controls."
  }
];

const WORKSPACE_CHOICES: readonly Choice<WorkspaceKind>[] = [
  {
    value: "individual",
    title: "Individual",
    description: "Private work and personal automations in an authorized workspace."
  },
  {
    value: "team",
    title: "Team",
    description: "Shared presentation only until real membership is confirmed by Corvus."
  }
];

const SAFETY_CHOICES: readonly Choice<SafetyGuidance>[] = [
  {
    value: "standard",
    title: "Standard guidance",
    description: "Show the active protection and important blocked or confirmed actions."
  },
  {
    value: "detailed",
    title: "Detailed guidance",
    description: "Show more runtime evidence, policy detail, and artifact screening context."
  }
];

const TOTAL_STEPS = 3;

function messageFor(reason: unknown): string {
  if (!(reason instanceof Error)) return "Workspace setup could not be completed. Try again.";
  return reason.message.replaceAll("_", " ");
}

function stepNumber(step: OnboardingStep): number {
  return { profile: 1, protection: 2, create: 3 }[step];
}

export function OnboardingFlow({
  accountVersion,
  authStatus,
  experienceKind,
  onCreateWorkspace,
  onExperienceSaved,
  onGoogleStart,
  onWorkspaceConfirmed,
  onDismissMigration,
  preselection = null,
  storage = window.localStorage
}: OnboardingFlowProps) {
  const groupName = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const idempotencyKeyRef = useRef<string | null>(null);
  const [step, setStep] = useState<OnboardingStep>("profile");
  const [experience, setExperience] = useState<ExperienceKind | null>(
    experienceKind ?? preselection?.experience ?? null
  );
  const [workspaceKind, setWorkspaceKind] = useState<WorkspaceKind | null>(
    preselection?.workspaceKind ?? null
  );
  const [runtime, setRuntime] = useState<"local" | "cloud">(
    "cloud"
  );
  const [safetyGuidance, setSafetyGuidance] = useState<SafetyGuidance | null>("standard");
  const [workspaceName, setWorkspaceName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (experienceKind !== null) {
      setExperience(experienceKind);
    }
  }, [experienceKind]);

  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  useEffect(() => {
    if (error !== null) errorRef.current?.focus();
  }, [error]);

  if (authStatus === "unauthenticated") {
    return (
      <main
        className="onboarding-shell"
        data-corvus-surface="identity-entry"
        data-source-refs="corvus-platform chatgpt-conversion shadcn-button"
        id="main-content"
      >
        <section className="onboarding-panel onboarding-panel--identity" aria-labelledby="identity-heading">
          <BrandLockup className="onboarding-wordmark" />
          <p className="eyebrow">Safety-first agent</p>
          <h1 id="identity-heading" ref={headingRef} tabIndex={-1}>Welcome to Corvus</h1>
          <p className="onboarding-lede">Turn local work into reviewable results with protected runs and explicit approval.</p>
          <button
            className="button button--primary onboarding-google"
            data-action="sign-in-google"
            data-component-source="shadcn-button"
            onClick={onGoogleStart}
            type="button"
          >
            Continue with Google
          </button>
        </section>
      </main>
    );
  }

  const heading = step === "profile"
    ? "Set up your workspace"
    : step === "protection"
      ? "Choose protection and runtime"
      : "Create your workspace";

  async function continueProfile() {
    if (experience === null || workspaceKind === null) return;
    if (experienceKind !== null) {
      setStep("protection");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const saved = await onExperienceSaved(experience, accountVersion);
      setExperience(saved.experience_kind);
      setStep("protection");
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setBusy(false);
    }
  }

  async function createWorkspace() {
    if (workspaceKind === null || workspaceName.trim() === "") return;
    if (idempotencyKeyRef.current === null) idempotencyKeyRef.current = crypto.randomUUID();
    setBusy(true);
    setError(null);
    try {
      const workspace = await onCreateWorkspace(
        { name: workspaceName.trim(), workspace_kind: workspaceKind },
        idempotencyKeyRef.current
      );
      const device = loadDevicePreferences(storage, workspace.id);
      saveDevicePreferences(storage, workspace.id, {
        ...device,
        safetyGuidance: safetyGuidance ?? "standard"
      });
      await onWorkspaceConfirmed(workspace);
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setBusy(false);
    }
  }

  function back() {
    setError(null);
    setStep((current) => {
      if (current === "create") return "protection";
      if (current === "protection") return "profile";
      return current;
    });
  }

  const canGoBack = step !== "profile";

  return (
    <main
      className="onboarding-shell"
      data-corvus-surface="identity-entry"
      data-source-refs="corvus-platform chatgpt-conversion shadcn-button"
      id="main-content"
    >
      <section className="onboarding-panel" aria-labelledby="onboarding-heading">
        <BrandLockup className="onboarding-wordmark" />
        <div className="onboarding-progress" aria-live="polite">
          <span>Step {stepNumber(step)} of {TOTAL_STEPS}</span>
        </div>
        {error !== null && <div className="setup-error" ref={errorRef} role="alert" tabIndex={-1}>{error}</div>}
        <h1 id="onboarding-heading" ref={headingRef} tabIndex={-1}>{heading}</h1>

        {step === "profile" && (
          <>
            <p className="onboarding-lede">Choose how Corvus presents work and who owns this workspace. Neither choice grants new authority.</p>
            {experienceKind === null ? <ChoiceGroup<ExperienceKind>
              choices={EXPERIENCE_CHOICES}
              dataChoice="experience"
              groupName={`${groupName}-experience`}
              label="Experience"
              onChoose={setExperience}
              selected={experience}
            /> : <div className="onboarding-fixed-choice"><span>Experience</span><strong>{experienceKind === "developer" ? "Developer" : "Everyday"}</strong></div>}
            <ChoiceGroup<WorkspaceKind>
              choices={WORKSPACE_CHOICES}
              dataChoice="workspace-type"
              groupName={`${groupName}-workspace`}
              label="Workspace"
              onChoose={(value) => {
                idempotencyKeyRef.current = null;
                setWorkspaceKind(value);
              }}
              selected={workspaceKind}
            />
          </>
        )}

        {step === "protection" && (
          <>
            <p className="onboarding-lede">Guidance changes what Corvus explains. Every run keeps the same enforced protection.</p>
            <ChoiceGroup<SafetyGuidance>
              choices={SAFETY_CHOICES}
              dataChoice="safety-guidance"
              groupName={`${groupName}-safety`}
              label="Safety guidance"
              onChoose={setSafetyGuidance}
              selected={safetyGuidance}
            />
            <fieldset className="onboarding-choice-list" data-choice="runtime-policy">
              <legend>Runtime</legend>
              <label className={`onboarding-choice ${runtime === "local" ? "onboarding-choice--selected" : ""}`}>
                <input checked={runtime === "local"} name={`${groupName}-runtime`} onChange={() => setRuntime("local")} type="radio" value="local" />
                <span className="onboarding-choice__marker" aria-hidden="true" />
                <span><strong>Local</strong><small>Available now through your paired runtime.</small></span>
              </label>
              <label className={`onboarding-choice ${runtime === "cloud" ? "onboarding-choice--selected" : ""}`}>
                <input checked={runtime === "cloud"} name={`${groupName}-runtime`} onChange={() => setRuntime("cloud")} type="radio" value="cloud" />
                <span className="onboarding-choice__marker" aria-hidden="true" />
                <span><strong>Corvus Cloud</strong><small>Provision an isolated, resumable E2B workspace after setup.</small></span>
              </label>
              {preselection?.runtimePreselection === "cloud_preview" && (
                <p className="setup-notice" role="status">
                  Your previous Cloud choice is ready to migrate to the E2B runtime.
                  {onDismissMigration && <button className="text-button" onClick={onDismissMigration} type="button">Dismiss previous setup</button>}
                </p>
              )}
            </fieldset>
          </>
        )}

        {step === "create" && (
          <div className="workspace-create-form">
            <p className="onboarding-lede">Name the workspace Corvus will use on this device.</p>
            <dl className="onboarding-summary">
              <div><dt>Profile</dt><dd>{experience === "developer" ? "Developer" : "Everyday"} / {workspaceKind === "team" ? "Team" : "Individual"}</dd></div>
              <div><dt>Protection</dt><dd>{safetyGuidance === "detailed" ? "Detailed guidance" : "Standard guidance"}</dd></div>
              <div><dt>Runtime</dt><dd>{runtime === "cloud" ? "Corvus Cloud" : "Local"}</dd></div>
            </dl>
            <label htmlFor="workspace-name">Workspace name</label>
            <input
              id="workspace-name"
              maxLength={200}
              onChange={(event) => {
                if (event.target.value !== workspaceName) idempotencyKeyRef.current = null;
                setWorkspaceName(event.target.value);
              }}
              value={workspaceName}
            />
            <p className="choice-footnote">
              {workspaceKind === "team"
                ? "A Team workspace is created only after this explicit action."
                : "This creates your authorized Individual workspace."}
            </p>
            <div className="onboarding-create-actions">
              <button className="button button--quiet" disabled={busy} onClick={back} type="button">Back</button>
              <button
                className="button button--primary"
                data-action="create-workspace"
                data-component-source="shadcn-button"
                disabled={busy || workspaceName.trim() === ""}
                onClick={() => void createWorkspace()}
                type="button"
              >
                Create workspace
              </button>
            </div>
            <p className="choice-footnote">Joining an existing workspace is not available yet.</p>
          </div>
        )}

        {step !== "create" && (
          <div className="onboarding-actions">
            <button className="button button--quiet" disabled={!canGoBack || busy} onClick={back} type="button">Back</button>
            <button
              className="button button--primary"
              data-component-source="shadcn-button"
              disabled={
                busy ||
                (step === "profile" && (experience === null || workspaceKind === null)) ||
                (step === "protection" && (safetyGuidance === null || runtime === null))
              }
              onClick={() => {
                if (step === "profile") void continueProfile();
                else setStep("create");
              }}
              type="button"
            >
              Continue
            </button>
          </div>
        )}
      </section>
    </main>
  );
}

function ChoiceGroup<T extends string>({
  choices,
  dataChoice,
  groupName,
  label,
  onChoose,
  selected
}: {
  choices: readonly Choice<T>[];
  dataChoice: string;
  groupName: string;
  label: string;
  onChoose(value: T): void;
  selected: T | null;
}) {
  return (
    <fieldset className="onboarding-choice-list" data-choice={dataChoice}>
      <legend>{label}</legend>
      {choices.map((choice) => (
        <label className={`onboarding-choice ${selected === choice.value ? "onboarding-choice--selected" : ""}`} key={choice.value}>
          <input checked={selected === choice.value} name={groupName} onChange={() => onChoose(choice.value)} type="radio" value={choice.value} />
          <span className="onboarding-choice__marker" aria-hidden="true" />
          <span><strong>{choice.title}</strong><small>{choice.description}</small></span>
        </label>
      ))}
    </fieldset>
  );
}
