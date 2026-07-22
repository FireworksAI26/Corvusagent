import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Routine, SkillVersion } from "../api";
import { RoutinesWorkspace } from "./RoutinesWorkspace";

describe("RoutinesWorkspace", () => {
  it("creates an authorized routine and runs it now without claiming timed scheduling", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const onRun = vi.fn().mockResolvedValue(undefined);
    const skill = { id: "skill-1", name: "Daily brief", version: 1, status: "active" } as SkillVersion;
    const routine = { id: "routine-1", name: "Morning brief", skill_version_id: "skill-1" } as Routine;
    render(
      <RoutinesWorkspace
        busy={false}
        onCreate={onCreate}
        onRun={onRun}
        projectName="Field desk"
        routines={[routine]}
        skills={[skill]}
      />
    );
    const user = userEvent.setup();

    expect(screen.getByText(/Timed recurrence is coming soon/i)).toBeVisible();
    await user.type(screen.getByLabelText("Routine name"), "Release brief");
    await user.selectOptions(screen.getByLabelText("Skill"), "skill-1");
    await user.click(screen.getByRole("button", { name: "Create routine" }));
    expect(onCreate).toHaveBeenCalledWith("Release brief", "skill-1");

    await user.click(screen.getByRole("button", { name: "Run Morning brief now" }));
    expect(onRun).toHaveBeenCalledWith("routine-1");
  });
});
