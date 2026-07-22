import { useEffect, useRef, type ReactNode } from "react";

import type { components } from "../generated/api";
import { BrandLockup } from "../components/Brand";
import { ConnectionBanner } from "../components/ConnectionBanner";
import { NavigationRail } from "../components/NavigationRail";
import { ResponsiveNavigation } from "../components/ResponsiveNavigation";
import type { WorkspaceProfile } from "./workspaceProfiles";

type Workspace = components["schemas"]["Workspace"];

interface AppShellProps {
  accountEmail: string;
  activeRoute: string;
  children: ReactNode;
  error: string;
  inspector: ReactNode;
  inspectorOpen: boolean;
  legacyPreferencePending?: boolean;
  onNavigate(routeId: string): void;
  onDismissLegacyPreference?(): void;
  onWorkspaceSelect(workspaceId: string): void | Promise<void>;
  profile: WorkspaceProfile;
  projectContext: ReactNode;
  selectedWorkspace: Workspace;
  selectionRequired: boolean;
  workspaces: readonly Workspace[];
}

export function AppShell({
  accountEmail,
  activeRoute,
  children,
  error,
  inspector,
  inspectorOpen,
  legacyPreferencePending = false,
  onNavigate,
  onDismissLegacyPreference,
  onWorkspaceSelect,
  profile,
  projectContext,
  selectedWorkspace,
  selectionRequired,
  workspaces
}: AppShellProps) {
  const mainRef = useRef<HTMLElement>(null);

  useEffect(() => {
    mainRef.current?.focus();
  }, [activeRoute]);

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <div
        className="adaptive-shell"
        data-experience={profile.experience}
        data-inspector={inspectorOpen ? "open" : "closed"}
        data-scope={profile.workspaceKind}
        data-route={activeRoute}
      >
        {activeRoute !== "settings" ? <NavigationRail
          accountEmail={accountEmail}
          activeRoute={activeRoute}
          legacyPreferencePending={legacyPreferencePending}
          onDismissLegacyPreference={onDismissLegacyPreference}
          onNavigate={onNavigate}
          onWorkspaceSelect={onWorkspaceSelect}
          profile={profile}
          projectContext={projectContext}
          selectedWorkspace={selectedWorkspace}
          selectionRequired={selectionRequired}
          workspaces={workspaces}
        /> : null}
        <header className="adaptive-topbar">
          <div><BrandLockup className="mobile-wordmark" /><strong>{profile.label}</strong></div>
          {error ? <ConnectionBanner error={error} /> : null}
        </header>
        <main className="adaptive-main" id="main-content" ref={mainRef} tabIndex={-1}>
          {error ? <div className="adaptive-main__error"><ConnectionBanner error={error} /></div> : null}
          {children}
        </main>
        {inspectorOpen ? <div className="adaptive-inspector-overlay">{inspector}</div> : null}
        {activeRoute !== "settings" ? <ResponsiveNavigation
          accountEmail={accountEmail}
          activeRoute={activeRoute}
          legacyPreferencePending={legacyPreferencePending}
          onDismissLegacyPreference={onDismissLegacyPreference}
          onNavigate={onNavigate}
          onWorkspaceSelect={onWorkspaceSelect}
          profile={profile}
          selectedWorkspace={selectedWorkspace}
          selectionRequired={selectionRequired}
          workspaces={workspaces}
        /> : null}
      </div>
    </>
  );
}
