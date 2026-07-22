import { beforeEach, describe, expect, it } from "vitest";

import { MemoryStorage } from "../test/memoryStorage";
import {
  WORKSPACE_PREFERENCE_KEY,
  completeLegacyPreferenceMigration,
  dismissLegacyPreferenceMigration,
  loadLegacyWorkspacePreference
} from "./preferences";

describe("legacy workspace preference migration", () => {
  let storage: MemoryStorage;

  beforeEach(() => {
    storage = new MemoryStorage();
  });

  it("maps personal to canonical individual in one migration boundary", () => {
    storage.setItem(
      WORKSPACE_PREFERENCE_KEY,
      JSON.stringify({
        version: 1,
        experience: "developer",
        scope: "personal",
        runtime: "local",
        onboardingComplete: true
      })
    );

    expect(loadLegacyWorkspacePreference(storage)).toEqual({
      candidate: {
        experience: "developer",
        runtimePreselection: "local",
        workspaceKind: "individual"
      },
      recovered: false
    });
    expect(storage.getItem(WORKSPACE_PREFERENCE_KEY)).not.toBeNull();
  });

  it("treats Team as preselection only and Cloud as a disabled preview preselection", () => {
    storage.setItem(
      WORKSPACE_PREFERENCE_KEY,
      JSON.stringify({
        version: 1,
        experience: "everyday",
        scope: "team",
        runtime: "corvus_cloud",
        onboardingComplete: true
      })
    );

    expect(loadLegacyWorkspacePreference(storage).candidate).toEqual({
      experience: "everyday",
      runtimePreselection: "cloud_preview",
      workspaceKind: "team"
    });
    expect(storage.getItem(WORKSPACE_PREFERENCE_KEY)).not.toBeNull();
  });

  it("removes invalid legacy data immediately instead of guessing", () => {
    storage.setItem(
      WORKSPACE_PREFERENCE_KEY,
      JSON.stringify({ version: 1, experience: "expert", scope: "shared", runtime: "remote" })
    );

    expect(loadLegacyWorkspacePreference(storage)).toEqual({ candidate: null, recovered: true });
    expect(storage.getItem(WORKSPACE_PREFERENCE_KEY)).toBeNull();
  });

  it("keeps valid data until experience and explicit workspace creation are confirmed", () => {
    const serialized = JSON.stringify({
      version: 1,
      experience: "everyday",
      scope: "personal",
      runtime: "local",
      onboardingComplete: true
    });
    storage.setItem(WORKSPACE_PREFERENCE_KEY, serialized);

    completeLegacyPreferenceMigration(storage, {
      experienceConfirmed: true,
      workspaceCreationConfirmed: false
    });
    expect(storage.getItem(WORKSPACE_PREFERENCE_KEY)).toBe(serialized);

    completeLegacyPreferenceMigration(storage, {
      experienceConfirmed: true,
      workspaceCreationConfirmed: true
    });
    expect(storage.getItem(WORKSPACE_PREFERENCE_KEY)).toBeNull();
  });

  it("removes a valid migration candidate only when the person dismisses it", () => {
    storage.setItem(
      WORKSPACE_PREFERENCE_KEY,
      JSON.stringify({
        version: 1,
        experience: "everyday",
        scope: "team",
        runtime: "local",
        onboardingComplete: true
      })
    );

    dismissLegacyPreferenceMigration(storage);

    expect(storage.getItem(WORKSPACE_PREFERENCE_KEY)).toBeNull();
  });
});
