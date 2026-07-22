import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { getWorkspaceProfile } from "../app/workspaceProfiles";
import { NavigationRail } from "./NavigationRail";

const WORKSPACE = {
  id: "workspace-1",
  name: "Field desk",
  workspace_kind: "individual",
  status: "active",
  created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
  version: 1
} as const;

describe("NavigationRail", () => {
  it("keeps project context secondary behind an accessible disclosure", async () => {
    render(
      <NavigationRail
        accountEmail="person@example.com"
        activeRoute="threads"
        onNavigate={vi.fn()}
        onWorkspaceSelect={vi.fn()}
        profile={getWorkspaceProfile("everyday", "individual")}
        projectContext={<div data-testid="project-context">Projects</div>}
        selectedWorkspace={WORKSPACE}
        workspaces={[WORKSPACE]}
      />
    );
    const user = userEvent.setup();

    const disclosure = screen.getByRole("button", { name: "Projects" });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("project-context")).not.toBeInTheDocument();

    await user.click(disclosure);
    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("project-context")).toBeVisible();
  });

  it("pairs route labels with familiar decorative icons instead of sequence numbers", () => {
    render(
      <NavigationRail
        accountEmail="person@example.com"
        activeRoute="threads"
        onNavigate={vi.fn()}
        onWorkspaceSelect={vi.fn()}
        profile={getWorkspaceProfile("everyday", "individual")}
        projectContext={<div>Projects</div>}
        selectedWorkspace={WORKSPACE}
        workspaces={[WORKSPACE]}
      />
    );

    const navigation = screen.getByRole("navigation", { name: "Everyday / Individual navigation" });
    expect(navigation.querySelectorAll("svg[aria-hidden='true']")).toHaveLength(6);
    expect(navigation).not.toHaveTextContent("01");
  });
});
