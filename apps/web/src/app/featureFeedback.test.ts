import { describe, expect, it } from "vitest";

import { ApiFailure } from "../api";
import { featureFailure } from "./featureFeedback";

describe("featureFailure", () => {
  it("turns stable backend codes into actionable product language", () => {
    expect(featureFailure(new Error("codex_unavailable"), "run").message).toMatch(/check the CLI installation/i);
  });

  it("preserves typed detail and correlation identifiers", () => {
    const reason = new ApiFailure(409, { code: "skill_digest_mismatch", correlation_id: "corr-7" });
    expect(reason.message).toBe("skill_digest_mismatch");
    expect(reason.correlationId).toBe("corr-7");
    expect(featureFailure(reason, "skill")).toEqual({
      code: "skill_digest_mismatch",
      correlationId: "corr-7",
      message: expect.stringMatching(/review the latest version/i)
    });
  });

  it("humanizes unknown safe codes without exposing stack information", () => {
    expect(featureFailure(new Error("repository_refresh_failed"), "repository").message)
      .toBe("Repository refresh failed.");
  });

  it("preserves string errors returned by native desktop commands", () => {
    expect(featureFailure("browser_launch_failed", "settings")).toEqual({
      code: "browser_launch_failed",
      correlationId: null,
      message: "Corvus could not open your browser. Open Corvus Web and sign in there instead."
    });
  });

  it("explains lifecycle gates as user actions instead of internal state codes", () => {
    expect(featureFailure(new Error("schedule_run_already_active"), "schedule").message)
      .toMatch(/already has work in progress/i);
    expect(featureFailure(new Error("contribution_run_not_reviewable"), "contribution").message)
      .toMatch(/wait for it to reach review/i);
  });
});
