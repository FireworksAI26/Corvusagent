import { describe, expect, it } from "vitest";

import { isLoopbackRuntimeHost, localWorkspaceUrl } from "./localRuntime";

describe("local runtime routing", () => {
  it.each(["127.0.0.1", "localhost", "::1", "[::1]"])(
    "recognizes %s as a same-machine runtime host",
    (hostname) => {
      expect(isLoopbackRuntimeHost(hostname)).toBe(true);
    }
  );

  it("keeps hosted origins outside the local trust boundary", () => {
    expect(isLoopbackRuntimeHost("corvus-platform.vercel.app")).toBe(false);
  });

  it("rejects missing runtime hostnames without throwing", () => {
    expect(isLoopbackRuntimeHost(null as unknown as string)).toBe(false);
    expect(isLoopbackRuntimeHost(undefined as unknown as string)).toBe(false);
  });

  it("uses the documented loopback workspace endpoint", () => {
    expect(localWorkspaceUrl()).toBe("http://127.0.0.1:8080/");
  });
});
