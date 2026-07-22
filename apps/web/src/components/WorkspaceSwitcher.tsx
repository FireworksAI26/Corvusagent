import { useEffect, useRef, useState } from "react";

import type { components } from "../generated/api";
import { focusFirstControl, trapDialogFocus } from "./dialogFocus";

type ExperienceKind = components["schemas"]["ExperienceKind"];
type Workspace = components["schemas"]["Workspace"];

interface WorkspaceIdentityBlockProps {
  accountEmail: string;
  experience: ExperienceKind;
  legacyPreferencePending?: boolean;
  onDismissLegacyPreference?(): void;
  onNavigateSettings?(): void;
  onWorkspaceSelect(workspaceId: string): void | Promise<void>;
  selectedWorkspace: Workspace;
  selectionRequired?: boolean;
  workspaces: readonly Workspace[];
}

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

export function WorkspaceIdentityBlock({
  accountEmail,
  experience,
  legacyPreferencePending = false,
  onDismissLegacyPreference,
  onNavigateSettings,
  onWorkspaceSelect,
  selectedWorkspace,
  selectionRequired = false,
  workspaces
}: WorkspaceIdentityBlockProps) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState("");

  useEffect(() => {
    if (open) focusFirstControl(dialogRef.current);
  }, [open]);

  async function selectWorkspace(workspaceId: string, force = false) {
    if (!force && workspaceId === selectedWorkspace.id) return;
    setStatus("Switching workspace…");
    try {
      await onWorkspaceSelect(workspaceId);
      setStatus("Workspace selected");
    } catch {
      setStatus("Workspace could not be selected. Your current workspace is unchanged.");
    }
  }

  function close() {
    setOpen(false);
    triggerRef.current?.focus();
  }

  return (
    <div className="workspace-identity-block" data-source-refs="corvus-platform">
      <button
        aria-label="Open workspace identity"
        aria-expanded={open}
        className="workspace-identity-trigger"
        data-action="open-workspace-identity"
        onClick={() => setOpen(true)}
        ref={triggerRef}
        type="button"
      >
        <span>{selectedWorkspace.name}</span>
        <strong>{titleCase(experience)} · {titleCase(selectedWorkspace.workspace_kind)}</strong>
      </button>
      {workspaces.length > 1 && (
        <label className="workspace-authorized-picker">
          <span>Authorized workspace</span>
          <select
            aria-label="Authorized workspace"
            data-action="switch-workspace"
            onChange={(event) => void selectWorkspace(event.target.value)}
            value={selectedWorkspace.id}
          >
            {workspaces.map((workspace) => (
              <option key={workspace.id} value={workspace.id}>{workspace.name}</option>
            ))}
          </select>
        </label>
      )}
      {selectionRequired && workspaces.length > 0 && (
        <button
          className="button button--quiet"
          data-action="switch-workspace"
          onClick={() => {
            const candidate = workspaces.find((workspace) => workspace.id !== selectedWorkspace.id)
              ?? workspaces[0];
            if (candidate !== undefined) void selectWorkspace(candidate.id, true);
          }}
          type="button"
        >
          {workspaces.length === 1 && workspaces[0]?.id !== selectedWorkspace.id
            ? `Select ${workspaces[0].name}`
            : "Re-select workspace"}
        </button>
      )}
      {selectionRequired && workspaces.length === 0 && (
        <p role="status">
          No authorized workspaces are available. Ask a workspace owner to restore access.
        </p>
      )}
      <span aria-live="polite" className="sr-only">{status}</span>
      {open && (
        <div
          aria-labelledby="workspace-identity-heading"
          aria-modal="true"
          className="workspace-identity-dialog"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              close();
            }
            else trapDialogFocus(event, dialogRef.current);
          }}
          ref={dialogRef}
          role="dialog"
          tabIndex={-1}
        >
          <div className="section-heading">
            <h2 id="workspace-identity-heading">Workspace identity</h2>
            <button onClick={close} type="button">Close workspace identity</button>
          </div>
          <dl>
            <div><dt>Workspace</dt><dd>{selectedWorkspace.name}</dd></div>
            <div><dt>Profile</dt><dd>{titleCase(experience)} · {titleCase(selectedWorkspace.workspace_kind)}</dd></div>
            <div><dt>Account</dt><dd>{accountEmail}</dd></div>
          </dl>
          <button
            data-action="change-workspace-profile"
            onClick={() => {
              close();
              onNavigateSettings?.();
            }}
            type="button"
          >
            Open profile settings
          </button>
          {legacyPreferencePending && (
            <button onClick={onDismissLegacyPreference} type="button">
              Dismiss previous setup
            </button>
          )}
        </div>
      )}
    </div>
  );
}
