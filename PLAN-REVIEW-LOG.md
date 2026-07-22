# Plan Review Log: Corvus CLI V2 and Shared Web/Desktop Platform
Started 2026-07-13 12:58:48 +0800. Initial reviewer: Gemini CLI gemini-3.1-pro-preview. The external three-round target was followed by recorded Hermes refresh rounds because external reviewers were unavailable.

## Planner availability
Claude Code planning was attempted first and returned HTTP 401 before reading or changing the repository. No Claude-generated plan content was used.

## Round 1 - Gemini (blocked)
Gemini CLI exited before reading the plan with `IneligibleTierError`: the installed Gemini Code Assist for individuals client is no longer supported and requires migration to Antigravity. No Gemini critique or verdict was produced.

### Codex/Hermes response
- No reviewer feedback was incorporated because none was produced.
- The plan remains unapproved by Gemini.
- Lucas explicitly approved a fallback consisting of read-only Codex CLI review plus two independent Hermes audits.

## Round 2 - Codex CLI fallback (blocked)
Codex CLI ran in an ephemeral read-only sandbox and inspected the plan and V1 source, including a security-plugin preflight. It did not return a final message or verdict within the bounded ten-minute review window and was terminated. The configured output file was not created. No Codex critique was incorporated and no approval is claimed.

### Remaining approved review gate
Use the two independent Hermes audits already dispatched before implementation. Record their concrete findings and the Codex/Gemini limitations without representing either external reviewer as approved.

## Round 3 - Initial independent Hermes audits
Two read-only GPT-5.6-sol reviewers inspected all 27 V1 source files. Both concluded that V1 is a strong single-user prototype but must not be wrapped directly in a web API or split into independent clients.

### Material findings
- Critical: no multi-user authorization/tenant boundary; policy is largely disconnected from execution.
- Critical: snapshots can include secrets and retained plaintext; command output can be sent back to the model.
- Critical: the generating model selects its own checks, smoke checks are unused, and repair attempts can reuse stale files.
- Critical: approved bundle files are not rehashed before apply; approval is same-command/in-memory; journal and locking leave crash/TOCTOU windows.
- High: structured secret redaction fails for ordinary nested JSON; provider URLs create server-side SSRF risk; Codex children inherit a broad host environment.
- High: empty audit chains verify successfully, the chain is recomputable by a database modifier, artifact digests are not strictly validated, resource limits are incomplete, and optional token-budget narrowing can widen limits.
- Product: preserve fail-closed sandboxing, explicit chat/build boundary, delivery/undo ergonomics, transparent model routing, bounded delegation, doctor/trace JSON, and local-first behavior.
- Product: extract one headless control plane and versioned protocol; ship CLI and web against it before wrapping the proven web client in Tauri.
- Migration: add golden tests for all public commands and JSON shapes plus an idempotent V1 importer before schema evolution.

### Codex/Hermes response
Accepted all release-blocking findings. `PLAN.md` now adds Milestone 0.5 for snapshot/redaction/verification/delivery/provider/audit hardening; signed audit checkpoints; golden command/protocol tests; an idempotent V1 importer; and a runtime-profile capability projection. Web, desktop, and channel adapters remain blocked until these gates and the refreshed post-configuration audits pass.

The reviewers began before the local baseline Git repository was initialized; their statement that the archive was not a Git repository was accurate at audit start. The current repository now has immutable baseline commit `1410d7f`.

## Round 4 - Refreshed post-configuration Hermes review
The paired security/schema reviewer timed out after 15 calls and produced no summary or verdict. This is blocked evidence, not approval.

The completed product-topology reviewer inspected committed plan `a59babe` and returned `VERDICT: REVISE`. Material findings required: split overloaded `RuntimeProfile`; event-level private scope/audience; acting-agent grants and authority intersection; implementable connector and provider/credential lifecycles; one authoritative control plane per workspace; concrete production auth and SSE semantics; transport-neutral Python ports; a smaller real project vertical slice; distribution/desktop lifecycle; team/provider/channel work before desktop; channel step-up approval; supported SQLite/cross-platform envelope; and persisted idempotency semantics.

