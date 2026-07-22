import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkspaceErrorBoundary } from "./WorkspaceErrorBoundary";

function BrokenWorkspace(): never {
  throw new Error("render failed");
}

describe("WorkspaceErrorBoundary", () => {
  afterEach(() => vi.restoreAllMocks());

  it("contains a workspace render failure and offers a safe recovery", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    render(
      <WorkspaceErrorBoundary>
        <BrokenWorkspace />
      </WorkspaceErrorBoundary>
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Your saved work is unchanged.");
    expect(screen.getByRole("button", { name: "Reload workspace" })).toBeVisible();
  });
});
