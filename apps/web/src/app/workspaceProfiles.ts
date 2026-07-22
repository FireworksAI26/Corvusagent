import type { ExperienceMode, WorkspaceKind } from "./preferences";

export interface WorkspaceRoute {
  id: string;
  label: string;
  description: string;
}

export interface WorkspaceProfile {
  experience: ExperienceMode;
  workspaceKind: WorkspaceKind;
  label: string;
  eyebrow: string;
  routes: readonly WorkspaceRoute[];
}

const SETTINGS_ROUTE: WorkspaceRoute = {
  id: "settings",
  label: "Settings",
  description: "Workspace profile and identity settings"
};

const PROFILES: Record<`${ExperienceMode}:${WorkspaceKind}`, WorkspaceProfile> = {
  "everyday:individual": {
    experience: "everyday",
    workspaceKind: "individual",
    label: "Everyday / Individual",
    eyebrow: "Your private workspace",
    routes: [
      { id: "threads", label: "Conversations", description: "Ask Corvus and follow the result" },
      { id: "repositories", label: "Projects", description: "Connect a folder or GitHub repository" },
      { id: "runs", label: "Activity", description: "Follow supervised work and review its evidence" },
      { id: "schedule", label: "Schedule", description: "Create routines and run them now" },
      { id: "skills", label: "Skills", description: "Import trusted reusable instructions" },
      SETTINGS_ROUTE
    ]
  },
  "developer:individual": {
    experience: "developer",
    workspaceKind: "individual",
    label: "Developer / Individual",
    eyebrow: "Local engineering workspace",
    routes: [
      { id: "repositories", label: "Projects", description: "Folders, GitHub repositories, branches, and worktrees" },
      { id: "runs", label: "Runs", description: "Workflows, logs, checks, and budgets" },
      { id: "schedule", label: "Schedule", description: "Authorized routines and manual runs" },
      { id: "skills", label: "Skills", description: "Versioned tools, memory, and routines" },
      { id: "threads", label: "Threads", description: "Start a new agent conversation" },
      SETTINGS_ROUTE
    ]
  },
  "everyday:team": {
    experience: "everyday",
    workspaceKind: "team",
    label: "Everyday / Team",
    eyebrow: "Shared work preview",
    routes: [
      { id: "threads", label: "Conversations", description: "Team-shaped conversations on this device" },
      { id: "repositories", label: "Projects", description: "Connect a folder or GitHub repository" },
      { id: "runs", label: "Activity", description: "Follow supervised work and review its evidence" },
      { id: "schedule", label: "Schedule", description: "Shared routine presentation and run-now controls" },
      { id: "skills", label: "Skills", description: "Import trusted reusable instructions" },
      SETTINGS_ROUTE
    ]
  },
  "developer:team": {
    experience: "developer",
    workspaceKind: "team",
    label: "Developer / Team",
    eyebrow: "Governed engineering preview",
    routes: [
      { id: "repositories", label: "Projects", description: "Folders, GitHub repositories, branches, and worktrees" },
      { id: "runs", label: "Runs", description: "Workflows, logs, checks, and budgets" },
      { id: "schedule", label: "Schedule", description: "Authorized routines and manual runs" },
      { id: "skills", label: "Skills", description: "Versioned tools, memory, and routines" },
      { id: "threads", label: "Threads", description: "Start a new agent conversation" },
      SETTINGS_ROUTE
    ]
  }
};

export function getWorkspaceProfile(
  experience: ExperienceMode,
  workspaceKind: WorkspaceKind
): WorkspaceProfile {
  return PROFILES[`${experience}:${workspaceKind}`];
}

export function getWorkspaceDefaultRoute(profile: WorkspaceProfile): string {
  return profile.routes.some((route) => route.id === "threads") ? "threads" : profile.routes[0].id;
}