## Lucas feature brief - locked requirements
Lucas added outcome contracts; evaluated/canary/rollback skills; context firewall; scoped temporary secret broker; six autonomy levels; shadow mode; durable workflow graphs and bounded recovery; artifact lineage; governed memory; kill switches and limits; and offline local-model/cache/memory/queue operation. Security and the project create/read flow must precede CLI V2, FastAPI expansion, or React UI work. Existing modules should be reused, compatibility preserved where practical, changes remain migration-backed and TDD-driven, secrets never enter prompts/events/traces/snapshots/errors, and incomplete work must remain explicit.

### Codex/Hermes response
`PLAN.md` now separates deployment, workspace, client, execution, model-route, and credential contracts; adds an allowed-combination table and effective-capability projection; models scope/audience events, acting-agent/delegation authority, provider/credential grants, outcome/workflow/lineage/kill-switch records, context firewall, governed memory/skills, autonomy/shadow promotion, connector/offline behavior, concrete auth/SSE/idempotency, distribution, and the locked implementation sequence. The immediate authority milestone is now one migration-backed transport-neutral project create/read vertical slice, not a broad synthetic foundation or early CLI/API/UI expansion.

### Remaining gate
Commit and hash this revised plan, then run a fresh independent review against that exact revision. Do not begin Task 1.1 schema/domain implementation while that verdict is pending or requires revision.

## Round 5 - Exact-commit paired Hermes review of `6bc2136`
Two fresh read-only GPT-5.6-sol reviewers completed non-overlapping reviews of exact commit `6bc2136038c8abdea7746243cb057c004969f150`. Both returned `VERDICT: REVISE`; this is not approval and no authority/schema implementation began.

### Security/schema/authorization findings
- Critical: “one authoritative control plane” lacked persisted epochs/fencing and atomic handoff, so remote-offline work could execute under stale grants.
- Critical: leases prevented duplicate completion but not duplicate external effects or kill-switch/budget check-to-effect races.
- High: invalid-state-prone optional scope fields and digest-only audience references could not support deterministic replay authorization.
- High: credential grants were insufficiently bound to workspace, provider connection/version, request/agent grant, placement, purpose, and usage.
- High: idempotency lacked a complete persisted principal/context key and could replay data after revocation.
- High: workflows did not pin immutable outcome versions and lineage used mutable bare-ID arrays.
- High: audit checkpoints lacked a corresponding transactional workspace-global sequence, authority epoch, schema version, and signing-key lifecycle.
- High: autonomy levels had no normative effect semantics; shadow mode could still use existing credentials/effects.
- High: memory owner/scope/audience/promotion/encryption/deletion-retention contracts were ambiguous.
- High: V1 migration needed complete golden fixtures, version-aware bootstrap, quarantine, and policy/autonomy coverage before schema evolution.
- High: the Milestone 1 client-surface denial contradicted transport-neutral authority.
- High: desktop updates lacked a threshold-signed trust root, rollback/freeze protection, key lifecycle, provenance, and SBOM binding.

### Product topology/sequencing findings
- Worker B was allowed to overlap unfinished Milestone 0.5 work, contradicting the security gate.
- One all-domain importer was scheduled before most destination schemas existed and omitted V1 policy/autonomy YAML.
- The eleven locked capabilities lacked a complete persistence/migration/cutover/rollback/test ledger.
- Provider OAuth lacked a persisted authorization, PKCE/device, callback, refresh, revocation, and acceptance lifecycle.
- Self-hosted and vendor-cloud modes lacked an operational deployment milestone for PostgreSQL, workers, artifacts, vault/KMS, isolation, backup/restore, observability, and rollback.

