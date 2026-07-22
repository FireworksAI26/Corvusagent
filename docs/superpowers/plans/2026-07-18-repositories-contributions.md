# Repositories and Safe Contributions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect real Git repositories and GitHub metadata, execute only in isolated worktrees, and publish reviewed changes as resumable pull requests.

**Architecture:** Add focused Git/GitHub adapters behind a repository workspace service. Persist registration and contribution state in SQLite while keeping canonical host paths server-side and all Git mutations scoped to validated worktrees.

**Tech Stack:** Python subprocess APIs, Git, GitHub CLI JSON output, SQLite, FastAPI, React, Vitest, pytest.

## Global Constraints

- GitHub authentication is owned by `gh`; Corvus stores no token.
- Commands use argument arrays, bounded output, minimal environment, explicit cwd, and timeouts.
- The registered checkout is never modified by an agent run.
- No merge, approval, force-push, remote deletion, or repository administration.
- Cleanup deletes only a revalidated Corvus-owned run directory.

---

### Task 1: Git and GitHub process adapters

**Files:**
- Create: `corvus/mvp/git_process.py`
- Create: `corvus/mvp/github_cli.py`
- Test: `tests/mvp/test_git_process.py`
- Test: `tests/mvp/test_github_cli.py`

**Interfaces:**
- Produces: `GitProcess.run(cwd: Path, args: tuple[str, ...], timeout: float = 30) -> ProcessResult`.
- Produces: `GitHubCli.auth_status()`, `list_repositories()`, `list_pull_requests(repo)`, `pull_request_checks(repo, number)`, and `create_pull_request(...)`.

- [ ] **Step 1: Write failing tests** for argument preservation, output caps, timeout/process-tree cancellation, JSON parsing, malformed JSON, missing authentication, and secret-free errors.

```python
def test_github_cli_requires_json_and_redacts_stderr(fake_runner):
    fake_runner.queue(returncode=1, stdout=b"", stderr=b"token ghp_secret expired")
    with pytest.raises(GitHubCliError) as raised:
        GitHubCli(fake_runner).list_repositories()
    assert "ghp_secret" not in str(raised.value)
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `uv run pytest tests/mvp/test_git_process.py tests/mvp/test_github_cli.py -q`

- [ ] **Step 3: Implement bounded process and typed records** for repository, PR, and check JSON. Never invoke `cmd`, PowerShell, Bash, or `shell=True`.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: add bounded git and github adapters`

### Task 2: Repository registration and refresh

**Files:**
- Create: `corvus/mvp/repository_workspace.py`
- Modify: `corvus/mvp/store.py`
- Modify: `corvus/mvp/api.py`
- Test: `tests/mvp/test_repository_workspace.py`
- Test: `tests/mvp/test_api_repositories.py`

**Interfaces:**
- Produces: `RepositoryWorkspaceService.register_local(path, display_name) -> RepositoryRecord`.
- Produces: `refresh(repository_id) -> RepositorySnapshot` and `remove(repository_id) -> None`.

- [ ] **Step 1: Add failing tests** using temporary Git repositories for canonical registration, non-Git rejection, link/reparse refusal, duplicate registration, refresh, safe display path, ahead/behind, dirty state, and non-destructive removal.

- [ ] **Step 2: Add schema records**

```sql
CREATE TABLE mvp_repositories (
  id TEXT PRIMARY KEY,
  canonical_path TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  remote_slug TEXT,
  default_branch TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE mvp_repository_snapshots (
  repository_id TEXT PRIMARY KEY REFERENCES mvp_repositories(id),
  branch TEXT NOT NULL,
  head_sha TEXT NOT NULL,
  clean INTEGER NOT NULL,
  ahead INTEGER NOT NULL,
  behind INTEGER NOT NULL,
  health TEXT NOT NULL,
  refreshed_at TEXT NOT NULL
);
```

- [ ] **Step 3: Implement service and `/api/local/repositories` resource group** with tenant/session checks and typed conflict/not-found responses.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: register real local repositories`

### Task 3: Tauri folder picker and clone flow

**Files:**
- Modify: `apps/desktop/src-tauri/Cargo.toml`
- Modify: `apps/desktop/src-tauri/src/lib.rs`
- Modify: `apps/desktop/src-tauri/capabilities/default.json`
- Create: `apps/web/src/app/RepositoriesWorkspace.tsx`
- Test: `apps/web/src/app/RepositoriesWorkspace.test.tsx`
- Test: Rust unit tests in `apps/desktop/src-tauri/src/lib.rs`

**Interfaces:**
- Produces Tauri command: `select_repository_directory() -> Result<Option<String>, String>`.
- Consumes local repository API and GitHub repository list.

- [ ] **Step 1: Write UI tests** for Add local, Clone GitHub, Clone URL, cancellation, validation errors, loading, empty states, and refresh.

- [ ] **Step 2: Implement the narrowly scoped folder picker** using Tauri dialog APIs. It returns only the selected directory; React submits it to the authenticated sidecar for validation.

- [ ] **Step 3: Implement clone** into a Corvus-selected parent using `gh repo clone <slug> <exact-target>` or `git clone -- <url> <exact-target>`, refusing an existing non-empty target.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: connect local and github repositories`

### Task 4: Isolated worktree ownership

