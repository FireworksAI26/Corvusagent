import { useId, useState, type ReactNode } from "react";

import type { WorkspaceProfile } from "../app/workspaceProfiles";
import type { components } from "../generated/api";
import { BrandLockup } from "./Brand";
import { WorkspaceIdentityBlock } from "./WorkspaceSwitcher";

type Workspace = components["schemas"]["Workspace"];

function NavigationIcon({ routeId }: { routeId: string }) {
  const commonProps = {
    "aria-hidden": true,
    className: "nav-icon",
    fill: "none",
    viewBox: "0 0 24 24"
  } as const;

  switch (routeId) {
    case "threads":
      return <svg {...commonProps}><path d="M5 5.5h14v10H9l-4 3v-13Z" /></svg>;
    case "schedule":
      return <svg {...commonProps}><path d="M6 3v3m12-3v3M4 9h16M5 5h14a1 1 0 0 1 1 1v13H4V6a1 1 0 0 1 1-1Z" /></svg>;
    case "files":
    case "repositories":
      return <svg {...commonProps}><path d="M3.5 6.5h6l2 2h9v9.5a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18V6.5Z" /></svg>;
    case "runs":
      return <svg {...commonProps}><path d="m9 7 7 5-7 5V7Z" /><circle cx="12" cy="12" r="9" /></svg>;
    case "skills":
      return <svg {...commonProps}><path d="m12 3 1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6L12 3Zm6 12 .7 2.3L21 18l-2.3.7L18 21l-.7-2.3L15 18l2.3-.7L18 15Z" /></svg>;
    case "people":
      return <svg {...commonProps}><circle cx="9" cy="8" r="3" /><path d="M3.5 19c.7-4 2.5-6 5.5-6s4.8 2 5.5 6m1-10a2.5 2.5 0 0 1 0 5m.5 1c2.3.3 3.7 1.6 4.3 4" /></svg>;
    case "policies":
      return <svg {...commonProps}><path d="M12 3.5 19 6v5.5c0 4.2-2.3 7-7 9-4.7-2-7-4.8-7-9V6l7-2.5Z" /><path d="m9 12 2 2 4-4" /></svg>;
    case "settings":
      return <svg {...commonProps}><circle cx="12" cy="12" r="3" /><path d="M19 12a7 7 0 0 0-.1-1l2-1.6-2-3.4-2.5 1a8 8 0 0 0-1.8-1L14.2 3h-4.4l-.4 3a8 8 0 0 0-1.8 1L5.1 6 3 9.4 5.1 11a7 7 0 0 0 0 2L3 14.6 5.1 18l2.5-1a8 8 0 0 0 1.8 1l.4 3h4.4l.4-3a8 8 0 0 0 1.8-1l2.5 1 2-3.4-2-1.6a7 7 0 0 0 .1-1Z" /></svg>;
    default:
      return <svg {...commonProps}><path d="M5 4h14v16H5V4Zm3 4h8M8 12h8m-8 4h5" /></svg>;
  }
}

interface NavigationRailProps {
  accountEmail: string;
  activeRoute: string;
  legacyPreferencePending?: boolean;
  onDismissLegacyPreference?(): void;
  onNavigate(routeId: string): void;
  onWorkspaceSelect(workspaceId: string): void | Promise<void>;
  profile: WorkspaceProfile;
  projectContext: ReactNode;
  selectedWorkspace: Workspace;
  selectionRequired?: boolean;
  workspaces: readonly Workspace[];
}

export function NavigationRail({
  accountEmail,
  activeRoute,
  legacyPreferencePending = false,
  onDismissLegacyPreference,
  onNavigate,
  onWorkspaceSelect,
  profile,
  projectContext,
  selectedWorkspace,
  selectionRequired = false,
  workspaces
}: NavigationRailProps) {
  const projectsId = useId();
  const [projectsOpen, setProjectsOpen] = useState(false);

  return (
    <aside aria-label="Workspace navigation rail" className="adaptive-rail">
      <BrandLockup className="adaptive-wordmark" />
      <WorkspaceIdentityBlock
        accountEmail={accountEmail}
        experience={profile.experience}
        legacyPreferencePending={legacyPreferencePending}
        onDismissLegacyPreference={onDismissLegacyPreference}
        onNavigateSettings={() => onNavigate("settings")}
        onWorkspaceSelect={onWorkspaceSelect}
        selectedWorkspace={selectedWorkspace}
        selectionRequired={selectionRequired}
        workspaces={workspaces}
      />
      <nav aria-label={`${profile.label} navigation`} className="profile-navigation">
        {profile.routes.map((route) => (
          <a
            aria-current={activeRoute === route.id ? "page" : undefined}
            href={`#${route.id}`}
            key={route.id}
            onClick={(event) => {
              event.preventDefault();
              onNavigate(route.id);
            }}
            title={route.description}
          >
            <NavigationIcon routeId={route.id} />
            {route.label}
          </a>
        ))}
      </nav>
      <div className="sidebar-projects">
        <button
          aria-controls={projectsId}
          aria-expanded={projectsOpen}
          className="sidebar-projects__trigger"
          onClick={() => setProjectsOpen((current) => !current)}
          type="button"
        >
          <svg aria-hidden="true" className="nav-icon" fill="none" viewBox="0 0 24 24"><path d="m8 10 4 4 4-4" /></svg>
          Projects
        </button>
        {projectsOpen ? <div id={projectsId}>{projectContext}</div> : null}
      </div>
    </aside>
  );
}