### Codex/Hermes response
Accepted all 17 findings. `PLAN.md` now adds monotonic `WorkspaceAuthority` epochs and signed atomic handoff; queue-only remote-offline intents; a fenced centralized external-effect gateway with semantic idempotency, transactional outbox, current authorization/limits, and compensation; discriminated scopes and immutable audience snapshots; fully bound credential/OAuth records; authorization-aware idempotency; immutable outcome/lineage/audit contracts; normative autonomy/shadow effect rules; typed governed memory; complete V1 golden/quarantine/bootstrap work followed by per-domain importers; client-surface parity; TUF-style updates; an eleven-row capability implementation ledger; strict Worker A -> review -> Worker B -> review -> Worker C -> review -> Worker D sequencing; and a separate operational self-hosted/vendor-cloud deployment milestone.

### Remaining gate
Validate, commit, and hash the new plan-only revision, then rerun the same two independent read-only scopes against that exact commit. No remaining implementation begins while either verdict requires revision.

## Round 6 - Exact-commit paired Hermes review of `74e98a7`
Two fresh read-only GPT-5.6-sol reviewers verified exact commit `74e98a74b7ae4b0648e6437911b4ec136c579c9a`, `PLAN.md` SHA-256 `a2d37bf8d7796e53bec63976912803b8a91011ed9134e1ba3f5a5f3888cccdd3`, and a clean unchanged tree. Both returned `VERDICT: REVISE`; no implementation began.

### Security/schema/authorization findings
- High: authority epoch/closed state remained rollbackable because a restored pre-handoff database could resurrect the old active authority without a non-exportable capability, expiring external lease, anchored close/revocation or restore quarantine.
- High: external-effect records lacked resolvable request/requester/access/agent/delegation/placement/provider/credential/payload authorization evidence needed for the promised current gateway checks.
- High: budget accounts/reservations/settlements and unique materialized kill/permit/outbox/attempt constraints were missing; unknown non-idempotent provider outcomes could still duplicate effects on retry.
- High: immutable authorization-decision snapshots and durable workspace signing-key versions/validity/rotation/revocation/compromise semantics were not modeled or bound throughout requests, receipts, checkpoints, effects and handoffs.

### Product topology/client findings
- High: `local_daemon` was declared supported after the API slice without a Milestone 4 bootstrap/pairing/rotation/origin/CSRF/lifecycle/recovery gate.
- High: several locked capabilities had persistence/service tests but no owned application ports, retained-CLI cutover, API/web tasks or client-driven end-to-end proof, allowing parallel legacy implementations.

### Codex/Hermes response
Accepted all six findings. `PLAN.md` now makes authority non-rollback through non-exportable epoch credentials or expiring registry leases, externally anchored close certificates, old-capability destruction/revocation and default restore quarantine; restores from pre-handoff snapshots must remain read/queue-only. It adds self-contained signed `AuthorizationDecisionSnapshot` and durable `WorkspaceSigningKeyVersion` records with historical compromise semantics and binds them through requests, receipts, checkpoints, handoffs, effects and lineage. Effects now carry the complete authorization/provider/credential/payload chain; concrete hierarchical budget accounts/reservations/settlements, unique materialized kill rows, one-per-intent permits/outboxes, unique attempt numbers, canonical lock order and `outcome_unknown` no-auto-retry rules close local race/duplicate paths. Milestone 4 now owns the local-daemon bootstrap/auth/pairing/rotation/origin/CSRF/lifecycle/recovery proof. A second eleven-row ledger and Milestones 6–8 now require named application ports, retained-CLI cutover, API/web ownership and real client-driven OAuth, broker, autonomy, shadow, memory, skill, context and offline end-to-end tests.

### Remaining gate
Validate, commit and hash this plan-only revision, then rerun both independent scopes against that exact commit. Continue the review/revise cycle until both return `VERDICT: PASS`; implementation remains blocked.