**Files:**
- Create: `corvus/mvp/worktrees.py`
- Modify: `corvus/mvp/store.py`
- Test: `tests/mvp/test_worktrees.py`
- Test: `tests/security/test_worktree_boundaries.py`

**Interfaces:**
- Produces: `WorktreeManager.create(repository, run_id, base_sha) -> WorktreeLease`.
- Produces: `discard(lease) -> None` after ownership and containment verification.

- [ ] **Step 1: Write failing real-Git tests** proving exact-SHA checkout, original-checkout immutability, duplicate-run refusal, safe discard, active-run protection, and malicious path rejection.

- [ ] **Step 2: Implement the lease model**

```python
@dataclass(frozen=True, slots=True)
class WorktreeLease:
    run_id: UUID
    repository_id: UUID
    root: Path
    base_sha: str
    created_at: datetime
```

Use `git worktree add --detach <root> <base_sha>` and persist the canonical root and digest before provider execution.

- [ ] **Step 3: Implement discard** with `git worktree remove --force <validated-root>` followed by removal of an empty Corvus-owned directory only.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: isolate repository work in managed worktrees`

### Task 5: Diff, evidence, and actual secret scanning

**Files:**
- Create: `corvus/mvp/change_review.py`
- Create: `corvus/mvp/secret_scan.py`
- Test: `tests/mvp/test_change_review.py`
- Test: `tests/security/test_contribution_secret_scan.py`

**Interfaces:**
- Produces: `ChangeReviewService.snapshot(worktree) -> ChangeSet`.
- Produces: `SecretScanner.scan(worktree, paths) -> SecretScanResult` with `not_scanned|passed|warning|blocked`.

- [ ] **Step 1: Add failing tests** for added/modified/deleted/untracked files, binary files, rename detection, patch caps, path selection, known token patterns, entropy warnings, ignored `.git`, and proof that only an executed scan can return `passed`.

- [ ] **Step 2: Implement machine-readable Git diff collection** and bounded patch retrieval. Selected paths must be normalized relative paths contained inside the worktree.

- [ ] **Step 3: Implement scanner result provenance**

```python
class SecretScanResult(MvpModel):
    status: Literal["not_scanned", "passed", "warning", "blocked"]
    scanner_version: str
    scanned_paths: tuple[str, ...]
    findings: tuple[SecretFinding, ...]
    completed_at: datetime | None
    digest: str | None
```

- [ ] **Step 4: Run tests and commit**

Commit: `feat: add reviewable diffs and real secret scans`

### Task 6: Resumable contribution state machine

**Files:**
- Create: `corvus/mvp/contributions.py`
- Modify: `corvus/mvp/store.py`
- Modify: `corvus/mvp/api.py`
- Test: `tests/mvp/test_contributions.py`
- Test: `tests/mvp/test_api_contributions.py`

**Interfaces:**
- Produces: `prepare(run_id, selected_paths, message, draft) -> ContributionRecord`.
- Produces: `publish(run_id, expected_digest) -> ContributionRecord`.

- [ ] **Step 1: Write failing tests** for normalized branch naming, selected-only staging, scan gating, commit creation, preview digest, confirmation mismatch, non-force push, PR creation, and idempotent resume after branch/commit/push/PR partial success.

- [ ] **Step 2: Add contribution schema** recording branch, selected-path digest, commit SHA, remote ref, PR number/URL, state, and last error.

- [ ] **Step 3: Implement prepare** using `git switch -c`, `git add -- <paths>`, and `git commit -m <message>` only inside the managed worktree.

- [ ] **Step 4: Implement publish** using `git push --set-upstream origin <branch>` without force flags and `gh pr create --repo <slug> --head <branch> --base <base> --title <title> --body <body> [--draft]`.

- [ ] **Step 5: Run tests and commit**

Commit: `feat: publish supervised github contributions`

### Task 7: Repository and contribution UI

**Files:**
- Modify: `apps/web/src/api.ts`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/app/RepositoriesWorkspace.tsx`
- Create: `apps/web/src/app/ContributionPanel.tsx`
- Modify: `apps/web/src/styles/product-workspace.css`
- Test: `apps/web/src/app/RepositoriesWorkspace.test.tsx`
- Test: `apps/web/src/app/ContributionPanel.test.tsx`

- [ ] **Step 1: Write UI tests** for repository detail tabs, PR/check display, New Run handoff, file selection, scan states, prepare preview, explicit publish confirmation, resumed partial state, and PR link.

- [ ] **Step 2: Implement API methods and data-backed views** without optimistic claims of Git or GitHub success.

- [ ] **Step 3: Verify focused and full web tests**

Run: `pnpm --dir apps/web test && pnpm --dir apps/web build`

- [ ] **Step 4: Commit**

Commit: `feat: review and publish contributions in app`

### Task 8: Repository subsystem checkpoint

**Files:**
- Modify: `openapi/corvus-mvp.json`
- Modify: `apps/web/src/generated/api.ts`
- Modify: `HACKATHON_STATUS.md`

- [ ] **Step 1: Regenerate API contracts.**
- [ ] **Step 2: Run `uv run pytest tests/mvp tests/security -q`.**
- [ ] **Step 3: Run `pnpm --dir apps/web test && pnpm --dir apps/web build`.**
- [ ] **Step 4: Commit** as `test: verify repositories and contributions`.
