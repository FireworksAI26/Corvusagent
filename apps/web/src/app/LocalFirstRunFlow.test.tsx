import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LocalFirstRunFlow } from "./LocalFirstRunFlow";

describe("LocalFirstRunFlow", () => {
  it("opens with a concise introduction before local profile setup", async () => {
    const onComplete = vi.fn();
    const user = userEvent.setup();
    render(<LocalFirstRunFlow onComplete={onComplete} />);

    expect(screen.getByRole("heading", { name: "Welcome to Corvus" })).toBeVisible();
    expect(screen.getByText("Plan, build, and review agent work with clear safety boundaries.")).toBeVisible();
    expect(screen.queryByText(/device-local identity/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Google/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("radio", { name: "Everyday" }));
    await user.click(screen.getByRole("button", { name: "Open Corvus" }));
    expect(onComplete).toHaveBeenCalledWith("everyday", "individual");
  });
});
