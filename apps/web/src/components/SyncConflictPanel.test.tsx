import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SyncConflictPanel } from "./SyncConflictPanel";

describe("SyncConflictPanel", () => {
  it("shows both versions and exposes explicit reload and retry actions", async () => {
    const user = userEvent.setup();
    const onReload = vi.fn();
    const onRetry = vi.fn();
    render(
      <SyncConflictPanel
        currentVersion={3}
        desiredVersion={1}
        onReload={onReload}
        onRetry={onRetry}
      />
    );

    expect(screen.getByText("Your version: 1")).toBeVisible();
    expect(screen.getByText("Current version: 3")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Reload current workspace" }));
    await user.click(screen.getByRole("button", { name: "Retry with current version" }));
    expect(onReload).toHaveBeenCalledTimes(1);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