## Round 7 - Exact-commit paired Hermes review of `134475e`
Two fresh read-only GPT-5.6-sol reviewers verified exact commit `134475ea8a1c5fe0c94f815f2575647240499d35`, `PLAN.md` SHA-256 `23bdee0833328ed472e93e03b572dff07da2655a63b042d9c54dbcef0c3ed1c8`, and a clean unchanged tree at both boundaries. Both returned `VERDICT: REVISE`; no production files changed and implementation remained blocked.

### Security/schema/authorization findings
- High: the non-exportable-key branch did not prevent a same-host clone or in-place rollback from reusing one OS-held key; restore could `resume_same_epoch`, and no externally pinned/versioned registry-verifier history protected lease/close certificates.
- High: external effects bound only a mutable ciphertext locator plus sanitized digest rather than immutable ciphertext identity, encryption/commitment key versions and a keyed commitment to exact canonical plaintext across permit/outbox/attempt/audit/lineage.
- High: tuple uniqueness did not prevent overlapping budget periods; parent rows lacked enforceable same-workspace/single-parent/legal-order/acyclic constraints; list-valued permit reservations did not prove complete ancestor coverage.

### Product topology/client finding
- High: the matrix enabled local-daemon web support after Milestone 4 even though the actual generated web client and API-backed Playwright pairing/reconnect tests were not scaffolded until Milestone 5.

### Codex/Hermes response
Accepted all four findings. `PLAN.md` now requires every authority-bearing commit to advance an externally registered or device-sealed monotonic generation/state root under one deployment-instance lease or fixed workspace OS lock; same-epoch clones and in-place rollback quarantine, registry verifier keys are pinned/versioned with compromise recovery, and restore mutation requires former-instance revocation plus exclusive new-epoch takeover. Effects now use immutable content-addressed `EffectPayloadVersion` records with ciphertext SHA-256, encryption/commitment key versions and canonical-plaintext HMAC bound through intent, permit, outbox, attempt, audit and lineage and reverified immediately before dispatch. Budgets now use non-overlapping canonical periods, same-workspace legal single-parent/closure rows, cycle checks and normalized permit-reservation bindings that must equal the complete ancestor closure. Local-daemon support is staged: retained CLI only after Milestone 4; real web support only after Milestone 5's generated-client pairing/session/rotation/restart/reconnect Playwright gate.

### Remaining gate
Validate, commit and hash this exact plan-only revision, then rerun both independent scopes. Continue until both return `VERDICT: PASS` on one commit; implementation remains blocked.

## Round 8 - Exact-commit paired Hermes review of `8343418`
Two fresh read-only GPT-5.6-sol reviewers verified exact commit `834341842a870fd723093bf511540cf6a35a6a69`, `PLAN.md` SHA-256 `498b72b3a3e79f1dcebdf8cc7c0d316b1778f204cebd2626333c4c0a4eec71ab`, and a clean unchanged tree at both boundaries. Both returned `VERDICT: REVISE`; no production files changed and implementation remained blocked.

### Security/schema/authorization findings
- High: locally stored registry verifier history could be rolled back to an older still-valid chain prefix because it lacked an outside-database monotonic history head, mandatory expiry, and fresh challenge binding.
- High: the authority-state root omitted deployment-instance, trust-anchor, lease/epoch-credential, registry-verifier, and workspace-signing-key lifecycle families, allowing selective rollback without changing the accepted root.
- High: audit anchoring was circular because a receipt appeared to hash the resulting state root while that root also included the receipt hash.
- High: budget parentage enforced rank only, not same unit, real semantic owner ancestry, or compatible parent/child active windows.
- High: request-owned reservations could be reused by multiple permits and settlements, so one held capacity could authorize multiple external effects.

### Product topology/client finding
- High: self-hosted and vendor-cloud could be called supported after infrastructure fixtures without exercising real authentication, callback, session/CSRF, rotation, reconnect, upgrade, or restore flows through the retained CLI HTTP and generated React clients; desktop support was not separately gated through Tauri.

