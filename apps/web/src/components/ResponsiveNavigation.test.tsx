import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../generated/api";
import { getWorkspaceProfile } from "../app/workspaceProfiles";
import { ResponsiveNavigation } from "./ResponsiveNavigation";

const WORKSPACE: components["schemas"]["Workspace"] = {
  id: "33333333-3333-4333-8333-333333333333",
  name: "Corvus field desk",
  workspace_kind: "individual",
  status: "active",
  created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
  version: 1
};

describe("ResponsiveNavigation", () => {
  it("keeps mobile identity read-only and restores More focus after closing", async () => {
    const user = userEvent.setup();
    render(
      <ResponsiveNavigation
        accountEmail="person@example.com"
        activeRoute="home"
        onNavigate={vi.fn()}
        onWorkspaceSelect={vi.fn()}
        profile={getWorkspaceProfile("everyday", "individual")}
        selectedWorkspace={WORKSPACE}
        workspaces={[WORKSPACE]}
      />
    );

    const more = screen.getByText("More");
    expect(more).toHaveAttribute("data-action", "mobile-more");
    await user.click(more);
    expect(screen.getByText("Everyday · Individual")).toBeVisible();
    const close = screen.getByRole("button", { name: "Close More menu" });
    expect(close).toHaveFocus();
    expect(screen.queryByRole("button", { name: /Developer work style/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Team workspace/ })).not.toBeInTheDocument();

    const identity = screen.getByRole("button", { name: "Open workspace identity" });
    identity.focus();
    await user.tab();
    expect(close).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(more).toHaveFocus();
  });

  it("closes only nested identity on Escape and keeps More open", async () => {
    const user = userEvent.setup();
    render(
      <ResponsiveNavigation
        accountEmail="person@example.com"
        activeRoute="home"
        onNavigate={vi.fn()}
        onWorkspaceSelect={vi.fn()}
        profile={getWorkspaceProfile("everyday", "individual")}
        selectedWorkspace={WORKSPACE}
        workspaces={[WORKSPACE]}
      />
    );

    await user.click(screen.getByRole("button", { name: "More" }));
    const identityTrigger = screen.getByRole("button", { name: "Open workspace identity" });
    await user.click(identityTrigger);
    expect(screen.getByRole("dialog", { name: "Workspace identity" })).toBeVisible();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "Workspace identity" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "More navigation" })).toBeVisible();
    expect(identityTrigger).toHaveFocus();
  });
});
