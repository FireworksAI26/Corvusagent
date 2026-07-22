# Corvus OpenAI Build Week MVP Design

**Status:** Approved in conversation on 2026-07-18

**Target:** OpenAI Build Week Developer Tools submission

**Primary runtime:** Locally authenticated OpenAI Codex CLI

**Delivery boundary:** Complete the local Windows MVP, verification, installers, Vercel preview, and the existing pull request. Do not merge the pull request.

**Precedence:** This specification supersedes `2026-07-16-corvus-full-product-platform-design.md` for the Build Week branch wherever the documents conflict. Cloud execution, team collaboration, mandatory OAuth, billing, E2B, and event-driven automation remain roadmap items.

## 1. Product promise

Corvus is a safe local control plane that turns Codex work into reviewable, repeatable GitHub contributions.

The MVP must let a developer:

1. Verify a locally authenticated Codex installation.
2. Connect a real local or GitHub repository.
3. Import reusable agent skills from Codex, Claude Code, Hermes Agent, or a portable Agent Skills package.
4. Run Codex against an isolated Git worktree without modifying the original checkout.
5. Review changes, tests, activity, and safety evidence.
6. Explicitly publish a branch and pull request through the authenticated GitHub CLI.
7. Turn a successful workflow into a reliable local schedule whose code-changing output waits for human review.

Corvus does not merge pull requests, force-push, store GitHub tokens, grant a skill its own permissions, or silently substitute a provider, model, repository, or skill version.

## 2. Demo narrative and success measure

The recorded demo must fit within three minutes and use a real repository, real imported skill, real Codex run, real diff, and real pull request.

The narrative is:

1. Launch Corvus without a terminal and show verified Codex models.
2. Open a GitHub-backed repository and show its Git/PR/CI status.
3. Discover and import an existing Claude or Codex skill.
4. run that workflow in an isolated worktree.
5. review its diff, tests, and evidence.
6. create a `corvus/<task>` branch and pull request after confirmation.
7. schedule the same workflow to prepare future changes for review.

The experience is successful when a judge can understand, without narration, what Corvus is doing, what authority it has, what changed, what was verified, and where human approval is required.

## 3. Architecture and state ownership

```text
Tauri desktop shell
  |-- single-instance + foreground activation
  |-- hidden Python sidecar lifecycle
  |-- native folder picker, tray, notification, and external-link commands
  v
React application
  |-- app/settings shells
  |-- repository, run, skill, schedule, and settings workspaces
  v
Loopback FastAPI sidecar
  |-- authenticated mutation API + SSE
  |-- provider and safety discovery
  |-- repositories/GitHub adapter
  |-- worktree run coordinator
  |-- skill import/quarantine service
  |-- local scheduler
  v
SQLite + Corvus application-data directory
```

The Python sidecar owns authoritative local product state. React does not infer successful discovery, execution, scanning, Git mutations, or scheduling from optimistic UI state. Tauri owns OS integration and process lifecycle but not domain decisions.

SQLite stores records and immutable metadata. File payloads that do not belong in SQLite—worktrees, skill bundles, evidence files, and logs—live under a Corvus-owned application-data root and are referenced by relative identifiers plus SHA-256 digests.

All subprocesses use argument arrays, bounded output, bounded duration, a minimal environment, hidden Windows process flags, and explicit working directories. No feature constructs a shell command from user input.

## 4. Application shell and visual behavior

### 4.1 Viewport and scrolling

The Tauri webview, `html`, `body`, React root, and application shell occupy exactly the available viewport below native window chrome. The page itself must not scroll or rubber-band beyond its content.

Only designated content panes may scroll. Navigation, page header, composer, status areas, and settings navigation remain fixed inside the application shell. Nested scroll containers use `min-height: 0`, `overscroll-behavior: contain`, and appropriate scrollbar gutters. Modal and sheet opening must not change the overall page width.

### 4.2 Color hierarchy

The main center workspace uses the darkest application surface. The app/sidebar and settings/sidebar use a visibly lighter surface. Borders provide structure without making the center brighter than navigation.

### 4.3 App and settings shells

Normal application routes use the Corvus navigation sidebar with Threads, Repositories, Runs, Schedule, Skills, and Settings.

Settings routes replace the entire app sidebar with a settings sidebar. Its first interactive element is **Back to app**, with a left arrow and restored previous-app route. Below it are General, Models, Agent, MCP, Safety, Appearance, and Account destinations. The normal app navigation is not displayed simultaneously.

Every route has truthful loading, empty, disabled, error, and recovery states. Placeholder buttons that cannot perform their labeled operation are prohibited.

