# Schedules, Demo, and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reliable local schedules, background notifications, final demo polish, production verification, signed-source release artifacts, and a working Vercel preview.

**Architecture:** A SQLite scheduler computes timezone-aware occurrences and claims them transactionally before creating ordinary durable Runs. Tauri supplies background/tray/notification integration while the Python sidecar retains scheduling truth.

**Tech Stack:** Python datetime/zoneinfo, SQLite, FastAPI, React, Tauri, GitHub Actions, Vercel.

## Global Constraints

- Local schedules run only while Corvus is running and the computer is awake.
- Occurrences are unique and never overlap for one schedule.
- Dependencies are reverified; no provider/model/skill substitution.
- Code-changing schedule output stops before push or PR creation.
- Release artifacts are built from the committed branch through CI.

---

### Task 1: Recurrence model and immutable schedule revisions

**Files:**
- Create: `corvus/mvp/schedule_models.py`
- Create: `corvus/mvp/schedule_store.py`
- Modify: `corvus/mvp/store.py`
- Test: `tests/mvp/test_schedule_store.py`
- Test: `tests/mvp/test_schedule_recurrence.py`

**Interfaces:**
- Produces: `Recurrence.next_after(moment, timezone) -> datetime`.
- Produces: `ScheduleStore.create`, `revise`, `pause`, `resume`, `archive`, and `claim_due`.

- [ ] **Step 1: Write failing tests** for once/hourly/daily/weekday/weekly recurrence, timezone conversion, DST gaps/folds, revision immutability, and exact next-occurrence preview.

```python
class Recurrence(MvpModel):
    kind: Literal["once", "hourly", "daily", "weekdays", "weekly"]
    local_time: time | None = None
    weekdays: tuple[int, ...] = ()
    once_at: datetime | None = None
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/mvp/test_schedule_store.py tests/mvp/test_schedule_recurrence.py -q`

- [ ] **Step 3: Add schedule, revision, execution, and claim tables** with unique `(schedule_revision_id, scheduled_for)`.

- [ ] **Step 4: Implement recurrence using `zoneinfo.ZoneInfo`** and persist UTC instants plus the IANA timezone and local recurrence definition.

- [ ] **Step 5: Run tests and commit**

Commit: `feat: persist timezone-aware schedules`

### Task 2: Scheduler claims, recovery, and Run creation

**Files:**
- Create: `corvus/mvp/scheduler.py`
- Modify: `corvus/mvp/run_coordinator.py`
- Test: `tests/mvp/test_scheduler.py`

**Interfaces:**
- Produces: `LocalScheduler.tick(now) -> TickResult`.
- Consumes: `RunCoordinator.start()` with schedule revision and occurrence key.

- [ ] **Step 1: Write failing tests** for transactional claim, concurrent ticks, lease expiry, missed-run grace, stale skip, no backlog replay, one queued replacement, no overlap, dependency changes, and restart recovery.

- [ ] **Step 2: Implement preflight** verifying repository, exact provider/model, current safety digest, skill digest, and worktree capacity.

- [ ] **Step 3: Create ordinary Runs** with `report_only`, `prepare_changes`, or `prepare_contribution`; never invoke contribution publish from the scheduler.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: execute reliable local schedules`

### Task 3: Schedule API and workspace

**Files:**
- Modify: `corvus/mvp/api.py`
- Modify: `apps/web/src/api.ts`
- Replace: `apps/web/src/app/RoutinesWorkspace.tsx`
- Create: `apps/web/src/app/ScheduleEditor.tsx`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/styles/product-workspace.css`
- Test: `tests/mvp/test_api_schedules.py`
- Test: `apps/web/src/app/RoutinesWorkspace.test.tsx`
- Test: `apps/web/src/app/ScheduleEditor.test.tsx`

- [ ] **Step 1: Write failing tests** for create/test-run, recurrence preview, list metadata, run now, pause/resume, edit revision, duplicate, archive, validation, and generated Run navigation.

- [ ] **Step 2: Implement the local Schedule endpoints** and remove the current fake immediate-success routine behavior.

- [ ] **Step 3: Build the wizard** for repository, prompt/skill, cadence/timezone, provider/model/effort, capabilities, output policy, and test run.

