import { useState } from "react";

interface SyncConflictPanelProps {
  currentVersion: number;
  desiredVersion: number;
  onReload(): void | Promise<void>;
  onRetry(): void | Promise<void>;
}

export function SyncConflictPanel({
  currentVersion,
  desiredVersion,
  onReload,
  onRetry
}: SyncConflictPanelProps) {
  const [error, setError] = useState("");

  async function run(action: () => void | Promise<void>) {
    setError("");
    try {
      await action();
    } catch {
      setError("The workspace could not be updated. Reload its current version and try again.");
    }
  }

  return (
    <section aria-labelledby="sync-conflict-title" className="workspace-landing">
      <p className="eyebrow">Workspace changed elsewhere</p>
      <h1 id="sync-conflict-title">Review the newer workspace version.</h1>
      <p>Your version: {desiredVersion}</p>
      <p>Current version: {currentVersion}</p>
      {error && <p role="alert">{error}</p>}
      <div className="button-row">
        <button onClick={() => void run(onReload)} type="button">Reload current workspace</button>
        <button onClick={() => void run(onRetry)} type="button">Retry with current version</button>
      </div>
    </section>
  );
}