## 5. Provider discovery, settings, and safety

### 5.1 Discovery contract

Provider discovery returns one record per provider with:

- stable provider ID and label;
- executable path digest, not the raw path in browser-facing payloads;
- installed and executable status;
- authentication status and method;
- CLI version;
- detected, configured, and curated model options;
- supported thinking levels per model;
- supported modes and MCP capability;
- last successful discovery time;
- current error reason code and recovery action.

Discovery is per-provider. Failure to verify Claude must not hide a valid Codex result. A refresh starts a new bounded discovery attempt and preserves the last known successful catalog for display with a stale badge. Starting a run always requires a fresh ready result.

### 5.2 Codex models

Codex model options are assembled in this order:

1. an effective model explicitly configured by the installed Codex CLI;
2. models returned by a supported Codex discovery surface;
3. the tested curated fallback catalog bundled with Corvus;
4. **Codex default**, which delegates selection to the authenticated account.

Duplicate model IDs are removed without losing order. An unavailable configured model remains visible with an unavailable explanation. The selector is never rendered as an empty enabled control.

Thinking levels come from the provider/model capability record. Codex supports `low`, `medium`, `high`, and `xhigh` where compatible. The UI resets an invalid saved effort to the recommended supported value and explains the change. It must not silently force all users to Medium.

### 5.3 Safety policy

Safety policy construction is deterministic local code with a versioned digest. Provider discovery and safety-policy construction are separate health checks.

Runs fail closed when the policy cannot be constructed or when the submitted digest does not match the current policy. A provider discovery failure is labeled **Provider unavailable**, not **Safety unavailable**. A safety failure is labeled **Safety policy unavailable** only when policy verification itself fails.

The UI provides one-click retry, opens the relevant Settings page, and gives a specific command or action when Codex is missing or signed out.

## 6. Repositories and GitHub

### 6.1 Repository registration

Corvus supports:

- **Add local repository** through a Tauri directory picker;
- **Clone from GitHub** by listing repositories available through `gh repo list` and cloning a selected repository;
- **Clone URL** for an explicitly entered Git URL.

The folder picker returns one canonical absolute directory selected by the user. The API rejects missing directories, non-Git directories, file paths, Corvus internal directories, and link/reparse traversal outside the selected root.

GitHub uses the existing `gh` authentication and credential storage. Corvus calls `gh auth status`; it does not read or persist a GitHub token.

### 6.2 Repository record and dashboard

Each repository record stores:

- ID and display name;
- canonical local path in sidecar-only storage;
- safe display path;
- remote owner/name and default branch when available;
- current branch and HEAD SHA;
- working-tree cleanliness;
- ahead/behind counts;
- last refresh and typed health state.

The Repositories page shows real data for registered repositories. Its detail view includes Overview, Pull requests, Checks, Runs, and Schedules. It provides New Run, Refresh, Open folder, Open on GitHub, and Remove registration actions.

Removing a registration never deletes the repository. A repository with active worktrees or runs cannot be removed until those runs finish or are explicitly discarded.

### 6.3 GitHub read operations

For GitHub-backed repositories Corvus shows:

- open pull requests from `gh pr list`;
- pull-request detail and URL;
- check states from `gh pr checks` or the equivalent JSON surface;
- authentication and access errors as actionable typed states.

Corvus does not scrape human-formatted terminal output when a stable JSON output is available.

## 7. Runs and isolated execution

### 7.1 Run lifecycle

The run state machine is:

```text
preparing -> running -> review_required -> contribution_ready -> publishing
        |       |               |                 |                |
        +------>failed          +->discarded      +->discarded     +->published
        +------>cancelled
        +------>interrupted
```

Read-only runs may transition from `running` directly to `completed`. Retry always creates a new run linked by `retry_of_run_id`; it never rewrites prior evidence.

### 7.2 Run record

Every run pins:

- repository ID, canonical base branch, and base SHA;
- task text;
- provider, model, thinking level, and mode;
- safety-policy version and digest;
- skill version ID and bundle digest when used;
- schedule revision and occurrence key when scheduled;
- output policy and requested capabilities;
- worktree identifier;
- timestamps, status, error reason, and retry relationship.

Run events are append-only and monotonically sequenced. Event payloads are bounded and redacted before persistence. SSE resumes after a sequence cursor and never fabricates a completion event.

### 7.3 Worktrees

A code-changing run creates a Git worktree under a Corvus-owned run directory at the exact recorded base SHA. It never writes to the registered checkout.

Before execution Corvus verifies:

- the repository identity and base commit still exist;
- the Corvus worktree root is not a link or reparse point;
- the selected provider and safety digest are current;
- no conflicting run owns the same run directory;
- available disk and configured size limits are sufficient.

