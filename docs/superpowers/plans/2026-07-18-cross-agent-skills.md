# Cross-Agent Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace saved prompt records with immutable Agent Skills packages and intuitive, safe imports from Codex, Claude Code, Hermes Agent, and generic sources.

**Architecture:** A portable core parser validates Agent Skills while source adapters discover and normalize vendor extensions. Every import copies into quarantine, scans without execution, records provenance, and becomes a draft only after explicit review.

**Tech Stack:** Python pathlib/YAML, SQLite, SHA-256, pytest fixtures, FastAPI, React, Vitest.

## Global Constraints

- Import never modifies its source and never executes package scripts.
- Canonical containment and link/reparse checks precede every copy.
- Imported permissions become unapproved capability requests.
- Skill versions are immutable and runs pin one package digest.
- Exact duplicates are deduplicated; variants are never auto-merged.

---

### Task 1: Portable package parser and validator

**Files:**
- Create: `corvus/mvp/skill_packages.py`
- Test: `tests/mvp/test_skill_packages.py`
- Create fixtures: `tests/fixtures/skills/portable/*`

**Interfaces:**
- Produces: `SkillPackageReader.read(root: Path) -> SkillPackage`.
- Produces: `SkillValidator.validate(package) -> SkillValidationReport`.

- [ ] **Step 1: Write failing tests** for required `SKILL.md`, YAML frontmatter, name/directory match, length constraints, relative references, unsupported file type, file/total-size caps, path escape, symlink/reparse, and deterministic digest.

```python
class SkillPackage(MvpModel):
    name: str
    description: str
    root: Path = Field(exclude=True)
    files: tuple[SkillFile, ...]
    metadata: dict[str, JsonValue]
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
```

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/mvp/test_skill_packages.py -q`

- [ ] **Step 3: Implement strict parser and validator** with UTF-8 frontmatter, normalized POSIX relative paths, 2 MiB per file, 20 MiB per package, and no implicit file execution.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: validate portable agent skill packages`

### Task 2: Skill version storage and activation

**Files:**
- Create: `corvus/mvp/skill_library.py`
- Modify: `corvus/mvp/governance.py`
- Modify: `corvus/mvp/store.py`
- Test: `tests/mvp/test_skill_library.py`

**Interfaces:**
- Produces: `SkillLibrary.import_draft`, `create_draft`, `activate`, `archive`, `list_versions`, and `export`.

- [ ] **Step 1: Add failing tests** for immutable version increments, one active version per scope/name, rollback activation, archive, digest pinning, personal/repository scope, and package-copy ownership.

- [ ] **Step 2: Add normalized schema** for skill identities, versions, files, provenance, validation reports, capabilities, and optional skill sets. Migrate existing saved prompt rows into instruction-only draft versions without claiming they were scanned.

