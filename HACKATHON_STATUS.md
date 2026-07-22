# Corvus M2-M11 Hackathon MVP Status

This is a hackathon MVP implementation record, not formal M2-M11 certification.

## OpenAI Build Week developer-tools MVP

The current PR #7 branch extends the preserved M2-M11 foundation into a focused local developer workflow:

- Real repository registration, refresh, GitHub pull-request/check visibility, and safe clone/open actions.
- Durable Codex runs in managed Git worktrees with pinned inputs, append-only events, diff/evidence review, cancellation, retry, recovery, and discard.
- A contribution state machine for selected-file staging, secret-scan gating, branch/commit preview, explicit confirmation, non-force push, and draft/ready pull-request creation. Merge is intentionally absent.
- Quarantined, digest-bound skill discovery and import from Codex, Claude Code, Hermes, Copilot, generic Agent Skills, and repository-local locations. Imported tool declarations are unapproved requests, never transferred permissions.
- Timezone-aware one-time/hourly/daily/weekday/weekly schedules with immutable revisions, transactional occurrence claims, missed-run grace, dependency revalidation, and code-changing output capped at review.
- Desktop tray/background operation, launch-at-login controls, redacted native notifications, hidden Windows sidecar processes, and single-instance foreground activation.

The exact Devpost journey and visual acceptance checklist are documented in [docs/demo/BUILD_WEEK_DEMO.md](docs/demo/BUILD_WEEK_DEMO.md). Integrated verification, fresh installers, the Vercel preview, and final PR review remain release gates; this section does not claim those pending gates have passed.

## Baseline and branch

- Verified baseline: `repair/m1-certification` at `8c18f53`.
- Original implementation branch: `hackathon/m2-m11-mvp`; current fast-forward integration branch: `codex/main-integration`.
- GitHub `main` contains the verified M2-M11 hackathon implementation through `424aeb6`; the final acceptance evidence follows as a documentation-only release commit.
- M0.5/M1 history and frozen `corvus` CLI behavior remain intact. Additive MVP code lives under `corvus.mvp`, uses `mvp_*` SQLite tables, and exposes a separate `corvus-mvp` command.

## Architecture

- `CorvusService` is the authoritative workflow, effect, approval, budget, artifact, conversation, and event core.
- `GovernanceService`, `OfflineConnectorService`, and `ChannelIngressService` share the same transactional SQLite store.
- CLI, FastAPI, generated TypeScript client, and React UI are thin adapters. They do not duplicate authority rules.
- Credentials persist only as `env://` or `keyring://` references and are resolved only by the broker at an effect boundary.
- Signed connector/channel envelopes remain proposals or untrusted input; server-side identity and authorization decide what is accepted.

## Google identity and cross-device continuity (Full-product Milestone 1)

- Hosted Corvus now starts with Google OAuth and keeps session, CSRF, authorized workspace selection, and ordered sync state in memory as server-owned authority.
- Resumable onboarding persists Everyday/Developer with exact versions, requires explicit Individual or Team workspace creation, and never treats a persona choice as membership or permission.
- The shell presents experience and workspace type as read-only identity, supports only authorized workspace selection, and routes profile changes to a truthful Settings surface.
- Workspace synchronization is ordered, idempotent, conflict-aware, provenance-bound, secret-safe, and acknowledgement-after-reduction; stale workspace/auth operations cannot acknowledge or restore superseded state.
- Real loopback Local mode remains authority-neutral and consumes the legacy pairing/session/project API without fabricating hosted account, workspace, CSRF, or migration truth. Corvus Cloud remains visibly disabled Preview.
- Desktop, tablet, and mobile layouts preserve keyboard focus, nested dialog containment, reduced motion, 44px targets, and the four profile-specific navigation/copy adaptations.

## Agent runtime foundation and local-agent fast track