Cancellation terminates the full provider process group. Worktrees remain available for review until the user publishes or explicitly discards them. Discard removes only the validated Corvus-owned run directory and its Git worktree registration.

### 7.4 Run views

The Runs list shows task, repository, state, provider/model, base SHA, diff summary, duration, linked PR, and CI status.

Run detail provides:

- **Overview:** task, configuration, status, timing, and recovery;
- **Changes:** file list, additions/deletions, patch preview, and file selection;
- **Activity:** ordered provider/run events;
- **Evidence:** commands, exit states, tests, safety receipt, and secret scan;
- **Contribution:** branch, commit, push, PR, and checks.

## 8. Safe contribution workflow

Publishing is a multi-step state machine, not a single opaque command:

1. Refresh the diff and repository identity.
2. Require a completed secret scan. `not_scanned` cannot publish.
3. Let the user select changed files; paths must resolve inside the worktree.
4. Create a branch named `corvus/<normalized-task>-<short-run-id>` from the pinned base SHA.
5. Stage only selected files.
6. Create a commit with a user-reviewed message.
7. Present remote, branch, commit SHA, selected files, and intended PR title/body.
8. Require explicit confirmation.
9. Push without force and without changing the default branch.
10. Create a draft or ready pull request using `gh pr create`.
11. Persist the URL and refresh CI checks.

The operation is resumable and idempotent. If a branch, commit, push, or PR already exists for the run, retrying discovers and reuses that state rather than duplicating it.

Corvus never merges, approves, force-pushes, deletes a remote branch, changes repository settings, or bypasses branch protection.

## 9. Skills and cross-agent imports

### 9.1 Corvus skill package

A skill version is an immutable Agent Skills-compatible directory containing:

- required `SKILL.md`;
- optional `scripts/`, `references/`, `assets/`, `templates/`, and `examples/`;
- preserved vendor metadata files;
- a Corvus provenance and validation manifest stored outside the exported package.

Required frontmatter follows the open Agent Skills constraints. `name` is a matching lowercase hyphenated directory name of at most 64 characters. `description` is non-empty, at most 1024 characters, and states what the skill does and when to use it.

Skill status is `draft`, `active`, or `archived`. Editing creates the next version. Activating a version deactivates other active versions of the same scoped skill without deleting them. Runs always pin one immutable version and digest.

### 9.2 Creation and use

Users can:

- create a skill manually;
- save a successful run as a skill draft;
- import from a discovered source;
- preview the exact normalized package;
- compare versions and reactivate an older version;
- run an active version now;
- schedule an active version;
- export it to a folder, ZIP, supported agent location, or repository contribution.

### 9.3 Discovery sources

The MVP discovers:

| Ecosystem | Personal locations | Repository locations |
|---|---|---|
| Codex | `$HOME/.agents/skills`, `$CODEX_HOME/skills`, legacy `$HOME/.codex/skills` | `.agents/skills` from CWD through repository root |
| Claude Code | `$HOME/.claude/skills`, `$HOME/.claude/commands/*.md` | `.claude/skills`, legacy `.claude/commands/*.md` |
| Hermes Agent | `$HOME/.hermes/skills`, configured `skills.external_dirs` | paths declared by Hermes configuration |
| GitHub Copilot/generic | `$HOME/.copilot/skills`, `$HOME/.agents/skills`, selected folder/ZIP/`SKILL.md` | `.github/skills`, `.claude/skills`, `.agents/skills` |

On Windows the UI offers an explicit **Scan WSL environments** action. Corvus never scans every distribution or arbitrary home directory without that action.

### 9.4 Normalization

Common Agent Skills fields and referenced files copy directly after validation.

Codex `agents/openai.yaml` display metadata, invocation policy, and tool dependencies map into Corvus presentation and prerequisite fields while the original file is preserved.

Claude Code imports:

- map `$ARGUMENTS` to a Corvus task input;
- convert `${CLAUDE_SKILL_DIR}` references to portable relative references;
- turn legacy command Markdown files into draft skill packages;
- preserve invocation controls;
- flag dynamic `!command` interpolation, `context: fork`, custom agents, and unsupported substitutions for review;
- convert `allowed-tools` to unapproved requested capabilities.

Hermes imports preserve version, platforms, author, tags, categories, compatibility, and `metadata.hermes`. Required/fallback toolsets become prerequisites. Config entries become prompted inputs. Hermes bundles import as Corvus Skill Sets that pin member versions.

No imported permission transfers into Corvus execution authority.

### 9.5 Import pipeline

