import { useCallback, useEffect, useState } from "react";

import type { CloudRuntime, PlatformApi } from "../auth/authApi";
import { CloudIcon } from "../icons";

export function CloudWorkspace({
  api,
  csrfToken,
  workspaceId
}: {
  api: PlatformApi;
  csrfToken: string;
  workspaceId: string;
}) {
  const [runtime, setRuntime] = useState<CloudRuntime | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setError("");
    try {
      setRuntime(await api.getCloudRuntime(workspaceId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message.replaceAll("_", " ") : "Cloud runtime unavailable");
    }
  }, [api, workspaceId]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function act(operation: "provision" | "resume" | "pause" | "revoke") {
    setBusy(true);
    setError("");
    try {
      const next = operation === "provision"
        ? await api.provisionCloudRuntime(workspaceId, csrfToken, crypto.randomUUID())
        : operation === "resume"
          ? await api.resumeCloudRuntime(workspaceId, csrfToken)
          : operation === "pause"
            ? await api.pauseCloudRuntime(workspaceId, csrfToken)
            : await api.revokeCloudRuntime(workspaceId, csrfToken);
      setRuntime(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message.replaceAll("_", " ") : "Cloud operation failed");
    } finally {
      setBusy(false);
    }
  }

  const state = runtime?.state ?? "loading";
  const configured = runtime?.configured ?? true;
  return (
    <main className="cloud-preview-shell" id="main-content">
      <section className="cloud-preview-panel cloud-workspace-panel">
        <div className="preview-badge"><CloudIcon /> Corvus Cloud</div>
        <p className="eyebrow">E2B protected runtime</p>
        <h1>{state === "ready" ? "Your cloud workspace is ready." : "Run Corvus in the cloud."}</h1>
        <p className="cloud-preview-lede">
          Corvus provisions a dedicated E2B workspace, verifies readiness, preserves it on pause,
          and keeps the E2B API key on the server.
        </p>
        <div className="preview-route" aria-label="Cloud workspace lifecycle">
          <span>Corvus account</span><i aria-hidden="true" /><span>Governed control plane</span><i aria-hidden="true" /><span>E2B workspace</span>
        </div>
        <section className="preview-plan" aria-labelledby="cloud-runtime-title">
          <div>
            <p className="eyebrow">Runtime status</p>
            <h2 id="cloud-runtime-title">{state.replaceAll("_", " ")}</h2>
            <p>{runtime?.sandbox_id ? `Sandbox ${runtime.sandbox_id}` : "No sandbox has been provisioned for this workspace."}</p>
          </div>
          <span className={`cloud-runtime-state cloud-runtime-state--${state}`}>{state}</span>
        </section>
        {!configured ? <p className="setup-error" role="alert">Cloud is not configured on this deployment. Set E2B_API_KEY and CORVUS_E2B_TEMPLATE on the server.</p> : null}
        {error ? <p className="setup-error" role="alert">{error}</p> : null}
        <div className="cloud-preview-actions">
          {state === "unprovisioned" || state === "lost" || state === "failed" ? <button className="button button--primary" disabled={busy || !configured} onClick={() => void act("provision")} type="button">Provision cloud workspace</button> : null}
          {state === "paused" ? <button className="button button--primary" disabled={busy} onClick={() => void act("resume")} type="button">Resume workspace</button> : null}
          {state === "ready" ? <button className="button" disabled={busy} onClick={() => void act("pause")} type="button">Pause workspace</button> : null}
          {runtime?.sandbox_id ? <button className="text-button" disabled={busy} onClick={() => void act("revoke")} type="button">Delete cloud workspace</button> : null}
          <button className="text-button" disabled={busy} onClick={() => void refresh()} type="button">Refresh status</button>
        </div>
      </section>
    </main>
  );
}