- Strict agent-run contracts bind the complete request, provider binding version/digest, executable or credential identity, model, scope, authorization snapshot, autonomy, credential, budget, kill-switch, sandbox, network, filesystem, tool, and idempotency inputs.
- A verified agent-run authorization adapter reuses the canonical capability-intersection and authorization-snapshot verification core; it does not introduce a second policy engine.
- Start, resume, cancel, event streaming, and audit receipts reject substituted runtime identities, non-proof resume changes, invalid event ordering, duplicate provider events, secret-bearing payload keys, and unacknowledged or tampered audit outcomes.
- Capability discovery is fail-closed and typed. Deterministic contract adapters remain intact; installed native Codex and Claude CLIs are now discovered and connected for paired same-device runs. Gemini and xAI/Grok remain Preview, while Cursor remains unavailable.
- The composer exposes verified providers, recommended models, thinking effort, Chat/Build mode, and explicit MCP opt-in. Messages plus provider-supplied safe reasoning summaries and generic work status stream over owner-scoped SSE.
- Chat is read-only. Codex Build runs in a fresh workspace-write sandbox, always disables user plugins/apps/hooks, can use configured MCP servers only after explicit opt-in, and hands back a bounded, secret-screened ZIP with a SHA-256 manifest. Real Windows acceptance produced and verified the requested project artifact.
- The backend now authors the safety preview shown in the composer, requires its current digest before Build starts, and issues an owner-scoped terminal receipt. The UI does not claim network blocking: it states that network follows the selected CLI sandbox policy and that Corvus grants no separate permission.
- Durable provider binding, autonomy, credential, budget, kill-switch, and post-effect audit-reconciliation repositories remain an explicit later milestone. M2A does not claim those production integrations.

## Install and run

```powershell
uv sync --all-groups --locked
cd apps/web
pnpm install --frozen-lockfile
pnpm build
cd ../..
```

Migrations run transactionally when the SQLite-backed service opens.

```powershell
$env:CORVUS_BOOTSTRAP_TOKEN = '<one-time-pairing-value>'
$env:CORVUS_SESSION_SECRET = '<at-least-32-byte-signing-value>'
uv run corvus-mvp server --database corvus-mvp.sqlite3
```

In a second terminal:

```powershell
cd apps/web
pnpm dev
```

Open `http://127.0.0.1:5173`, pair once, then use Execution and Operations. For a CLI-only durable demo:

```powershell
uv run corvus-mvp demo --database corvus-mvp.sqlite3 --json
uv run corvus-mvp workflow inspect <WORKFLOW_ID> --database corvus-mvp.sqlite3 --json
uv run corvus-mvp capabilities-demo --database corvus-mvp.sqlite3 --json
```

For the production-style, single-origin local path, build once and let FastAPI serve the real web client:

```powershell
cd apps/web
pnpm build
cd ../..
uv run corvus-mvp server --database corvus-mvp.sqlite3 --static-web-dir apps/web/dist
```

The reproducible self-host container path is:

```powershell
Copy-Item .env.example .env
# Replace both demo secret values in .env, then:
docker compose up --build
```

Build the wheel and bind it plus the static asset inventory into SBOM/provenance output:

```powershell
uv build --wheel
uv run python -m scripts.generate_supply_chain --artifact dist/corvus-0.2.0a1-py3-none-any.whl --static-dir apps/web/dist
```

Build and launch the Windows desktop shell from a Visual Studio Developer PowerShell:

```powershell
$env:PATH = "C:\Users\lucas\.cargo\bin;$env:PATH"
$env:CORVUS_SIDECAR_EXECUTABLE = (Resolve-Path .venv\Scripts\corvus-mvp.exe).Path
pnpm --dir apps/desktop install --frozen-lockfile
pnpm --dir apps/desktop tauri build --no-bundle
& apps\desktop\src-tauri\target\release\corvus-desktop.exe
```

## Concise end-to-end demo

The CLI demos use the same SQLite core as HTTP, web, and desktop and emit identifiers for inspection:

```powershell
$db = Join-Path $env:TEMP "corvus-acceptance.sqlite3"
Remove-Item $db -ErrorAction SilentlyContinue
$run = uv run corvus-mvp demo --database $db --json | ConvertFrom-Json
uv run corvus-mvp workflow inspect $run.workflow_id --database $db --json
uv run corvus-mvp capabilities-demo --database $db --json
pnpm --dir apps/web build
```

`demo` creates the project, versioned outcome, dependency graph, attempts, leases, checkpoints, artifacts/lineage, conversation/events, approval replay, and settled budget, then reopens SQLite before reporting success. `capabilities-demo` exercises team/provider references, shadow autonomy, governed memory, an active versioned skill and routine, offline queue/reconciliation, signed channel identity/step-up, and duplicate-safe replay. Start the documented single-origin server to inspect the same durable run through CLI and web/SSE; launch the desktop command above to use that same client in Tauri.

## Milestone status

### Implemented and locally verified