Import is read-only against its source and consists of:

1. discover candidate package without executing it;
2. resolve canonical paths and refuse escapes;
3. copy allowed content into a new quarantine directory;
4. validate frontmatter, names, paths, sizes, file types, and references;
5. scan instructions and executable content for secrets, destructive behavior, exfiltration, prompt injection, and supply-chain indicators;
6. compute package and per-file digests;
7. classify as `ready`, `needs_review`, or `blocked`;
8. show source-to-normalized differences;
9. import an approved candidate as a draft.

Scripts do not execute during discovery, import, preview, or activation.

Duplicate handling compares canonical source, name, and content hash. Exact duplicates can link to the existing version. Same-name variants offer skip, safe rename, or import as the next version. Corvus never performs an automatic textual merge.

## 10. Schedules

### 10.1 Schedule contract

The MVP supports one-time, hourly, daily, selected-weekday, weekly, and manual run-now triggers. Arbitrary cron and repository event triggers are outside this delivery.

A schedule revision pins:

- repository ID;
- task template and input values;
- skill version and digest;
- provider, model, thinking level, and mode;
- timezone and recurrence;
- output policy;
- requested capabilities;
- missed-run grace period and notification policy.

Schedule status is `enabled`, `paused`, `running`, `needs_attention`, or `archived`. Editing creates a new immutable revision.

### 10.2 Output policies

- `report_only`: inspect and return evidence without code changes;
- `prepare_changes`: create a review-ready worktree diff;
- `prepare_contribution`: create a local branch and commit but wait for the user before push and PR creation.

No schedule silently pushes, creates a pull request, merges, deletes, approves, or performs repository administration.

### 10.3 Reliability

Each due occurrence has a unique `(schedule_revision_id, scheduled_for)` key. A database claim with an expiry prevents duplicate execution.

On startup or wake:

- run one missed occurrence if it remains inside the configured grace window;
- skip stale occurrences and record the reason;
- never replay an unbounded backlog;
- never overlap two occurrences of the same schedule;
- queue at most one replacement occurrence while an earlier run is active.

Before each execution Corvus re-verifies repository availability, provider readiness, exact model, safety policy, skill digest, and worktree capacity. Failure sets `needs_attention`; it does not substitute another dependency.

### 10.4 UI and desktop behavior

The Schedule page shows list and compact calendar previews, next/last occurrence, last result, repository, provider/model, pinned skill, and output policy. Actions are Run now, Pause, Resume, Edit, Duplicate, View Runs, and Archive.

The desktop can start at login and remain in the tray while enabled schedules exist. Native notifications announce reports, review-required changes, and actionable failures. The UI clearly states that local schedules require Corvus running and the computer awake.

## 11. Desktop process behavior

The packaged Windows application uses the GUI subsystem and launches its Python sidecar with `CREATE_NO_WINDOW`. No console window appears during normal startup, discovery, run execution, Git operations, or scheduling.

Only one Corvus application instance may run per user session. A second launch sends an activation message to the first process, restores it if minimized, focuses the existing main window, forwards any supported deep-link payload, and exits.

Closing the window exits when no background schedule behavior is enabled. When tray/background operation is enabled, closing hides the window and keeps the scheduler running. **Quit Corvus** from the tray performs an orderly sidecar and scheduler shutdown.

Sidecar startup uses a bounded readiness wait and communicates through a random loopback port plus one-time pairing secret. Shutdown terminates the owned process tree. Corvus never kills unrelated Python, Codex, Git, or GitHub CLI processes.

## 12. API surface

The existing authenticated API remains the base. New local-MVP resources use `/api/local/*` and preserve the existing origin, session, mutation-token, request-size, and loopback controls.

Required resource groups are:

```text
GET/POST       /api/local/repositories
GET/DELETE     /api/local/repositories/{id}
POST           /api/local/repositories/{id}/refresh
GET            /api/local/repositories/{id}/pull-requests
GET            /api/local/repositories/{id}/checks

GET/POST       /api/local/runs
GET            /api/local/runs/{id}
GET            /api/local/runs/{id}/events
POST           /api/local/runs/{id}/cancel
POST           /api/local/runs/{id}/retry
POST           /api/local/runs/{id}/discard
GET            /api/local/runs/{id}/changes
GET            /api/local/runs/{id}/evidence
POST           /api/local/runs/{id}/contribution/prepare
POST           /api/local/runs/{id}/contribution/publish
GET            /api/local/runs/{id}/contribution/checks

GET/POST       /api/local/skills
GET            /api/local/skills/{id}
POST           /api/local/skills/{id}/activate
POST           /api/local/skills/{id}/archive
GET            /api/local/skill-sources
POST           /api/local/skill-sources/refresh
POST           /api/local/skill-imports/preview
POST           /api/local/skill-imports/{candidate_id}/commit
POST           /api/local/skills/{id}/export

GET/POST       /api/local/schedules
GET/PUT/DELETE /api/local/schedules/{id}
POST           /api/local/schedules/{id}/run
POST           /api/local/schedules/{id}/pause
POST           /api/local/schedules/{id}/resume
GET            /api/local/schedules/{id}/executions
```