### Codex/Hermes response
Accepted all six findings. `PLAN.md` now adds threshold-signed, expiring, externally/sealed `AuthorityRegistryTrustState` metadata with a monotonic complete-history head plus nonce/sequence-bound freshness proofs and adversarial prefix/freeze/recovery tests. A schema-versioned exhaustive authority-root manifest requires every mutable authority/key family to be an in-root leaf or named external proof, and per-family rollback/unlisted-family tests fail closed. Audit receipts now sign only prior authority state and intended mutation; the receipt hash becomes the proposed audit-head leaf, and an immutable post-finalization `AuditAnchorBinding` proves the resulting generation/root/commit receipt without circularity, with crash tests at every boundary. Budgets now use database-validated real-owner scope nodes/containment, same-unit and containing-window account links, exclusive per-effect reservation sets, globally unique permit bindings, and one-time conserved settlements. Milestone 10 now requires no-substitute self-host/cloud authentication and lifecycle flows through the retained CLI HTTP and actual generated React clients before enabling CLI/web/channel support; Milestone 11 reruns each flow through Tauri before enabling desktop.

### Remaining gate
Validate, commit and hash the new plan-only revision, then rerun the same two exact-commit scopes. Continue until both return `VERDICT: PASS` on one immutable commit; implementation remains blocked.

## Round 9 - Exact-commit paired Hermes review of `0f4a560`
Both read-only reviewers targeted exact commit `0f4a560863e06d7c886fd8db1b16c0c6ae621984` and `PLAN.md` SHA-256 `841681e6b5e70e471f199245d6ecd6a81b7f0269198089cb64e9ad32cfd7f12e`. The security/schema reviewer timed out after 21 calls and returned no summary or verdict; that is blocked evidence, not approval. The product/topology reviewer verified the exact commit/hash and a clean unchanged tree at both boundaries, modified no files, and returned `VERDICT: REVISE`. Implementation remained blocked.

### Product topology/client findings
- High: retained `corvus chat`, `corvus run`, and the Textual TUI still directly constructed configuration/provider/workflow/`ConversationRuntime` paths; no sequenced in-process/HTTP/SSE client cutover prevented the main interface from bypassing the composition root when CLI topology support was enabled.
- High: retained review/approve/apply and undo still called `DeliveryManager` directly; no delivery/approval/undo application ports or real-client current-authorization/effect/audit/conflict/crash/undo parity gate owned that authority-bearing filesystem mutation path.
- High: Milestone 10 enabled self-hosted support before the wheel/standalone CLI, OCI image and static web distribution artifacts were created in Milestone 11, allowing source-tree fixtures rather than shipped artifact digests to satisfy install/upgrade/rollback claims.

### Codex/Hermes response
Accepted all three completed findings. `PLAN.md` now has a retained platform-surface cutover ledger. Milestone 2 defines durable conversation command/query/event ports and delivery query/approval/apply/undo ports over the centralized authority/effect/audit path. Milestone 3 routes the actual retained chat/run/TUI and review/apply/undo adapters through the in-process client, forbids direct runtime/provider/workflow/delivery construction, and proves command/exit/JSON, restart, cancellation, event parity, approval conflict/replay/expiry, crash recovery, current revocation and undo receipts before enabling embedded CLI support. Milestone 4 reruns the same retained surfaces over real HTTP/SSE before daemon CLI support; Milestone 5 adds generated-web conversation and delivery controls; Milestones 10–11 rerun them through packaged network and Tauri clients. Milestone 10 now creates, signs, provenance/SBOM-binds, installs, upgrades, restores and rolls back the exact wheel/standalone CLI, OCI image and static web digests before self-host/cloud support. Milestone 11 is limited to installed Tauri/sidecar artifacts and desktop-specific updates. The timed-out security scope must be rerun against the next exact candidate.

### Remaining gate
Validate, commit and hash this plan-only revision, then rerun both independent scopes. Continue until both return `VERDICT: PASS` on the same immutable commit; implementation remains blocked.

