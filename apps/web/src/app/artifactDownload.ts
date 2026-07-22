import { invoke, isTauri } from "@tauri-apps/api/core";

const DEFAULT_ARTIFACT_NAME = "corvus-finished-project.zip";
const MAX_DESKTOP_ARTIFACT_BYTES = 256 * 1024 * 1024;

function safeArtifactName(value: string): string {
  const name = value.trim();
  if (name === "" || name.length > 120 || !/^[A-Za-z0-9._-]+\.zip$/i.test(name)) {
    return DEFAULT_ARTIFACT_NAME;
  }
  return name;
}

export async function downloadArtifact(url: string, suggestedName: string): Promise<string | null> {
  const downloadName = safeArtifactName(suggestedName);
  if (isTauri()) {
    const response = await fetch(url, { credentials: "include", headers: { Accept: "application/zip" } });
    if (!response.ok) throw new Error(`artifact_download_failed_${response.status}`);
    const advertisedSize = Number(response.headers.get("Content-Length"));
    if (Number.isFinite(advertisedSize) && advertisedSize > MAX_DESKTOP_ARTIFACT_BYTES) {
      throw new Error("artifact_download_too_large");
    }
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength === 0 || bytes.byteLength > MAX_DESKTOP_ARTIFACT_BYTES) {
      throw new Error("artifact_download_size_invalid");
    }
    return await invoke<string | null>("save_artifact_file", {
      suggestedName: downloadName,
      contents: bytes
    });
  }

  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = downloadName;
  anchor.rel = "noopener";
  anchor.click();
  return null;
}