- [ ] **Step 3: Implement Corvus-owned package storage** under `<app-data>/skills/<skill-id>/<version>/` with a staging directory and atomic rename after successful validation.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: store immutable skill package versions`

### Task 3: Codex and Claude discovery adapters

**Files:**
- Create: `corvus/mvp/skill_importers/base.py`
- Create: `corvus/mvp/skill_importers/codex.py`
- Create: `corvus/mvp/skill_importers/claude.py`
- Test: `tests/mvp/test_skill_import_codex.py`
- Test: `tests/mvp/test_skill_import_claude.py`
- Create fixtures: `tests/fixtures/skills/codex/*`, `tests/fixtures/skills/claude/*`

**Interfaces:**
- Produces: `SkillSourceAdapter.discover(context) -> tuple[SkillCandidate, ...]`.
- Produces: `normalize(candidate, quarantine_root) -> NormalizedSkillCandidate`.

- [ ] **Step 1: Write failing tests** for personal/project location precedence, `$CODEX_HOME`, legacy `.codex/skills`, Claude skills, legacy Claude commands, plugin namespace preservation, `$ARGUMENTS`, `${CLAUDE_SKILL_DIR}`, `allowed-tools`, dynamic command injection, and duplicate paths.

- [ ] **Step 2: Implement read-only discovery** of explicit standard roots and repository parent roots only.

- [ ] **Step 3: Implement normalization**

```python
VendorWarning("claude_dynamic_command", severity="review", location="SKILL.md:12")
RequestedCapability(kind="tool", value="Bash(git:*)", approved=False)
```

Legacy commands receive a normalized slug and generated description, but remain `needs_review` until edited or accepted.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: import codex and claude skills`

### Task 4: Hermes, Copilot, generic, ZIP, and optional WSL adapters

**Files:**
- Create: `corvus/mvp/skill_importers/hermes.py`
- Create: `corvus/mvp/skill_importers/generic.py`
- Create: `corvus/mvp/skill_importers/wsl.py`
- Test: `tests/mvp/test_skill_import_hermes.py`
- Test: `tests/mvp/test_skill_import_generic.py`
- Test: `tests/mvp/test_skill_import_wsl.py`
- Create fixtures: `tests/fixtures/skills/hermes/*`

- [ ] **Step 1: Write failing tests** for `~/.hermes/skills`, nested categories, `external_dirs`, environment-variable expansion allowlisting, bundles, Hermes metadata, `.github/skills`, `.copilot/skills`, safe ZIP extraction, and explicit WSL opt-in.

- [ ] **Step 2: Implement Hermes config parsing** without writing config or following external paths beyond each configured root. Convert bundles to version-pinned Skill Sets.

- [ ] **Step 3: Implement generic folder/file/ZIP imports** with Zip Slip, decompression-size, file-count, device-file, and link defenses.

- [ ] **Step 4: Implement WSL discovery** by listing distributions only after the explicit endpoint action and reading only the known skill roots inside the selected distribution.

- [ ] **Step 5: Run tests and commit**

Commit: `feat: import hermes and portable skills`

### Task 5: Quarantine scan and duplicate classification

**Files:**
- Create: `corvus/mvp/skill_scanner.py`
- Create: `corvus/mvp/skill_import_service.py`
- Test: `tests/security/test_skill_import_security.py`
- Test: `tests/mvp/test_skill_import_service.py`

**Interfaces:**
- Produces: `preview_import(source, candidate_id) -> ImportPreview`.
- Produces: `commit_import(candidate_id, expected_digest) -> SkillVersion`.

- [ ] **Step 1: Write failing tests** for secret patterns, exfiltration URLs, destructive commands, prompt injection, executable scripts, unsupported substitutions, exact duplicate, same-name variant, changed-source digest, and quarantine cleanup.

- [ ] **Step 2: Implement classification** as `ready`, `needs_review`, or `blocked`, recording scanner version, findings, source, original digest, normalized digest, and translation diff.

- [ ] **Step 3: Require expected digest on commit** so a changed source or quarantine candidate cannot be imported after preview without re-review.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: quarantine and classify skill imports`

### Task 6: Skills API and UI

**Files:**
- Modify: `corvus/mvp/api.py`
- Modify: `apps/web/src/api.ts`
- Create: `apps/web/src/app/SkillsWorkspace.tsx`
- Create: `apps/web/src/app/SkillImportFlow.tsx`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/styles/product-workspace.css`
- Test: `tests/mvp/test_api_skills.py`
- Test: `apps/web/src/app/SkillsWorkspace.test.tsx`
- Test: `apps/web/src/app/SkillImportFlow.test.tsx`

- [ ] **Step 1: Add API and UI tests** for source cards/counts, manual refresh, selection, compatibility badges, translation preview, duplicate choices, draft activation, versions, archive, Run now, Schedule, and export.

- [ ] **Step 2: Implement `/api/local/skills`, source discovery, preview, commit, activate, archive, and export endpoints** with the existing mutation protections.

- [ ] **Step 3: Implement the three-step Discover/Review/Import flow** including **Import all compatible skills**, without hiding blocked findings.

- [ ] **Step 4: Run tests and commit**

Commit: `feat: add cross-agent skill library`

### Task 7: Skills checkpoint

**Files:**
- Modify: `openapi/corvus-mvp.json`
- Modify: `apps/web/src/generated/api.ts`
- Modify: `HACKATHON_STATUS.md`

- [ ] **Step 1: Regenerate contracts.**
- [ ] **Step 2: Run all skill, security, API, and web tests.**
- [ ] **Step 3: Manually preview imports from this machine's `.agents`, `.codex`, and `.claude` roots without committing them.**
- [ ] **Step 4: Commit** as `test: verify cross-agent skill imports`.
