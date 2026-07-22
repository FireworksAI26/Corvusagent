import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { getWorkspaceProfile } from "./workspaceProfiles";
import { WorkspaceRouter } from "./WorkspaceRouter";

describe("WorkspaceRouter", () => {
  it("renders a truthful read-only settings placeholder", () => {
    render(
      <WorkspaceRouter
        activeRoute="settings"
        executionSurface={<div>Execution</div>}
        operationsSurface={<div>Operations</div>}
        profile={getWorkspaceProfile("developer", "individual")}
        projectName="Corvus"
      />
    );

    expect(screen.getByRole("heading", { name: "Workspace settings" })).toBeVisible();
    expect(screen.getByText(/editing is not available yet/i)).toBeVisible();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("does not send an unexposed route to an unrelated execution surface", () => {
    render(
      <WorkspaceRouter
        activeRoute="files"
        executionSurface={<div>Execution</div>}
        operationsSurface={<div>Operations</div>}
        profile={getWorkspaceProfile("developer", "individual")}
        projectName="Corvus"
      />
    );

    expect(screen.getByRole("heading", { name: "View unavailable" })).toBeVisible();
    expect(screen.queryByText("Execution")).not.toBeInTheDocument();
    expect(screen.queryByText("Operations")).not.toBeInTheDocument();
  });

  it.each([
    ["repositories", "Execution"],
    ["runs", "Execution"],
    ["skills", "Operations"],
    ["threads", "Execution"]
  ] as const)("maps the exposed %s route intentionally", (activeRoute, expectedSurface) => {
    render(
      <WorkspaceRouter
        activeRoute={activeRoute}
        executionSurface={<div>Execution</div>}
        operationsSurface={<div>Operations</div>}
        profile={getWorkspaceProfile("developer", "individual")}
        projectName="Corvus"
      />
    );

    expect(screen.getByText(expectedSurface)).toBeVisible();
  });

  it("gives the exposed schedule route its own truthful landing", () => {
    render(
      <WorkspaceRouter
        activeRoute="schedule"
        executionSurface={<div>Execution</div>}
        operationsSurface={<div>Operations</div>}
        profile={getWorkspaceProfile("developer", "individual")}
        projectName="Corvus"
      />
    );

    expect(screen.getByRole("heading", { name: "Schedule" })).toBeVisible();
    expect(screen.queryByText("Execution")).not.toBeInTheDocument();
  });
});
