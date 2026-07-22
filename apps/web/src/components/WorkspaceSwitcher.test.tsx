import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../generated/api";
import { WorkspaceIdentityBlock } from "./WorkspaceSwitcher";

type Workspace = components["schemas"]["Workspace"];

const FIRST: Workspace = {
  id: "33333333-3333-4333-8333-333333333333",
  name: "Corvus field desk",
  workspace_kind: "individual",
  status: "active",
  created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
  version: 1
};
const SECOND: Workspace = { ...FIRST, id: "44444444-4444-4444-8444-444444444444", name: "Team flight desk", workspace_kind: "team" };

describe("WorkspaceIdentityBlock", () => {
  it("shows read-only profile labels and only authorized workspace selection", async () => {
    const onWorkspaceSelect = vi.fn().mockResolvedValue(undefined);
    render(
      <WorkspaceIdentityBlock
        accountEmail="person@example.com"
        experience="developer"
        onWorkspaceSelect={onWorkspaceSelect}
        selectedWorkspace={FIRST}
        workspaces={[FIRST, SECOND]}
      />
    );

    expect(screen.getByText("Developer · Individual")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Everyday/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Team workspace/ })).not.toBeInTheDocument();

    await userEvent.setup().selectOptions(
      screen.getByRole("combobox", { name: "Authorized workspace" }),
      SECOND.id
    );
    await waitFor(() => expect(onWorkspaceSelect).toHaveBeenCalledWith(SECOND.id));
  });

  it("opens read-only identity details and restores focus to its trigger", async () => {
    const user = userEvent.setup();
    render(
      <WorkspaceIdentityBlock
        accountEmail="person@example.com"
        experience="developer"
        onWorkspaceSelect={vi.fn()}
        selectedWorkspace={FIRST}
        workspaces={[FIRST]}
      />
    );
    const trigger = screen.getByRole("button", { name: "Open workspace identity" });
    expect(trigger).toHaveAttribute("data-action", "open-workspace-identity");
    await user.click(trigger);
    expect(screen.getByRole("dialog", { name: "Workspace identity" })).toBeVisible();
    expect(screen.getByText("person@example.com")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Close workspace identity" }));
    expect(trigger).toHaveFocus();
  });

  it("offers explicit re-selection after authority was invalidated even with one workspace", async () => {
    const onWorkspaceSelect = vi.fn().mockResolvedValue(undefined);
    render(
      <WorkspaceIdentityBlock
        accountEmail="person@example.com"
        experience="developer"
        onWorkspaceSelect={onWorkspaceSelect}
        selectedWorkspace={FIRST}
        selectionRequired
        workspaces={[FIRST]}
      />
    );

    await userEvent.setup().click(screen.getByRole("button", { name: "Re-select workspace" }));

    expect(onWorkspaceSelect).toHaveBeenCalledWith(FIRST.id);
  });

  it("offers the sole refreshed authority instead of retrying a stale selected workspace", async () => {
    const onWorkspaceSelect = vi.fn().mockResolvedValue(undefined);
    render(
      <WorkspaceIdentityBlock
        accountEmail="person@example.com"
        experience="developer"
        onWorkspaceSelect={onWorkspaceSelect}
        selectedWorkspace={FIRST}
        selectionRequired
        workspaces={[SECOND]}
      />
    );

    await userEvent.setup().click(screen.getByRole("button", { name: "Select Team flight desk" }));

    expect(onWorkspaceSelect).toHaveBeenCalledWith(SECOND.id);
    expect(onWorkspaceSelect).not.toHaveBeenCalledWith(FIRST.id);
  });

  it("opens Settings from an interactive profile action", async () => {
    const user = userEvent.setup();
    const onNavigateSettings = vi.fn();
    render(
      <WorkspaceIdentityBlock
        accountEmail="person@example.com"
        experience="developer"
        onNavigateSettings={onNavigateSettings}
        onWorkspaceSelect={vi.fn()}
        selectedWorkspace={FIRST}
        workspaces={[FIRST]}
      />
    );

    await user.click(screen.getByRole("button", { name: "Open workspace identity" }));
    await user.click(screen.getByRole("button", { name: "Open profile settings" }));

    expect(onNavigateSettings).toHaveBeenCalledTimes(1);
  });

  it("focuses and traps the identity dialog, closes with Escape, and restores its trigger", async () => {
    const user = userEvent.setup();
    render(
      <WorkspaceIdentityBlock
        accountEmail="person@example.com"
        experience="developer"
        onNavigateSettings={vi.fn()}
        onWorkspaceSelect={vi.fn()}
        selectedWorkspace={FIRST}
        workspaces={[FIRST]}
      />
    );

    const trigger = screen.getByRole("button", { name: "Open workspace identity" });
    await user.click(trigger);
    const close = screen.getByRole("button", { name: "Close workspace identity" });
    const settings = screen.getByRole("button", { name: "Open profile settings" });
    expect(close).toHaveFocus();

    settings.focus();
    await user.tab();
    expect(close).toHaveFocus();

    close.focus();
    await user.tab({ shift: true });
    expect(settings).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Workspace identity" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("shows truthful no-access guidance without a no-op re-selection control", () => {
    render(
      <WorkspaceIdentityBlock
        accountEmail="person@example.com"
        experience="developer"
        onWorkspaceSelect={vi.fn()}
        selectedWorkspace={FIRST}
        selectionRequired
        workspaces={[]}
      />
    );

    expect(screen.queryByRole("button", { name: "Re-select workspace" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "No authorized workspaces are available. Ask a workspace owner to restore access."
    );
  });
});
