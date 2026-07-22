import type { components } from "../generated/api";
import { localWorkspaceUrl } from "./localRuntime";

interface LocalRuntimeLauncherProps {
  experience: components["schemas"]["ExperienceKind"];
  workspaceKind: components["schemas"]["WorkspaceKind"];
}

export function LocalRuntimeLauncher({
  experience,
  workspaceKind
}: LocalRuntimeLauncherProps) {
  return (
    <div className="local-launch-shell">
      <section className="local-launch-panel" aria-labelledby="local-launch-title">
        <div className="preview-badge">Local workspace</div>
        <p className="eyebrow">{experience} · {workspaceKind}</p>
        <h1 id="local-launch-title">Open Corvus on this computer.</h1>
        <p className="local-launch-lede">
          The hosted page never receives your local session or pairing value. Continue on the
          same-origin local page so Corvus can keep its cookie, CSRF, and authority boundaries
          intact.
        </p>
        <ol className="local-launch-steps">
          <li><span>1</span><p><strong>Start Corvus locally</strong><small>Run the desktop app or the documented local server command.</small></p></li>
          <li><span>2</span><p><strong>Open the workspace</strong><small>Your browser moves to the loopback-only Corvus service.</small></p></li>
          <li><span>3</span><p><strong>Pair once</strong><small>The one-time value stays on this computer.</small></p></li>
        </ol>
        <div className="cloud-preview-actions">
          <a className="button button--primary" href={localWorkspaceUrl()}>Open local Corvus</a>
        </div>
        <p className="local-launch-note">
          Requires Corvus at 127.0.0.1:8080. This alpha handoff does not verify which app owns
          port 8080; start Corvus yourself before continuing.
        </p>
      </section>
    </div>
  );
}
