import { beforeEach, describe, expect, it, vi } from "vitest";

const tauri = vi.hoisted(() => ({ invoke: vi.fn(), isTauri: vi.fn() }));

vi.mock("@tauri-apps/api/core", () => tauri);

import { downloadArtifact } from "./artifactDownload";

describe("downloadArtifact", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    tauri.invoke.mockReset();
    tauri.isTauri.mockReset();
  });

  it("opens the native save flow with authenticated artifact bytes", async () => {
    tauri.isTauri.mockReturnValue(true);
    tauri.invoke.mockResolvedValue("C:\\Downloads\\corvus-project.zip");
    const fetchArtifact = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(new Uint8Array([80, 75, 3, 4]), { status: 200 })
    );

    await expect(downloadArtifact("/api/artifact", "corvus-project.zip")).resolves.toBe(
      "C:\\Downloads\\corvus-project.zip"
    );

    expect(fetchArtifact).toHaveBeenCalledWith("/api/artifact", {
      credentials: "include",
      headers: { Accept: "application/zip" }
    });
    expect(tauri.invoke).toHaveBeenCalledWith("save_artifact_file", {
      suggestedName: "corvus-project.zip",
      contents: new Uint8Array([80, 75, 3, 4])
    });
  });

  it("replaces an unsafe server filename before invoking the desktop", async () => {
    tauri.isTauri.mockReturnValue(true);
    tauri.invoke.mockResolvedValue(null);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(new Uint8Array([80, 75])));

    await downloadArtifact("/api/artifact", "../outside.zip");

    expect(tauri.invoke).toHaveBeenCalledWith("save_artifact_file", expect.objectContaining({
      suggestedName: "corvus-finished-project.zip"
    }));
  });
});
