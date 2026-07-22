import { invoke, isTauri } from "@tauri-apps/api/core";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import type { GitHubAuthStatus, GitHubRepositorySummary, LocalRepository } from "../api";
import { focusFirstControl, trapDialogFocus } from "../components/dialogFocus";
import { featureErrorMessage } from "./featureFeedback";

export interface RepositoryApi {
  listRepositories(): Promise<LocalRepository[]>;
  registerRepository(path: string, displayName: string): Promise<LocalRepository>;
  refreshRepository(repositoryId: string): Promise<LocalRepository>;
  removeRepository(repositoryId: string): Promise<void>;
  getGitHubAuthStatus?(): Promise<GitHubAuthStatus>;
  authenticateGitHub?(): Promise<GitHubAuthStatus>;
  listGitHubRepositories?(): Promise<GitHubRepositorySummary[]>;
  connectGitHubRepository?(slug: string): Promise<LocalRepository>;
  createEmptyRepository?(name: string): Promise<LocalRepository>;
}

interface RepositoriesWorkspaceProps {
  api: RepositoryApi;
  onDialogSignalHandled?(): void;
  onOpenRuns?(repositoryId: string): void;
  openDialogSignal?: number;
  pickDirectory?: () => Promise<string | null>;
}

async function pickDesktopDirectory(): Promise<string | null> {
  if (!isTauri()) return null;
  return invoke<string | null>("select_repository_directory");
}

function directoryName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  return trimmed.split(/[\\/]/).at(-1) ?? "Repository";
}

function ProjectSourceIcon({ name }: { name: "blank" | "folder" | "github" }) {
  if (name === "github") {
    return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 .7A11.5 11.5 0 0 0 8.4 23c.6.1.8-.3.8-.6v-2.2c-3.4.7-4.1-1.4-4.1-1.4-.6-1.4-1.4-1.8-1.4-1.8-1.1-.8.1-.8.1-.8 1.3.1 2 1.3 2 1.3 1.1 2 3 1.4 3.7 1 .1-.8.4-1.4.8-1.7-2.7-.3-5.5-1.3-5.5-5.7 0-1.3.5-2.3 1.2-3.1-.1-.3-.5-1.6.1-3.1 0 0 1-.3 3.2 1.2a11 11 0 0 1 5.8 0c2.2-1.5 3.2-1.2 3.2-1.2.6 1.5.2 2.8.1 3.1.8.8 1.2 1.8 1.2 3.1 0 4.4-2.8 5.4-5.5 5.7.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A11.5 11.5 0 0 0 12 .7Z" /></svg>;
  }
  if (name === "folder") {
    return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M3.5 6.5h6l2 2h9v9.5a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18V6.5Z" /></svg>;
  }
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>;
}

