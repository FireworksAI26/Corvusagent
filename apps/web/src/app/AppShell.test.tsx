import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppShell } from "./AppShell";
import { getWorkspaceProfile } from "./workspaceProfiles";

const WORKSPACE = {
  id: "workspace-1",
  name: "Field desk",
  workspace_kind: "individual",
  status: "active",
  created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
  version: 1
} as const;

function shell(inspectorOpen: boolean) {
  return (
    <AppShell
      accountEmail="person@example.com"
      activeRoute="threads"
      error=""
      inspector={<div>Inspector content</div>}
      inspectorOpen={inspectorOpen}
      onNavigate={vi.fn()}
      onWorkspaceSelect={vi.fn()}
      profile={getWorkspaceProfile("everyday", "individual")}
      projectContext={<div>Project context</div>}
      selectedWorkspace={WORKSPACE}
      selectionRequired={false}
      workspaces={[WORKSPACE]}
    >
      <div>Conversation content</div>
    </AppShell>
  );
}

describe("AppShell", () => {
  it("does not reserve or render the inspector while it is closed", () => {
    const { container } = render(shell(false));

    expect(screen.getByText("Conversation content")).toBeVisible();
    expect(screen.queryByText("Inspector content")).not.toBeInTheDocument();
    expect(container.querySelector(".adaptive-inspector-overlay")).toBeNull();
    expect(container.querySelector(".adaptive-shell")).toHaveAttribute("data-inspector", "closed");
  });

  it("renders the inspector in an on-demand overlay without changing the shell grid", () => {
    const { container } = render(shell(true));

    expect(screen.getByText("Inspector content")).toBeVisible();
    expect(container.querySelector(".adaptive-inspector-overlay")).not.toBeNull();
    expect(container.querySelector(".adaptive-shell")).toHaveAttribute("data-inspector", "open");
    expect(screen.getAllByText("Everyday · Individual")).not.toHaveLength(0);
  });
});