- **M2:** durable outcomes, dependency graphs, legal states, scheduler, attempts, fenced leases/heartbeat/recovery, checkpoints, artifacts/lineage, conversations/events, typed effects, deterministic idempotency, one-time approval decisions, budget reservation/settlement/release, kill switches, controls, and restart persistence.
- **M3:** additive project/outcome/workflow CLI, workflow inspection, configuration checking, durable demo, and governed capabilities demo over application services.
- **M4:** one-time pairing, signed cookie sessions, CSRF/origin checks, tenant-scoped API, typed OpenAPI responses/errors, workflow controls, team/provider/memory/skill/routine/offline/channel routes, signed HTTP channel ingress, and bounded resumable SSE.
- **M5:** generated OpenAPI TypeScript client; connected React pairing, project/outcome/workflow execution, dependency rail, live SSE activity, approval inbox with approve/reject, budget and kill controls, artifacts/conversation inspector, collaboration/provider/autonomy controls, governed memory, skills/routines, and offline/channel visibility. Desktop and mobile browser paths were exercised.
- **M6:** teams/memberships, owner authorization, provider references/grants, secret broker, simulated OAuth PKCE/device flow, shadow decisions, and evidence-gated autonomy promotion.
- **M7:** governed memory with untrusted retrieval, versioned active skills, routines, and authorized routine runs.
- **M8:** Ed25519-signed offline intents, disconnect/queue/reconcile, duplicate-safe application, and restore quarantine.
- **M9:** Ed25519-signed channel envelopes, expiry/digest/signature checks, identity mapping, deduplication, persisted results, step-up state, and real HTTP ingress.
- **M10:** validated local/self-host configuration, tenant isolation queries, simulated OIDC mapping, compiled React assets served by FastAPI on the API origin, non-root multi-stage container/Compose configuration, wheel build, deterministic CycloneDX SBOM, static asset inventory, and artifact-bound provenance.
- **M11:** Tauri v2 Windows shell; validated fixed-argv sidecar launch; starting/ready/reconnecting/failed/stopped supervision; launch-instance-authenticated readiness checks; restart-safe ephemeral session repair; fragment-only one-time pairing; effective HTTP CSP; bounded redacted diagnostics; real compiled web client; graceful window/stdin shutdown with kill fallback; restricted loopback navigation with no remote Tauri IPC; unsigned current-user NSIS packaging; and expiring, rollback-protected, threshold-signed update metadata using explicitly non-production test keys.
- **Release beta:** Windows/macOS/Linux unsigned installer workflow, standalone packaged `corvus-mvp` sidecar, tag-gated GitHub prerelease publishing, Vercel project `corvus-platform`, GitHub `main` integration rooted at `apps/web`, and hosted Local handoff that keeps same-machine session secrets off the hosted origin.

### Implemented but not externally exercised

- PostgreSQL configuration recognition, real OAuth/OIDC providers, external secret stores, production key ceremonies, and external channel/provider delivery.

### Scaffolded or partial

- No M2-M11 feature surface is placeholder-only. Production integrations listed below remain outside the hackathon local path.

### Blocked

- Docker/Podman is not installed, so the authored container image has not been built or started on this workstation. The equivalent single-origin source-tree runtime was exercised successfully.

## Verification actually run