export function RepositoriesWorkspace({
  api,
  onDialogSignalHandled,
  onOpenRuns,
  openDialogSignal = 0,
  pickDirectory = pickDesktopDirectory
}: RepositoriesWorkspaceProps) {
  const [repositories, setRepositories] = useState<LocalRepository[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [path, setPath] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [githubError, setGithubError] = useState("");
  const [githubStatus, setGitHubStatus] = useState<GitHubAuthStatus | null>(null);
  const [githubRepositories, setGitHubRepositories] = useState<GitHubRepositorySummary[]>([]);
  const [githubInput, setGitHubInput] = useState("");
  const [githubOpen, setGitHubOpen] = useState(false);
  const projectDialogRef = useRef<HTMLElement>(null);
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null);
  const githubAvailable = api.getGitHubAuthStatus !== undefined
    && api.authenticateGitHub !== undefined
    && api.listGitHubRepositories !== undefined
    && api.connectGitHubRepository !== undefined;

  const loadRepositories = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadFailed(false);
    setError("");
    try {
      setRepositories(await api.listRepositories());
    } catch (reason) {
      setLoadFailed(true);
      setError(featureErrorMessage(reason, "repository"));
    } finally {
      setLoading(false);
    }
  }, [api]);

  const loadGitHub = useCallback(async (): Promise<void> => {
    if (api.getGitHubAuthStatus === undefined) return;
    setGithubError("");
    try {
      const status = await api.getGitHubAuthStatus();
      setGitHubStatus(status);
      if (status.authenticated && api.listGitHubRepositories !== undefined) {
        setGitHubRepositories(await api.listGitHubRepositories());
      }
    } catch (reason) {
      setGitHubStatus(null);
      setGithubError(featureErrorMessage(reason, "github"));
    }
  }, [api]);

  useEffect(() => { void loadRepositories(); }, [loadRepositories]);
  useEffect(() => { void loadGitHub(); }, [loadGitHub]);
  useEffect(() => {
    if (openDialogSignal <= 0) return;
    setError("");
    setAddOpen(true);
    onDialogSignalHandled?.();
  }, [onDialogSignalHandled, openDialogSignal]);
  const dialogOpen = addOpen || creating || newProjectOpen || githubOpen;
  useEffect(() => {
    if (dialogOpen) focusFirstControl(projectDialogRef.current);
  }, [dialogOpen]);

  function closeProjectDialog(): void {
    setAddOpen(false);
    setCreating(false);
    setGitHubOpen(false);
    setNewProjectOpen(false);
    setGitHubInput("");
    setGithubError("");
    setError("");
  }

  async function browse(): Promise<void> {
    setError("");
    try {
      const selected = await pickDirectory();
      if (selected === null) return;
      setPath(selected);
      if (displayName.trim() === "") setDisplayName(directoryName(selected));
    } catch (reason) {
      setError(featureErrorMessage(reason, "repository"));
    }
  }

  async function connect(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (path.trim() === "" || displayName.trim() === "") return;
    setBusyId("new");
    setError("");
    setNotice("");
    try {
      const registered = await api.registerRepository(path.trim(), displayName.trim());
      setRepositories((current) => [
        registered,
        ...current.filter((item) => item.id !== registered.id)
      ]);
      setPath("");
      setDisplayName("");
      setCreating(false);
      setNotice(`${registered.display_name} is connected and ready for repository checks.`);
    } catch (reason) {
      setError(featureErrorMessage(reason, "repository"));
    } finally {
      setBusyId(null);
    }
  }

  async function refresh(repository: LocalRepository): Promise<void> {
    setBusyId(repository.id);
    setError("");
    setNotice("");
    try {
      const refreshed = await api.refreshRepository(repository.id);
      setRepositories((current) =>
        current.map((item) => item.id === refreshed.id ? refreshed : item)
      );
      setNotice(`${refreshed.display_name} was refreshed. Git reports ${refreshed.snapshot.health}.`);
    } catch (reason) {
      setError(featureErrorMessage(reason, "repository"));
    } finally {
      setBusyId(null);
    }
  }

  async function authenticateGitHub(): Promise<void> {
    if (api.authenticateGitHub === undefined) return;
    setBusyId("github-auth");
    setError("");
    setGithubError("");
    setNotice("");
    try {
      const status = await api.authenticateGitHub();
      setGitHubStatus(status);
      if (status.authenticated && api.listGitHubRepositories !== undefined) {
        setGitHubRepositories(await api.listGitHubRepositories());
        setGitHubOpen(true);
      }
      if (!status.authenticated) setGithubError("GitHub sign-in did not complete. Try again when you are ready.");
    } catch (reason) {
      setGithubError(featureErrorMessage(reason, "github"));
    } finally {
      setBusyId(null);
    }
  }

  async function connectGitHub(source: string): Promise<void> {
    if (api.connectGitHubRepository === undefined) return;
    const normalizedSource = source.trim();
    if (normalizedSource === "") return;
    setBusyId(normalizedSource);
    setError("");
    setNotice("");
    try {
      const connected = await api.connectGitHubRepository(normalizedSource);
      setRepositories((current) => [connected, ...current.filter((item) => item.id !== connected.id)]);
      closeProjectDialog();
      setNotice(`${connected.display_name} was cloned into the managed project area.`);
    } catch (reason) {
      setError(featureErrorMessage(reason, "github"));
    } finally {
      setBusyId(null);
    }
  }

  async function createEmptyProject(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (api.createEmptyRepository === undefined || newProjectName.trim() === "") return;
    setBusyId("empty-project");
    setError("");
    setNotice("");
    try {
      const created = await api.createEmptyRepository(newProjectName.trim());
      setRepositories((current) => [created, ...current]);
      setNewProjectName("");
      closeProjectDialog();
      setNotice(`${created.display_name} was created in the Corvus projects directory.`);
    } catch (reason) {
      setError(featureErrorMessage(reason, "repository"));
    } finally {
      setBusyId(null);
    }
  }

  async function remove(repository: LocalRepository): Promise<void> {
    setBusyId(repository.id);
    setError("");
    setNotice("");
    try {
      await api.removeRepository(repository.id);
      setRepositories((current) => current.filter((item) => item.id !== repository.id));
      setConfirmRemoveId(null);
      setNotice(`${repository.display_name} was removed from Corvus. Files on disk were not deleted.`);
    } catch (reason) {
      setError(featureErrorMessage(reason, "repository"));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section aria-labelledby="repositories-title" className="repositories-workspace">
      <header className="resource-heading">
        <div>
          <p className="eyebrow">This computer</p>
          <h1 id="repositories-title">Projects</h1>
          <p>Choose the folder Corvus should work from. Every Build run still uses an isolated managed worktree.</p>
        </div>
        <button aria-haspopup="dialog" className="button button--primary button--with-icon" disabled={busyId !== null} onClick={() => { setError(""); setAddOpen(true); }} type="button"><ProjectSourceIcon name="blank" />Add project</button>
      </header>

      {dialogOpen ? <div className="project-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && busyId === null) closeProjectDialog(); }}>
        <section aria-labelledby="add-project-title" aria-modal="true" className="project-dialog" onKeyDown={(event) => { if (event.key === "Escape" && busyId === null) { event.preventDefault(); closeProjectDialog(); } else trapDialogFocus(event, projectDialogRef.current); }} ref={projectDialogRef} role="dialog">
          <header className="project-dialog__header"><div><p className="eyebrow">Project workspace</p><h2 id="add-project-title">{creating ? "Use a local folder" : newProjectOpen ? "Start from scratch" : githubOpen ? "Link a GitHub project" : "Add a project"}</h2><p>{creating ? "Choose an existing Git checkout. Corvus will use that folder as the source for protected runs." : newProjectOpen ? "Corvus creates a named folder inside corvus-agent-projects and initializes Git for you." : githubOpen ? "Paste a repository URL or select one after signing in." : "Choose where this project should come from."}</p></div><button aria-label="Close add project" className="icon-button" disabled={busyId !== null} onClick={closeProjectDialog} type="button">×</button></header>
          {error ? <p className="inline-error" role="alert">{error}</p> : null}
          {addOpen ? <div aria-label="Project source" className="project-source-options">
            <button aria-label="Start from scratch" className="project-source-card" disabled={api.createEmptyRepository === undefined} onClick={() => { setAddOpen(false); setNewProjectOpen(true); }} type="button"><ProjectSourceIcon name="blank" /><span><strong>Start from scratch</strong><small>Create a blank Git project in the managed Corvus folder.</small></span><b aria-hidden="true">→</b></button>
            <button aria-label="Use a local folder" className="project-source-card" onClick={() => { setAddOpen(false); setCreating(true); }} type="button"><ProjectSourceIcon name="folder" /><span><strong>Use a local folder</strong><small>Choose an existing project already on this computer.</small></span><b aria-hidden="true">→</b></button>
            <button aria-label={githubStatus?.authenticated ? "Choose from GitHub" : "Sign in with GitHub"} className="project-source-card project-source-card--github" disabled={!githubAvailable} onClick={() => { setAddOpen(false); setGitHubOpen(true); if (!githubStatus?.authenticated) void authenticateGitHub(); }} type="button"><ProjectSourceIcon name="github" /><span><strong>{githubStatus?.authenticated ? "Choose from GitHub" : "Sign in with GitHub"}</strong><small>{githubAvailable ? "Open GitHub sign-in, paste a URL, or choose a connected repository." : "Available in the installed Corvus desktop app."}</small></span><b aria-hidden="true">→</b></button>
          </div> : null}
          {newProjectOpen ? <form className="repository-new-project" onSubmit={(event) => void createEmptyProject(event)}><div><label htmlFor="new-project-name">Project name</label><input autoFocus id="new-project-name" onChange={(event) => setNewProjectName(event.target.value)} placeholder="My project" value={newProjectName} /></div><div className="row-actions"><button className="button" onClick={() => { setNewProjectOpen(false); setAddOpen(true); }} type="button">Back</button><button className="button button--primary" disabled={busyId !== null || newProjectName.trim() === ""} type="submit">{busyId === "empty-project" ? "Creating…" : "Create project"}</button></div></form> : null}
          {githubOpen ? <section className="github-repository-picker"><form className="github-url-form" onSubmit={(event) => { event.preventDefault(); void connectGitHub(githubInput); }}><label htmlFor="github-project-url">GitHub repository URL</label><div><input autoFocus id="github-project-url" onChange={(event) => setGitHubInput(event.target.value)} placeholder="https://github.com/owner/project" type="url" value={githubInput} /><button className="button button--primary" disabled={busyId !== null || githubInput.trim() === ""} type="submit">{busyId === githubInput.trim() ? "Cloning…" : "Clone project"}</button></div></form>{githubError ? <p className="inline-error" role="alert">{githubError} <button className="text-button" onClick={() => void authenticateGitHub()} type="button">Try sign-in again</button></p> : null}{githubStatus?.authenticated ? <><div className="project-dialog__divider"><span>or choose from connected GitHub</span></div><div>{githubRepositories.length === 0 ? <div className="resource-empty"><strong>No repositories returned</strong><span>Paste a GitHub URL above, or refresh after granting access.</span></div> : githubRepositories.map((repository) => <article key={repository.slug}><div><strong>{repository.slug}</strong><span>{repository.private ? "Private" : "Public"} · {repository.default_branch ?? "Default branch unavailable"}</span></div><button className="button button--primary" disabled={busyId !== null} onClick={() => void connectGitHub(repository.slug)} type="button">{busyId === repository.slug ? "Cloning…" : "Use project"}</button></article>)}</div></> : <button className="project-source-card project-source-card--github" disabled={busyId !== null} onClick={() => void authenticateGitHub()} type="button"><ProjectSourceIcon name="github" /><span><strong>Sign in with GitHub</strong><small>Your browser will open. Corvus will only list repositories after you complete sign-in.</small></span><b aria-hidden="true">→</b></button>}</section> : null}
          {creating ? <form className="repository-connect" onSubmit={(event) => void connect(event)}>
          <div className="repository-connect__path">
            <label htmlFor="repository-path">Project folder</label>
            <div>
              <input
                autoFocus
                id="repository-path"
                onChange={(event) => setPath(event.target.value)}
                placeholder="C:\\work\\project"
                value={path}
              />
              <button className="button" onClick={() => void browse()} type="button">Browse</button>
            </div>
          </div>
          <div>
            <label htmlFor="repository-display-name">Display name</label>
            <input
              id="repository-display-name"
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="Project name"
              value={displayName}
            />
          </div>
          <div className="row-actions">
            <button className="button" onClick={() => { setCreating(false); setAddOpen(true); }} type="button">Back</button>
            <button
              aria-label="Add local project"
              className="button button--primary"
              disabled={busyId !== null || path.trim() === "" || displayName.trim() === ""}
              type="submit"
            >
              {busyId === "new" ? "Adding…" : "Add project"}
            </button>
          </div>
          </form> : null}
        </section>
      </div> : null}

      {notice ? <p className="inline-success" role="status">{notice}</p> : null}
      {error && !dialogOpen ? <p className="inline-error" role="alert">{error} {loadFailed && !loading ? <button className="text-button" onClick={() => void loadRepositories()} type="button">Retry projects</button> : null}</p> : null}
      {loading ? <div className="resource-empty"><strong>Loading projects…</strong></div> : null}
      {!loading && repositories.length === 0 ? (
        <div className="resource-empty">
          <strong>No projects added yet</strong>
          <span>Start from scratch, choose a local folder, or connect GitHub.</span>
        </div>
      ) : null}
      <div aria-label="Connected projects" className="repository-grid">
        {repositories.map((repository) => (
          <article className="repository-card" key={repository.id}>
            <header>
              <div>
                <h2>{repository.display_name}</h2>
                <span>{repository.remote_slug ?? "Local only"}</span>
              </div>
              <span className="repository-health" data-health={repository.snapshot.health}>
                {repository.snapshot.health}
              </span>
            </header>
            <small title={repository.path}>{repository.path}</small>
            <dl>
              <div><dt>Branch</dt><dd>{repository.snapshot.branch || "Detached"}</dd></div>
              <div><dt>Working tree</dt><dd>{repository.snapshot.clean ? "Clean" : "Modified"}</dd></div>
              <div><dt>Sync</dt><dd>{repository.snapshot.ahead} ahead · {repository.snapshot.behind} behind</dd></div>
              <div><dt>HEAD</dt><dd>{repository.snapshot.head_sha.slice(0, 8)}</dd></div>
            </dl>
            <footer>
              <button
                className="button"
                disabled={busyId !== null}
                onClick={() => void refresh(repository)}
                type="button"
              >
                {busyId === repository.id ? "Refreshing…" : `Refresh ${repository.display_name}`}
              </button>
              <button className="button button--primary" disabled={busyId !== null || repository.snapshot.health !== "healthy" || onOpenRuns === undefined} onClick={() => onOpenRuns?.(repository.id)} title={repository.snapshot.health !== "healthy" ? "Refresh this repository until Git reports it healthy" : undefined} type="button">
                Use in Runs
              </button>
              {confirmRemoveId === repository.id ? <span className="repository-remove-confirm"><span>Remove from Corvus?</span><button className="button" disabled={busyId !== null} onClick={() => setConfirmRemoveId(null)} type="button">Keep</button><button className="button" disabled={busyId !== null} onClick={() => void remove(repository)} type="button">Remove</button></span> : <button className="button button--quiet" disabled={busyId !== null} onClick={() => setConfirmRemoveId(repository.id)} type="button">Remove</button>}
            </footer>
          </article>
        ))}
      </div>
    </section>
  );
}
