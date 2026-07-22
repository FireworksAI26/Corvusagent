import { useEffect, useRef, useState } from "react";

import type { WorkspaceProfile } from "../app/workspaceProfiles";
import type { components } from "../generated/api";
import { WorkspaceIdentityBlock } from "./WorkspaceSwitcher";
import { focusFirstControl, trapDialogFocus } from "./dialogFocus";

type Workspace = components["schemas"]["Workspace"];

interface ResponsiveNavigationProps {
  accountEmail: string;
  activeRoute: string;
  legacyPreferencePending?: boolean;
  onDismissLegacyPreference?(): void;
  onNavigate(routeId: string): void;
  onWorkspaceSelect(workspaceId: string): void | Promise<void>;
  profile: WorkspaceProfile;
  selectedWorkspace: Workspace;
  selectionRequired?: boolean;
  workspaces: readonly Workspace[];
}

const MOBILE_PRIMARY_COUNT = 4;

export function ResponsiveNavigation({
  accountEmail,
  activeRoute,
  legacyPreferencePending = false,
  onDismissLegacyPreference,
  onNavigate,
  onWorkspaceSelect,
  profile,
  selectedWorkspace,
  selectionRequired = false,
  workspaces
}: ResponsiveNavigationProps) {
  const moreRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const primaryRoutes = profile.routes.slice(0, MOBILE_PRIMARY_COUNT);
  const remainingRoutes = profile.routes.slice(MOBILE_PRIMARY_COUNT);

  useEffect(() => {
    if (open) focusFirstControl(dialogRef.current);
  }, [open]);

  function closeMenu() {
    setOpen(false);
    moreRef.current?.focus();
  }

  function navigate(routeId: string) {
    closeMenu();
    onNavigate(routeId);
  }

  return (
    <nav aria-label="Mobile workspace navigation" className="responsive-navigation">
      {primaryRoutes.map((route) => (
        <a
          aria-label={`${route.label} mobile`}
          aria-current={activeRoute === route.id ? "page" : undefined}
          href={`#${route.id}`}
          key={route.id}
          onClick={(event) => {
            event.preventDefault();
            navigate(route.id);
          }}
        >
          {route.label}
        </a>
      ))}
      <button
        aria-expanded={open}
        data-action="mobile-more"
        onClick={() => setOpen(true)}
        ref={moreRef}
        type="button"
      >
        More
      </button>
      {open && (
        <div
          aria-label="More navigation"
          aria-modal="true"
          className="mobile-settings"
          onKeyDown={(event) => {
            if (event.key === "Escape") closeMenu();
            else trapDialogFocus(event, dialogRef.current);
          }}
          ref={dialogRef}
          role="dialog"
        >
          <div className="section-heading">
            <strong>More</strong>
            <button onClick={closeMenu} type="button">Close More menu</button>
          </div>
          {remainingRoutes.length > 0 && (
            <div className="mobile-settings__routes">
              {remainingRoutes.map((route) => (
                <button key={route.id} onClick={() => navigate(route.id)} type="button">{route.label}</button>
              ))}
            </div>
          )}
          <WorkspaceIdentityBlock
            accountEmail={accountEmail}
            experience={profile.experience}
            legacyPreferencePending={legacyPreferencePending}
            onDismissLegacyPreference={onDismissLegacyPreference}
            onNavigateSettings={() => navigate("settings")}
            onWorkspaceSelect={onWorkspaceSelect}
            selectedWorkspace={selectedWorkspace}
            selectionRequired={selectionRequired}
            workspaces={workspaces}
          />
        </div>
      )}
    </nav>
  );
}
