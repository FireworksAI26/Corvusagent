# Durable Runs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing ephemeral local-chat execution into durable repository-aware Runs with recovery, evidence, cancellation, retry, and review states.

**Architecture:** Keep provider adapters as process boundaries, but add a run coordinator and repository-backed state machine above them. Persist events before streaming them and attach code-changing providers only to managed worktrees.

**Tech Stack:** Python asyncio, SQLite, FastAPI SSE, Codex CLI JSON events, React, Vitest, pytest.

## Global Constraints

- Every run pins repository/base SHA/provider/model/effort/safety/skill/schedule inputs.
- Events are append-only, ordered, bounded, and redacted before persistence.
- Retry creates a new linked run.
- Cancellation terminates only the owned provider process group.
- A terminal state is never inferred from a dropped SSE connection.

---

### Task 1: Run domain model and persistence

**Files:**
- Create: `corvus/mvp/run_models.py`
- Create: `corvus/mvp/run_store.py`
- Modify: `corvus/mvp/store.py`
- Test: `tests/mvp/test_run_store.py`

**Interfaces:**
- Produces: `RunStatus`, `RunRecord`, `RunEvent`, `RunEvidence`, and `RunStore`.

- [ ] **Step 1: Write failing tests** for valid transitions, invalid transition rejection, event monotonicity, retry linkage, occurrence uniqueness, restart reads, and tenant/repository scoping.

```python
class RunStatus(StrEnum):
    PREPARING = "preparing"
    RUNNING = "running"
    REVIEW_REQUIRED = "review_required"
    CONTRIBUTION_READY = "contribution_ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    DISCARDED = "discarded"
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/mvp/test_run_store.py -q`

- [ ] **Step 3: Add `mvp_runs`, `mvp_run_events`, and `mvp_run_evidence` tables** with immutable pinned inputs and unique `(run_id, sequence)`.

- [ ] **Step 4: Implement transactional transitions and append operations.**

- [ ] **Step 5: Run tests and commit**

Commit: `feat: persist durable run state`

### Task 2: Repository-aware Codex run coordinator

**Files:**
- Create: `corvus/mvp/run_coordinator.py`
- Modify: `corvus/mvp/local_chat.py`
- Modify: `corvus/infrastructure/agent_runtimes/codex.py`
- Test: `tests/mvp/test_run_coordinator.py`
- Test: `tests/contract/providers/test_codex_adapter.py`

**Interfaces:**
- Consumes: `WorktreeManager`, provider discovery, safety preview, optional Skill bundle.
- Produces: `RunCoordinator.start(request) -> RunRecord`, `cancel(run_id)`, and `recover_interrupted()`.

- [ ] **Step 1: Write failing tests** for preflight failure, exact worktree cwd, prompt envelope, event persistence-before-publication, code-change completion, read-only completion, and process cancellation.

- [ ] **Step 2: Implement request contract**

```python
class StartRunRequest(MvpModel):
    repository_id: str
    task: str = Field(min_length=1, max_length=262_144)
    provider: Literal["codex"]
    model: str | None
    effort: Literal["low", "medium", "high", "xhigh"]
    mode: Literal["chat", "build"]
    safety_digest: str
    skill_version_id: str | None = None
    output_policy: Literal["report_only", "prepare_changes", "prepare_contribution"]
```

- [ ] **Step 3: Start Codex with the worktree as explicit cwd** and preserve the existing protected approval/sandbox options. Inject only the selected Skill package and task context, never every discovered skill.

- [ ] **Step 4: Persist normalized events and derive terminal/review state** only from provider completion plus a refreshed worktree snapshot.

- [ ] **Step 5: Run tests and commit**

Commit: `feat: coordinate repository-aware codex runs`

### Task 3: Recovery, cancellation, retry, and discard

**Files:**
- Modify: `corvus/mvp/run_coordinator.py`
- Modify: `corvus/mvp/api.py`
- Test: `tests/mvp/test_run_recovery.py`
- Test: `tests/mvp/test_api_runs.py`

- [ ] **Step 1: Add failing tests** for restart marking orphaned running processes interrupted, resuming completed evidence, idempotent cancellation, retry as a new run, and discard refusal during active/published states.

- [ ] **Step 2: Implement startup recovery** that checks owned process handles and worktree presence, marks unrecoverable execution `interrupted`, and never claims work completed.

- [ ] **Step 3: Add the local Runs API resource group** including cursor-based SSE, details, changes, evidence, cancel, retry, and discard.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: recover and control durable runs`

### Task 4: Evidence pipeline

**Files:**
- Create: `corvus/mvp/run_evidence.py`
- Modify: `corvus/mvp/run_coordinator.py`
- Test: `tests/mvp/test_run_evidence.py`
- Test: `tests/security/test_run_event_redaction.py`

**Interfaces:**
- Produces: `EvidenceRecorder.record_command`, `record_test`, `record_scan`, and `finalize_receipt`.

- [ ] **Step 1: Write failing tests** for command argument redaction, output caps, exit status, duration, test summaries, safety receipt digest, secret scan attachment, and deterministic evidence digest.

- [ ] **Step 2: Implement evidence records** storing sanitized summaries and digests rather than unrestricted raw terminal streams.

- [ ] **Step 3: Attach change and scan evidence** before moving to `review_required` or `contribution_ready`.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: record auditable run evidence`

### Task 5: Runs list and detail workspace

**Files:**
- Create: `apps/web/src/app/RunsWorkspace.tsx`
- Create: `apps/web/src/app/RunDetail.tsx`
- Modify: `apps/web/src/api.ts`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/styles/product-workspace.css`
- Test: `apps/web/src/app/RunsWorkspace.test.tsx`
- Test: `apps/web/src/app/RunDetail.test.tsx`

- [ ] **Step 1: Write UI tests** for filtering/list metadata, Overview/Changes/Activity/Evidence/Contribution tabs, SSE resume, Cancel, Retry, Discard, PR/check links, and typed recovery errors.

- [ ] **Step 2: Implement list and detail APIs** with real loading/empty/error states and no fabricated progress percentages.

- [ ] **Step 3: Integrate `ContributionPanel`** from the repository plan only when the run reaches a compatible review state.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: add durable run review workspace`

### Task 6: Run checkpoint

**Files:**
- Modify: `openapi/corvus-mvp.json`
- Modify: `apps/web/src/generated/api.ts`
- Modify: `HACKATHON_STATUS.md`

- [ ] **Step 1: Regenerate contracts.**
- [ ] **Step 2: Run `uv run pytest tests/mvp tests/contract tests/security -q`.**
- [ ] **Step 3: Run `pnpm --dir apps/web test && pnpm --dir apps/web build`.**
- [ ] **Step 4: Commit** as `test: verify durable runs`.