- [ ] **Step 4: Build data-backed schedule cards and compact calendar preview.**

- [ ] **Step 5: Run tests and commit**

Commit: `feat: add reliable schedule workspace`

### Task 4: Tray, background scheduler, login launch, and notifications

**Files:**
- Modify: `apps/desktop/src-tauri/Cargo.toml`
- Modify: `apps/desktop/src-tauri/src/lib.rs`
- Modify: `apps/desktop/src-tauri/capabilities/default.json`
- Modify: `apps/web/src/app/SettingsPanel.tsx`
- Test: Rust unit tests in `apps/desktop/src-tauri/src/lib.rs`
- Test: `apps/web/src/app/SettingsPanel.test.tsx`

- [ ] **Step 1: Add tests** for close-to-tray only when enabled, Show Corvus, Quit Corvus, launch-at-login preference, notification payload redaction, and orderly scheduler/sidecar shutdown.

- [ ] **Step 2: Add Tauri tray, notification, and autostart plugins** using exact compatible versions and least-privilege capabilities.

- [ ] **Step 3: Add device settings** for Run in background, Launch at login, and native notifications with truthful unsupported/error states.

- [ ] **Step 4: Run source-level tests and CI Cargo tests, then commit**

Commit: `feat: keep schedules available in background`

### Task 5: Demo journey and visual acceptance

**Files:**
- Create: `docs/demo/BUILD_WEEK_DEMO.md`
- Modify: `README.md`
- Modify: `HACKATHON_STATUS.md`
- Modify: relevant React workspace and style files found by failures

- [ ] **Step 1: Write the exact under-three-minute script** with timestamps, prepared repository state, real task, real imported skill, expected run evidence, PR title, and schedule settings.

- [ ] **Step 2: Exercise the journey against a dedicated demo repository** without merging its PR and record actual URLs/identifiers outside committed secrets.

- [ ] **Step 3: Capture and review screenshots** at 1440x1000, 1024x900, and 390x844, plus Settings and import dialog states. Fix clipping, document scrolling, contrast, focus, and disabled-state explanations.

- [ ] **Step 4: Run keyboard and reduced-motion checks.**

- [ ] **Step 5: Commit**

Commit: `docs: prepare build week demo journey`

### Task 6: Full verification and Vercel preview

**Files:**
- Modify only files required by failing checks.

- [ ] **Step 1: Run Python gates**

Run: `uv run ruff check .`

Run: `uv run mypy corvus`

Run: `uv run pytest`

- [ ] **Step 2: Run web gates**

Run: `pnpm --dir apps/web test`

Run: `pnpm --dir apps/web build`

- [ ] **Step 3: Regenerate and verify OpenAPI with no drift.**

- [ ] **Step 4: Run desktop gates locally when allowed and always through GitHub Actions**

Run: `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml`

Run: `cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml`

- [ ] **Step 5: Trigger/inspect Vercel deployment**, fix root/build/output/runtime configuration until the hosted preview loads without attempting local loopback access.

- [ ] **Step 6: Commit only necessary fixes**

Commit: `fix: pass integrated build week verification`

### Task 7: Installers, release, and PR handoff

**Files:**
- Modify: `.github/workflows/desktop-release.yml` only if verification identifies a release defect.
- Modify: `apps/desktop/src-tauri/tauri.release.conf.json` only if packaging metadata is incorrect.
- Modify: `HACKATHON_STATUS.md`

- [ ] **Step 1: Push the fully verified branch to the existing PR #7.**

- [ ] **Step 2: Trigger the desktop release workflow at the exact pushed SHA** and require Windows, macOS, and Linux jobs configured by the workflow to pass.

- [ ] **Step 3: Download and hash the Windows NSIS installer**, install/launch test it, and record unsigned-alpha limitations truthfully.

- [ ] **Step 4: Upload release artifacts to the matching GitHub prerelease**, replacing only assets for the same version after confirming their names and hashes.

- [ ] **Step 5: Check PR #7 reviews, comments, CI, and deployment statuses**; resolve new actionable findings with focused tests and commits.

- [ ] **Step 6: Request final Codex review and leave the PR unmerged.**

- [ ] **Step 7: Commit final status documentation**

Commit: `chore: finalize build week MVP release`
