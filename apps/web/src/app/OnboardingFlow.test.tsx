import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../generated/api";
import { OnboardingFlow, type OnboardingFlowProps } from "./OnboardingFlow";
import { MemoryStorage } from "../test/memoryStorage";

type Workspace = components["schemas"]["Workspace"];

const WORKSPACE: Workspace = {
  id: "33333333-3333-4333-8333-333333333333",
  name: "Corvus field desk",
  workspace_kind: "individual",
  status: "active",
  created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
  version: 1
};

function onboardingProps(overrides: Partial<OnboardingFlowProps> = {}): OnboardingFlowProps {
  return {
    accountVersion: 4,
    authStatus: "authenticated",
    experienceKind: null,
    onCreateWorkspace: vi.fn().mockResolvedValue(WORKSPACE),
    onExperienceSaved: vi.fn().mockResolvedValue({ experience_kind: "developer", version: 5 }),
    onGoogleStart: vi.fn(),
    onWorkspaceConfirmed: vi.fn(),
    storage: new MemoryStorage(),
    ...overrides
  };
}

describe("OnboardingFlow", () => {
  it("puts Google before every profile, workspace, and runtime choice", async () => {
    const onGoogleStart = vi.fn();
    render(<OnboardingFlow {...onboardingProps({ authStatus: "unauthenticated", onGoogleStart })} />);

    expect(screen.getByRole("heading", { name: "Welcome to Corvus" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Continue with Google" })).toHaveAttribute(
      "data-action",
      "sign-in-google"
    );
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(onGoogleStart).toHaveBeenCalledOnce();
  });

  it("combines profile choices and saves an exact expected experience version", async () => {
    const onExperienceSaved = vi
      .fn()
      .mockResolvedValue({ experience_kind: "developer", version: 5 });
    const { rerender } = render(
      <OnboardingFlow {...onboardingProps({ onExperienceSaved })} />
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Developer/ }));
    await user.click(screen.getByRole("radio", { name: /Individual/ }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(onExperienceSaved).toHaveBeenCalledWith("developer", 4);
    expect(await screen.findByRole("heading", { name: "Choose protection and runtime" })).toHaveFocus();

    rerender(
      <OnboardingFlow
        {...onboardingProps({ experienceKind: "developer", onExperienceSaved })}
      />
    );
    expect(screen.queryByRole("radio", { name: /Everyday/ })).not.toBeInTheDocument();
  });

  it("requires explicit Team creation, keeps Join unavailable, and disables Cloud Preview", async () => {
    const onCreateWorkspace = vi.fn().mockResolvedValue({ ...WORKSPACE, workspace_kind: "team" });
    render(
      <OnboardingFlow
        {...onboardingProps({
          experienceKind: "developer",
          onCreateWorkspace
        })}
      />
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Team/ }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByRole("heading", { name: "Choose protection and runtime" })).toBeVisible();
    await user.click(screen.getByRole("radio", { name: /Detailed guidance/ }));
    expect(screen.getByRole("radio", { name: /Cloud Preview/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await user.type(screen.getByRole("textbox", { name: "Workspace name" }), "Platform team");
    expect(screen.getByText("Joining an existing workspace is not available yet.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Create workspace" }));

    expect(onCreateWorkspace).toHaveBeenCalledWith(
      { name: "Platform team", workspace_kind: "team" },
      expect.any(String)
    );
  });

  it("retries a failed create with the same idempotency key and preserves input and focus", async () => {
    const onCreateWorkspace = vi
      .fn()
      .mockRejectedValueOnce(new Error("workspace_create_failed"))
      .mockResolvedValueOnce(WORKSPACE);
    const onWorkspaceConfirmed = vi.fn();
    render(
      <OnboardingFlow
        {...onboardingProps({ experienceKind: "everyday", onCreateWorkspace, onWorkspaceConfirmed })}
      />
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Individual/ }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.type(screen.getByRole("textbox", { name: "Workspace name" }), "Corvus field desk");
    await user.click(screen.getByRole("button", { name: "Create workspace" }));

    const error = await screen.findByRole("alert");
    expect(error).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Workspace name" })).toHaveValue("Corvus field desk");
    const firstKey = onCreateWorkspace.mock.calls[0][1];

    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    await waitFor(() => expect(onWorkspaceConfirmed).toHaveBeenCalledWith(WORKSPACE));
    expect(onCreateWorkspace.mock.calls[1][1]).toBe(firstKey);
  });
});
