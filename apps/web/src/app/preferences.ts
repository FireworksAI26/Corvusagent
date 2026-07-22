import type { components } from "../generated/api";

export const WORKSPACE_PREFERENCE_KEY = "corvus.workspace-preference";
const LEGACY_WORKSPACE_PREFERENCE_VERSION = 1;

export type ExperienceMode = components["schemas"]["ExperienceKind"];
export type WorkspaceKind = components["schemas"]["WorkspaceKind"];
export type RuntimePreselection = "local" | "cloud_preview";

export interface LegacyPreferenceCandidate {
  experience: ExperienceMode;
  runtimePreselection: RuntimePreselection;
  workspaceKind: WorkspaceKind;
}

export interface LegacyPreferenceLoadResult {
  candidate: LegacyPreferenceCandidate | null;
  recovered: boolean;
}

interface MigrationCompletion {
  experienceConfirmed: boolean;
  workspaceCreationConfirmed: boolean;
}

function migrateLegacyWorkspacePreference(value: unknown): LegacyPreferenceCandidate | null {
  if (typeof value !== "object" || value === null) return null;
  const candidate = value as Record<string, unknown>;
  if (
    candidate.version !== LEGACY_WORKSPACE_PREFERENCE_VERSION ||
    candidate.onboardingComplete !== true ||
    (candidate.experience !== "everyday" && candidate.experience !== "developer") ||
    (candidate.scope !== "personal" && candidate.scope !== "team") ||
    (candidate.runtime !== "local" && candidate.runtime !== "corvus_cloud")
  ) {
    return null;
  }
  return {
    experience: candidate.experience,
    runtimePreselection: candidate.runtime === "corvus_cloud" ? "cloud_preview" : "local",
    workspaceKind: candidate.scope === "personal" ? "individual" : "team"
  };
}

export function loadLegacyWorkspacePreference(storage: Storage): LegacyPreferenceLoadResult {
  const serialized = storage.getItem(WORKSPACE_PREFERENCE_KEY);
  if (serialized === null) return { candidate: null, recovered: false };

  try {
    const candidate = migrateLegacyWorkspacePreference(JSON.parse(serialized) as unknown);
    if (candidate !== null) return { candidate, recovered: false };
  } catch {
    // Invalid legacy data is removed below; it never becomes current authority.
  }
  storage.removeItem(WORKSPACE_PREFERENCE_KEY);
  return { candidate: null, recovered: true };
}

export function completeLegacyPreferenceMigration(
  storage: Storage,
  completion: MigrationCompletion
): void {
  if (completion.experienceConfirmed && completion.workspaceCreationConfirmed) {
    storage.removeItem(WORKSPACE_PREFERENCE_KEY);
  }
}

export function dismissLegacyPreferenceMigration(storage: Storage): void {
  storage.removeItem(WORKSPACE_PREFERENCE_KEY);
}

export function saveLocalWorkspacePreference(
  storage: Storage,
  preference: Pick<LegacyPreferenceCandidate, "experience" | "workspaceKind">
): void {
  storage.setItem(WORKSPACE_PREFERENCE_KEY, JSON.stringify({
    version: LEGACY_WORKSPACE_PREFERENCE_VERSION,
    onboardingComplete: true,
    experience: preference.experience,
    scope: preference.workspaceKind === "team" ? "team" : "personal",
    runtime: "local"
  }));
}
