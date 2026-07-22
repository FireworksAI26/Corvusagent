import { describe, expect, it } from "vitest";

import { notificationForRunStatus } from "./BackgroundRunNotifier";

describe("notificationForRunStatus", () => {
  it("uses redacted copy without repository, prompt, or diagnostic content", () => {
    expect(notificationForRunStatus("review_required")).toEqual({
      title: "Run ready for review",
      body: "Open Corvus to inspect the changes."
    });
    expect(notificationForRunStatus("completed")).toEqual({
      title: "Run completed",
      body: "Open Corvus to review the result."
    });
    expect(notificationForRunStatus("failed")).toEqual({
      title: "Run needs attention",
      body: "Open Corvus to view redacted diagnostics."
    });
    expect(notificationForRunStatus("interrupted")).toEqual({
      title: "Run needs attention",
      body: "Open Corvus to view redacted diagnostics."
    });
    expect(notificationForRunStatus("running")).toBeNull();
  });
});