OpenAPI and generated TypeScript types are refreshed from the implementation. API models use stable reason codes and never expose raw credential values, unrestricted subprocess output, or internal absolute paths that are unnecessary for the UI.

## 13. Security requirements

- Local mutations require the paired authenticated session, mutation token, and trusted origin.
- Trusted development origins include explicit loopback hosts on ports `3000`, `4173`, and `5173`; wildcard origins remain prohibited.
- Every path crosses a canonical-root containment check after link/reparse inspection.
- Git and GitHub commands use argument arrays and machine-readable output.
- GitHub authentication stays inside `gh`.
- Secret scans report `not_scanned`, `passed`, `warning`, or `blocked`; only an actual completed scan may report `passed`.
- Skills are data until explicitly selected for a run. Import and activation grant no tool or network authority.
- Logs, events, errors, and evidence are redacted before persistence and bounded in size.
- Destructive cleanup targets only a resolved Corvus-owned run or quarantine directory and verifies that target immediately before removal.
- A changed provider executable invalidates its discovery proof until discovery is retried.
- Schedule execution has no broader authority than an equivalent manually configured run.

## 14. Verification and acceptance

### 14.1 Automated verification

Every subsystem adds focused Python and React tests. The final branch must pass:

- `uv run ruff check .`
- `uv run mypy corvus`
- `uv run pytest`
- `pnpm --dir apps/web test`
- `pnpm --dir apps/web build`
- `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml`
- `cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml`
- OpenAPI regeneration with no uncommitted generated drift
- security and supply-chain workflows

Time, subprocess, GitHub, and filesystem dependencies require deterministic fakes in unit tests. Integration tests may use temporary real Git repositories but must not mutate the developer checkout or contact an external GitHub repository.

### 14.2 Product acceptance

A clean Windows installation must demonstrate:

1. no visible terminal and no duplicate application instance;
2. bounded viewport behavior at 1440x1000, 1024x900, and 390x844;
3. settings sidebar replacement and Back to app;
4. real Codex verification, non-empty model list, and supported thinking levels;
5. correct separation of provider and safety failures;
6. local repository registration and GitHub repository cloning;
7. isolated real run with durable restart-safe evidence;
8. reviewed real branch, commit, push, and pull request;
9. import of one Codex, Claude Code, Hermes, and generic fixture package;
10. one schedule surviving application restart without duplicate execution;
11. native notification or an explicitly recorded unsupported-platform fallback;
12. successful Windows installer launch and Vercel hosted-preview build.

Accessibility requires keyboard-complete navigation, visible focus, named controls, appropriate live regions, meaningful disabled explanations, reduced-motion support, and usable target sizes.

## 15. Delivery order

Implementation proceeds in five independently testable plans:

1. **Foundation:** provider/safety reliability, viewport/settings shell, hidden single-instance desktop process.
2. **Repositories and contributions:** repository registration, GitHub status, worktrees, diff review, branch/commit/push/PR.
3. **Runs:** durable lifecycle, events, evidence, recovery, and run workspaces.
4. **Skills:** package versions, discovery adapters, quarantine/import, activation, export, and UI.
5. **Schedules and release:** recurrence, claims/recovery, background notifications, demo polish, documentation, installers, Vercel, and integrated verification.

Each plan uses test-driven changes and focused commits. Existing unrelated `artifacts/` and `logs/` working-tree content is preserved and never committed accidentally.

## 16. Explicit post-hackathon scope

The following are excluded from this build:

- GitHub OAuth or GitHub token storage;
- cloud execution while the computer is off;
- GitHub webhook/event schedules;
- automatic pull-request merging or approval;
- force-push and repository administration;
- public skill marketplace browsing;
- continuous two-way agent-library synchronization;
- team accounts and shared schedule administration;
- mobile remote control;
- arbitrary cron;
- multi-agent orchestration dashboards;
- organization policy administration;
- full Claude, Hermes, Gemini, or Cursor execution providers.

These exclusions do not permit placeholder controls. Deferred capabilities are omitted or clearly labeled informational roadmap items.