## Round 10 - Exact-commit paired Hermes review of `4927aab`
Both read-only reviewers verified exact commit `4927aab94fd51c3583dd018b33d2f08eec9684d3`, `PLAN.md` SHA-256 `fa7c553b409a026f152f2a28d4759f16afeed979e5ef1041fb40f5d1dc1adeb1`, review-log SHA-256 `3d1362fefd0cd49c999fba8ef71c1a6484b30c277cf5364db522e08613baf12c`, and a clean unchanged tree at both boundaries. The product/topology reviewer returned `VERDICT: PASS`; the security/schema reviewer returned `VERDICT: REVISE` with two HIGH findings. Implementation remained blocked because both verdicts did not pass the same commit.

### Security/schema/effect findings
- High: generic external-effect intent/attempt records required provider connection and credential fields, while filesystem apply/undo has neither; permits/gateway locks also lacked exact approval request/decision binding and atomic consumption, forcing synthetic credentials or a parallel delivery path.
- High: reservation/settlement amounts lacked positive/nonnegative database constraints and set-wide equality, allowing negative values or child/ancestor skew to mint reusable capacity even if each individual settlement equation balanced.

### Product topology result
- Pass: all three Round 9 findings were materially closed. Retained chat/run/TUI and review/apply/undo have sequenced in-process then HTTP/SSE cutovers, Milestone 10 creates/tests exact packaged non-desktop artifacts before support, and installed Tauri reruns remain separately gated.

### Codex/Hermes response
Accepted both security findings without changing the passing product topology. Effects now use an immutable discriminated `EffectBinding` with exactly one `ProviderEffectBinding` or `FilesystemDeliveryEffectBinding`. Generic intents/attempts no longer require provider credentials. Filesystem apply/undo carries exact bundle, manifest, destination, rollback snapshot and original-successful-apply references. Signed versioned approvals bind one effect, and permit claim serializably reauthorizes and consumes the exact current unexpired/unreplayed approval once. Budgets now use signed 64-bit integer canonical base units, one strictly positive set amount, composite foreign keys that force every closure reservation to carry that unit/amount, a common immutable settlement set with nonnegative actual/released values and conservation, equal per-account rows, closure digest/cardinality checks, and atomic full-set reserve/claim/settle/release transitions. These new approval, effect-binding and settlement families are covered by the exhaustive authority root and adverse tests.

### Remaining gate
Validate, commit and hash this plan-only revision, then rerun both exact-commit scopes. Continue until both return `VERDICT: PASS` on the same immutable commit; implementation remains blocked.

## Milestone 0.5 serial repair candidate
The prior implementation advanced into Milestone 1 without recorded Milestone 0.5 acceptance. The certification repair therefore restarted from pre-Milestone-1 commit `407c981` on branch `repair/m05-certification`; the later Milestone 1 tree remains preserved separately as an audit reference.

Implemented repair slices:
- one byte-exact exhaustive V1 fixture manifest and populated legacy-domain corpus;
- distinct project-local policy/autonomy evidence and source-bound quarantine capture;
- canonical `legacy_run` ownership for new retained V1 model-call provenance;
- aggregate character/byte/chunk/emission/persistence limits with cross-chunk secret redaction and fail-closed provider cancellation;
- cross-platform certification workflow definitions and strict typing dependencies.

Pre-freeze local evidence: 114 tests passed; Ruff lint/format, strict mypy, compile, and Git diff checks passed. Bandit retains documented legacy low-severity findings while container `/tmp` false positives are explicitly scoped. Docker/Podman integration is not claimed from this workstation and is assigned to Linux CI.

### Remaining Milestone 0.5 gate
Freeze one candidate commit/tree, run the complete local and cross-platform certification matrix, and obtain independent security/migration plus V1 compatibility/product `VERDICT: PASS` reports against those exact bytes. Until both pass, Milestone 0.5 is under certification and Milestone 1 remains blocked.
