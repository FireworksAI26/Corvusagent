import { beforeEach, describe, expect, it, vi } from "vitest";

const { invoke, isTauri } = vi.hoisted(() => ({
  invoke: vi.fn(),
  isTauri: vi.fn()
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke, isTauri }));

import { HOSTED_GOOGLE_SIGN_IN_URL, openHostedGoogleSignIn } from "./externalAuth";

describe("openHostedGoogleSignIn", () => {
  beforeEach(() => {
    invoke.mockReset();
    isTauri.mockReset();
  });

  it("opens the hosted Google flow in a new browser tab", async () => {
    isTauri.mockReturnValue(false);
    const openWindow = vi.fn().mockReturnValue({});

    await openHostedGoogleSignIn(openWindow);

    expect(openWindow).toHaveBeenCalledWith(HOSTED_GOOGLE_SIGN_IN_URL);
  });

  it("uses the constrained native opener in the desktop app", async () => {
    isTauri.mockReturnValue(true);
    invoke.mockResolvedValue(undefined);

    await openHostedGoogleSignIn();

    expect(invoke).toHaveBeenCalledWith("open_external_url", {
      url: HOSTED_GOOGLE_SIGN_IN_URL
    });
  });

  it("reports a blocked browser popup", async () => {
    isTauri.mockReturnValue(false);

    await expect(openHostedGoogleSignIn(() => null)).rejects.toThrow("google_sign_in_popup_blocked");
  });
});