- Python: `1062 passed, 6 skipped` across the serial Windows unit, contract, security, MVP, CLI, and integration groups; the skips are the explicitly opt-in destructive PostgreSQL cases and POSIX-only process-group case. Ruff and strict mypy passed across the repository.
- API/OpenAPI: focused API suite passed; OpenAPI and generated TypeScript hashes were stable across two consecutive generations.
- Web: `153 passed` across 24 files; the Vite production build passed (319 modules, 485.34 kB JS / 145.21 kB gzip).
- M10 packaging: wheel `corvus-0.2.0a1-py3-none-any.whl` built; provenance bound that wheel and a 23-file static manifest; single-origin `/ready`, `/`, and pairing smoke passed and the listener stopped cleanly.
- M11 desktop: Python subprocess two-launch start/HMAC-ready/web/re-pair/persistence/shutdown passed; 7 Rust lifecycle, fixed-launch, fresh-challenge decoy rejection, diagnostic-redaction, and fragment tests passed; Cargo fmt, Clippy with warnings denied, and `cargo check` passed. Windows Computer Use launched the exact worktree release binary, verified the simplified one-sidebar UI, detected Local Codex, streamed a live prompt through the sidecar, and rendered the exact `CORVUS_UI_OK` completion without HTTP 500. The unsigned NSIS `Corvus_0.2.0-beta.2_x64-setup.exe` remains a non-production artifact.
- Release beta: local Windows PyInstaller sidecar build passed and `--help` ran; Tauri NSIS release build passed; silent installer wrote `corvus-desktop.exe`, `corvus-mvp`, and `corvus-mvp.exe` into `%LOCALAPPDATA%\Corvus`; launching the installed app started both `corvus-desktop.exe` and the packaged `corvus-mvp.exe`. Vercel deployment `dpl_4kqEKRLSybjAHik6EUSAezPrRyYH` is Ready and aliased at `https://corvus-platform-tau.vercel.app`; project root was restored to `apps/web` for GitHub `main` deployments.
- Browser: real FastAPI + Vite pairing, safety-details disclosure, ready-only provider/model/effort controls, Settings, desktop layout, and 390x844 mobile composer geometry passed. The final authenticated run logged zero console errors or warnings and no horizontal overflow.
- Design blueprint: the packet, source evidence, and fixed-viewport captures were completed and reviewed during implementation, then removed from the tracked submission by the explicitly approved `.antigravity/` repository-hygiene cleanup. The resulting UI, responsive behavior, and regression tests remain in the product source; no fake restaurant artifact was added to satisfy the incompatible legacy auditor.

## Final 20-point acceptance evidence

1. The install, single-origin server, CLI, web, and desktop startup sequences are documented above.
2. API, real browser, and repeat desktop launch pairing succeeded; the desktop fragment was removed before HTTP traffic.
3. The final CLI acceptance run created a persisted project.
4. It created a versioned outcome and a two-item dependency graph.
5. It started the workflow through the authoritative application service.
6. The scheduler completed both work items with the deterministic executor.
7. CLI inspection found two work items, two artifacts with lineage, 12 events, two conversation entries, and durable attempts/leases/checkpoints in the same SQLite core.
8. The second item required an approval-bearing filesystem effect.
9. The effect executed exactly once; duplicate approval/application tests safely replayed the persisted decision.
10. The 10-unit budget ended with 4 settled, 0 reserved, and 6 available.
11. SSE monotonic replay/reconnect passed in API tests and the real browser execution path.
12. The same durable run was inspected by CLI, API, and connected web controls.
13. The capabilities run connected a provider reference and returned supervised autonomy after shadow/evidence evaluation.
14. Governed memory stored and retrieved external content as untrusted.
15. A versioned active skill completed through a routine.
16. An offline intent queued while disconnected, reconciled as applied, and retained application count 1.
17. A signed channel event mapped identity, required step-up, and retained processing count 1 after retry.
18. The production web application built successfully.
19. The Tauri release executable and unsigned current-user NSIS package built; a real WebView launch rendered the connected UI.
20. The demo reopened the backend store and reported `restart_verified: true`; the desktop also re-paired across launches using the same persisted database.

## Known limitations

- The deterministic local effect adapter returns digest-bound results and does not perform privileged host writes or real provider calls.
- No production cloud, PostgreSQL server, external OAuth registration, notarization, production signing, or multi-OS installer certification was attempted.
- The container definition is authored but not locally exercised because no container engine is installed.
- The unsigned beta installers are not production signed, notarized, or certified. macOS and Linux artifacts are built and verified through GitHub Actions rather than this Windows workstation.
- Local Codex and Claude are selectable when their native CLIs are detected; each provider verifies its own sign-in when a run starts. Gemini and xAI/Grok remain Preview; Cursor is unavailable; API-key and Cloud provider execution remain deferred.
- Durable current-state repositories for agent provider bindings, autonomy grants, credential proofs, runtime budgets, kill switches, and post-effect audit reconciliation require a later explicitly authorized persistence milestone.

## Build Week 2026 submission snapshot

- Release version and immutable tag: `v0.2.0-build-week.1`.
- Release title: **Corvus Build Week 2026 Hackathon Submission**.
- Source review: PR #16 into `main`; the release workflow rejects tags whose commit is not contained in `main`.
- Packaged platforms: Windows x64 NSIS, macOS Intel x64 DMG, Linux x64 AppImage and Debian package.
- Integrity evidence: one `SHA256SUMS.txt` generated from every published installer.
- Distribution policy: published, non-draft prerelease; unsigned and explicitly labeled as such.
- Freeze boundary: this tag and its assets are the judging snapshot. Later branches, commits, deployments, and releases are post-hackathon work.
