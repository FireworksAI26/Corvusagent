# Security Review Checklist (Corvus)

Required for every PR that touches any path in `.github/CODEOWNERS`
(security, sandbox, authorization, verification, trust root, `tests/security/`).
The security owner (Asif) must approve before merge. CODEOWNERS routes review;
enforcement depends on protected-branch settings requiring code-owner approval.
CI runs the automated gates, and a human runs this checklist.

## Fail-closed by default
- [ ] Authorization decisions **deny by default**. An unverified or
      erroring check must block, never allow.
- [ ] No new path introduces a default-allow branch in
      `corvus/application/authorization.py`, `corvus/infrastructure/project_authorization.py`,
      the explicit authority/repository modules listed in CODEOWNERS,
      `corvus/mvp/governance.py`, or `corvus/mvp/core.py` (effect approval,
      execution, idempotency, and budget state transitions).

## No secret material
- [ ] No real credentials committed. `.env` files stay gitignored; fixtures
      must NOT use the `.env` filename (see the force-tracked
      `tests/fixtures/v1/legacy/config/.env` footgun — rename it).
- [ ] Until a dedicated required CI job exists, run and record a manual
      repository secret scan (`gitleaks` or `trufflehog`) before approval.
- [ ] `.env.example` values remain placeholders only.

## Sandbox execution
- [ ] No host mounts, privilege escalation, or `--privileged` in the sandbox path.
- [ ] Sandbox base image is pinned by `sha256` (matches
      `.github/workflows/certification.yml`).
- [ ] `tests/security/test_sandbox_options.py` passes.

## Proof, not claims (proof-carrying completion)
- [ ] Every authorization/verification decision emits **evidence** — a test,
      log line, or hash — not just a boolean. No silent `return True`.
- [ ] `corvus/verification.py` changes are covered by a test that asserts the
      evidence artifact is produced.

## Agent-to-agent / input boundaries
- [ ] Inputs at agent boundaries are validated before use (no injection from
      agent messages into auth/sandbox decisions).
- [ ] Untrusted input never reaches `eval`/`exec`/shell without going through
      the sandbox path.

## Tests & CI
- [ ] `tests/security/` updated for the change; suite green on all matrix OS.
- [ ] `bandit`, `pip-audit`, and `ruff` clean (certification.yml gates).
