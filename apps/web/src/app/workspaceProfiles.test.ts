import { describe, expect, it } from "vitest";

import { getWorkspaceDefaultRoute, getWorkspaceProfile } from "./workspaceProfiles";

describe("workspace profiles", () => {
  it.each([
    ["everyday", "individual", ["Conversations", "Projects", "Activity", "Schedule", "Skills", "Settings"]],
    ["developer", "individual", ["Projects", "Runs", "Schedule", "Skills", "Threads", "Settings"]],
    ["everyday", "team", ["Conversations", "Projects", "Activity", "Schedule", "Skills", "Settings"]],
    ["developer", "team", ["Projects", "Runs", "Schedule", "Skills", "Threads", "Settings"]]
  ] as const)("defines %s %s navigation", (experience, scope, labels) => {
    expect(getWorkspaceProfile(experience, scope).routes.map((route) => route.label)).toEqual(labels);
  });

  it("never exposes routes without a dedicated shipped surface", () => {
    const unfinishedRoutes = new Set([
      "my-work",
      "files",
      "assigned-work",
      "approvals",
      "people",
      "reviews",
      "policies"
    ]);

    for (const experience of ["everyday", "developer"] as const) {
      for (const workspaceKind of ["individual", "team"] as const) {
        const exposed = getWorkspaceProfile(experience, workspaceKind).routes.map((route) => route.id);
        expect(exposed.filter((routeId) => unfinishedRoutes.has(routeId))).toEqual([]);
      }
    }
  });

  it("opens the new-thread surface even when Threads is lower in the navigation", () => {
    expect(getWorkspaceDefaultRoute(getWorkspaceProfile("developer", "individual"))).toBe("threads");
  });

  it.each(["everyday", "developer"] as const)(
    "keeps every local feature handoff reachable in the %s profile",
    (experience) => {
      for (const workspaceKind of ["individual", "team"] as const) {
        const routeIds = new Set(
          getWorkspaceProfile(experience, workspaceKind).routes.map((route) => route.id)
        );
        for (const required of ["threads", "repositories", "runs", "schedule", "skills", "settings"]) {
          expect(routeIds.has(required)).toBe(true);
        }
      }
    }
  );
});
