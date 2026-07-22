import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { LocalRepository } from "../api";
import { RepositoriesWorkspace, type RepositoryApi } from "./RepositoriesWorkspace";

const repository: LocalRepository = {
  id: "repository-1",
  tenant_id: "local",
  display_name: "Corvus",
  path: "C:\\work\\corvus",
  remote_slug: "team/corvus",
  default_branch: "main",
  created_at: "2026-07-18T00:00:00Z",
  updated_at: "2026-07-18T00:00:00Z",
  snapshot: {
    branch: "main",
    head_sha: "a".repeat(40),
    clean: true,
    ahead: 1,
    behind: 0,
    health: "healthy",
    refreshed_at: "2026-07-18T00:00:00Z"
  }
};

function apiWith(overrides: Partial<RepositoryApi> = {}): RepositoryApi {
  return {
    listRepositories: vi.fn().mockResolvedValue([]),
    registerRepository: vi.fn().mockResolvedValue(repository),
    refreshRepository: vi.fn().mockResolvedValue(repository),
    removeRepository: vi.fn().mockResolvedValue(undefined),
    ...overrides
  };
}

describe("RepositoriesWorkspace", () => {
  async function openLocalRepositoryForm(user: ReturnType<typeof userEvent.setup>): Promise<void> {
    await user.click(screen.getByRole("button", { name: "Add project" }));
    await user.click(screen.getByRole("button", { name: "Use a local folder" }));
  }

  it("offers one clear add flow for every supported project source", async () => {
    const user = userEvent.setup();
    render(<RepositoriesWorkspace api={apiWith({ createEmptyRepository: vi.fn() })} />);

    await screen.findByText("No projects added yet");
    expect(screen.getByRole("button", { name: "Add project" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Use a local folder" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add project" }));
    expect(screen.getByRole("button", { name: "Use a local folder" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Start from scratch" })).toBeVisible();
  });

  it("acknowledges a routed add-project signal after opening the dialog", async () => {
    const onDialogSignalHandled = vi.fn();
    render(
      <RepositoriesWorkspace
        api={apiWith()}
        onDialogSignalHandled={onDialogSignalHandled}
        openDialogSignal={1}
      />,
    );

    expect(await screen.findByRole("dialog", { name: "Add a project" })).toBeVisible();
    expect(onDialogSignalHandled).toHaveBeenCalledOnce();
  });

  it("traps keyboard focus in the project dialog and closes on Escape", async () => {
    const user = userEvent.setup();
    render(<RepositoriesWorkspace api={apiWith()} />);

    await user.click(await screen.findByRole("button", { name: "Add project" }));
    const dialog = screen.getByRole("dialog", { name: "Add a project" });
    const closeButton = within(dialog).getByRole("button", { name: "Close add project" });
    await waitFor(() => expect(closeButton).toHaveFocus());
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(within(dialog).getByRole("button", { name: "Use a local folder" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Add a project" })).not.toBeInTheDocument();
  });

  it("registers a folder selected by the desktop picker", async () => {
    const api = apiWith();
    const picker = vi.fn().mockResolvedValue("C:\\work\\corvus");
    render(<RepositoriesWorkspace api={api} pickDirectory={picker} />);
    const user = userEvent.setup();

    expect(await screen.findByText("No projects added yet")).toBeVisible();
    await openLocalRepositoryForm(user);
    await user.click(screen.getByRole("button", { name: "Browse" }));
    expect(screen.getByLabelText("Project folder")).toHaveValue("C:\\work\\corvus");
    expect(screen.getByLabelText("Display name")).toHaveValue("corvus");
    await user.click(screen.getByRole("button", { name: "Add local project" }));

    expect(api.registerRepository).toHaveBeenCalledWith("C:\\work\\corvus", "corvus");
    expect(await screen.findByText("team/corvus")).toBeVisible();
  });

  it("keeps cancellation and validation honest", async () => {
    const api = apiWith();
    render(
      <RepositoriesWorkspace
        api={api}
        pickDirectory={vi.fn().mockResolvedValue(null)}
      />
    );
    const user = userEvent.setup();

    await screen.findByText("No projects added yet");
    await openLocalRepositoryForm(user);
    await user.click(screen.getByRole("button", { name: "Browse" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add local project" })).toBeDisabled();
  });

  it("shows real state and refreshes without optimistic status", async () => {
    const refreshing = { ...repository, snapshot: { ...repository.snapshot, clean: false } };
    const api = apiWith({
      listRepositories: vi.fn().mockResolvedValue([repository]),
      refreshRepository: vi.fn().mockResolvedValue(refreshing)
    });
    render(<RepositoriesWorkspace api={api} />);
    const user = userEvent.setup();

    expect(await screen.findByText("Clean")).toBeVisible();
    expect(screen.getByText(/1 ahead/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Refresh Corvus" }));
    await waitFor(() => expect(screen.getByText("Modified")).toBeVisible());
  });

  it("hands a verified healthy repository to Runs", async () => {
    const client = apiWith({ listRepositories: vi.fn().mockResolvedValue([repository]) });
    const onOpenRuns = vi.fn();
    const user = userEvent.setup();
    render(<RepositoriesWorkspace api={client} onOpenRuns={onOpenRuns} />);

    await user.click(await screen.findByRole("button", { name: "Use in Runs" }));
    expect(onOpenRuns).toHaveBeenCalledWith(repository.id);
  });

  it("renders registration failures as errors", async () => {
    const api = apiWith({
      registerRepository: vi.fn().mockRejectedValue(new Error("not_a_git_repository"))
    });
    render(<RepositoriesWorkspace api={api} />);
    const user = userEvent.setup();

    await screen.findByText("No projects added yet");
    await openLocalRepositoryForm(user);
    await user.type(screen.getByLabelText("Project folder"), "C:\\plain");
    await user.type(screen.getByLabelText("Display name"), "Plain");
    await user.click(screen.getByRole("button", { name: "Add local project" }));

    const dialog = screen.getByRole("dialog", { name: "Use a local folder" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/not a git repository/i);
    expect(screen.queryByText("C:\\plain", { selector: "small" })).not.toBeInTheDocument();
  });

  it("requires explicit Corvus GitHub sign-in before listing connected repositories", async () => {
    const authenticateGitHub = vi.fn().mockResolvedValue({
      authenticated: true,
      hostname: "github.com",
      login: "lucas"
    });
    const listGitHubRepositories = vi.fn().mockResolvedValue([
      { slug: "team/corvus", private: false, default_branch: "main" }
    ]);
    const client = apiWith({
      getGitHubAuthStatus: vi.fn().mockResolvedValue({ authenticated: false, hostname: "github.com" }),
      authenticateGitHub,
      listGitHubRepositories,
      connectGitHubRepository: vi.fn().mockResolvedValue(repository)
    });
    const user = userEvent.setup();
    render(<RepositoriesWorkspace api={client} />);

    await screen.findByText("No projects added yet");
    expect(listGitHubRepositories).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Add project" }));
    await user.click(screen.getByRole("button", { name: "Sign in with GitHub" }));

    await waitFor(() => expect(authenticateGitHub).toHaveBeenCalledOnce());
    expect(await screen.findByText("team/corvus")).toBeVisible();
  });

  it("keeps GitHub authentication failures visible inside the project dialog", async () => {
    const client = apiWith({
      getGitHubAuthStatus: vi.fn().mockResolvedValue({ authenticated: false, hostname: "github.com" }),
      authenticateGitHub: vi.fn().mockRejectedValue(new Error("github_authentication_failed")),
      listGitHubRepositories: vi.fn().mockResolvedValue([]),
      connectGitHubRepository: vi.fn().mockResolvedValue(repository),
    });
    const user = userEvent.setup();
    render(<RepositoriesWorkspace api={client} />);

    await user.click(await screen.findByRole("button", { name: "Add project" }));
    await user.click(screen.getByRole("button", { name: "Sign in with GitHub" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/GitHub sign-in did not complete/i);
    expect(screen.getByRole("dialog", { name: "Link a GitHub project" })).toBeVisible();
  });

  it("clones a pasted GitHub URL through the managed project flow", async () => {
    const connectGitHubRepository = vi.fn().mockResolvedValue(repository);
    const client = apiWith({
      getGitHubAuthStatus: vi.fn().mockResolvedValue({ authenticated: true, hostname: "github.com", login: "lucas" }),
      authenticateGitHub: vi.fn(),
      listGitHubRepositories: vi.fn().mockResolvedValue([]),
      connectGitHubRepository
    });
    const user = userEvent.setup();
    render(<RepositoriesWorkspace api={client} />);

    await user.click(await screen.findByRole("button", { name: "Add project" }));
    await user.click(screen.getByRole("button", { name: "Choose from GitHub" }));
    await user.type(screen.getByLabelText("GitHub repository URL"), "https://github.com/team/corvus");
    await user.click(screen.getByRole("button", { name: "Clone project" }));

    await waitFor(() => expect(connectGitHubRepository).toHaveBeenCalledWith("https://github.com/team/corvus"));
  });

  it("recovers from an initial repository load failure", async () => {
    const client = apiWith();
    vi.mocked(client.listRepositories)
      .mockRejectedValueOnce(new Error("request_failed_503"))
      .mockResolvedValueOnce([repository]);
    const user = userEvent.setup();
    render(<RepositoriesWorkspace api={client} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/runtime is temporarily unavailable/i);
    await user.click(screen.getByRole("button", { name: "Retry projects" }));
    expect(await screen.findByText("team/corvus")).toBeVisible();
    expect(client.listRepositories).toHaveBeenCalledTimes(2);
  });

  it("removes only the Corvus registration after inline confirmation", async () => {
    const client = apiWith({ listRepositories: vi.fn().mockResolvedValue([repository]) });
    const user = userEvent.setup();
    render(<RepositoriesWorkspace api={client} />);

    await screen.findByText("team/corvus");
    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(client.removeRepository).not.toHaveBeenCalled();
    expect(screen.getByText("Remove from Corvus?")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(client.removeRepository).toHaveBeenCalledWith(repository.id));
    expect(await screen.findByRole("status")).toHaveTextContent(/files on disk were not deleted/i);
    expect(screen.queryByText("team/corvus")).not.toBeInTheDocument();
  });
});
