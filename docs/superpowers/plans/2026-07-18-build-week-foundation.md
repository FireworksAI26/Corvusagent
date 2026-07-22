# Build Week Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make provider discovery, safety state, application scrolling, settings navigation, and the Windows desktop process dependable enough for every later MVP workflow.

**Architecture:** Extend the existing local FastAPI catalog and React local shell instead of creating a parallel settings system. Keep OS lifecycle behavior in Tauri, provider truth in Python, and presentation in React.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, pytest, React 19, TypeScript 5.9, Vitest, Tauri 2, Rust 2024.

## Global Constraints

- The center workspace is darker than both app and settings sidebars.
- The document never scrolls; only designated content panes scroll with contained overscroll.
- Provider discovery and safety-policy construction expose separate health states.
- Codex model controls are never blank and thinking options reflect the selected model.
- Only one desktop process runs; a second launch restores and focuses the first.
- Normal packaged execution creates no visible Windows console.
- Existing untracked `artifacts/` and `logs/` remain untouched.

---

### Task 1: Provider discovery snapshot contract

**Files:**
- Create: `corvus/mvp/provider_discovery.py`
- Modify: `corvus/mvp/provider_catalog.py`
- Modify: `corvus/mvp/api.py`
- Test: `tests/mvp/test_provider_discovery.py`
- Test: `tests/mvp/test_api.py`

**Interfaces:**
- Produces: `ProviderDiscoveryService.discover(force: bool) -> ProviderDiscoverySnapshot`
- Produces: `ProviderDiscoverySnapshot.as_catalog() -> tuple[ProviderCatalogEntry, ...]`
- Consumes: existing Codex/Claude executable discovery and `build_provider_catalog()`.

- [ ] **Step 1: Write failing discovery tests** covering independent provider failure, last-good stale snapshot, executable digest redaction, curated Codex fallback, and supported thinking levels.

```python
def test_codex_failure_does_not_erase_claude_or_last_good_catalog(tmp_path):
    clock = FakeClock()
    service = ProviderDiscoveryService(probes={"codex": SequenceProbe([ready_codex(), failed_codex()]), "claude": StaticProbe(ready_claude())}, clock=clock)
    first = service.discover(force=True)
    second = service.discover(force=True)
    assert first.provider("codex").status == "ready"
    assert second.provider("codex").status == "stale"
    assert second.provider("claude").status == "ready"
    assert second.provider("codex").executable_path is None
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `uv run pytest tests/mvp/test_provider_discovery.py tests/mvp/test_api.py -q`

Expected: failure because `ProviderDiscoveryService` and the snapshot API do not exist.

- [ ] **Step 3: Implement immutable discovery records and bounded probes**

```python
@dataclass(frozen=True, slots=True)
class ProviderDiscoveryRecord:
    provider_id: str
    status: Literal["ready", "stale", "unavailable"]
    authenticated: bool
    version: str | None
    executable_digest: str | None
    models: tuple[str, ...]
    thinking_levels: Mapping[str, tuple[ThinkingLevel, ...]]
    discovered_at: datetime
    reason_code: str | None = None
    recovery_action: str | None = None
```

The service retains only the last successful non-secret record per provider and never exposes a raw executable path.

- [ ] **Step 4: Add `GET /api/local-chat/providers?refresh=true` semantics** and stable `stale`, `reason_code`, `recovery_action`, and `last_success_at` fields without weakening existing authentication.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/mvp/test_provider_discovery.py tests/mvp/test_provider_catalog.py tests/mvp/test_api.py -q`

Expected: all focused tests pass.

Commit: `feat: make local provider discovery resilient`

### Task 2: Separate safety health from provider health

**Files:**
- Modify: `corvus/mvp/safety.py`
- Modify: `corvus/mvp/api.py`
- Modify: `apps/web/src/app/conversationApi.ts`
- Modify: `apps/web/src/app/ConversationWorkspace.tsx`
- Test: `tests/mvp/test_safety.py`
- Test: `apps/web/src/app/ConversationWorkspace.test.tsx`

**Interfaces:**
- Produces: `SafetyPolicyStatus { status, preview, reason_code, recovery_action }`.
- Consumes: `build_safety_preview(provider, mode, mcp_enabled)`.

- [ ] **Step 1: Add failing tests** proving provider unavailability renders “Provider unavailable,” a digest mismatch renders “Safety policy changed,” and only policy-construction failure renders “Safety policy unavailable.”

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/mvp/test_safety.py tests/mvp/test_api_chat.py -q && pnpm --dir apps/web test -- ConversationWorkspace.test.tsx`

- [ ] **Step 3: Return an explicit safety status envelope**

```python
class SafetyPolicyStatus(ApiModel):
    status: Literal["ready", "unavailable"]
    preview: SafetyPreviewResponse | None
    reason_code: str | None = None
    recovery_action: str | None = None
```

Do not catch a provider exception and translate it into a safety error.

- [ ] **Step 4: Update composer recovery UI** with a Retry discovery action and a Settings navigation action using distinct accessible status text.

- [ ] **Step 5: Run focused tests and commit**

Commit: `fix: distinguish provider and safety readiness`

### Task 3: Settings sidebar replacement and model controls

**Files:**
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/app/SettingsPanel.tsx`
- Modify: `apps/web/src/components/NavigationRail.tsx`
- Modify: `apps/web/src/styles/product-workspace.css`
- Test: `apps/web/src/app/SettingsPanel.test.tsx`
- Test: `apps/web/src/App.workspace.test.tsx`

**Interfaces:**
- Produces: `SettingsShell({ previousRoute, onBack, children })` behavior inside `App`.
- Consumes: the provider catalog from Tasks 1–2.

- [ ] **Step 1: Add failing UI tests** for absence of app navigation while Settings is open, Back to app restoration, non-empty model selector, and thinking options changing with provider/model.

```tsx
expect(screen.getByRole("button", { name: /back to app/i })).toBeVisible();
expect(screen.queryByRole("link", { name: "Repositories" })).not.toBeInTheDocument();
await user.click(screen.getByRole("button", { name: /back to app/i }));
expect(screen.getByRole("link", { name: "Repositories" })).toBeVisible();
```

- [ ] **Step 2: Run Vitest and verify failure**

Run: `pnpm --dir apps/web test -- SettingsPanel.test.tsx App.workspace.test.tsx`

- [ ] **Step 3: Introduce the replacement shell** with General, Models, Agent, MCP, Safety, Appearance, and Account navigation and a stored previous non-settings route.

- [ ] **Step 4: Derive model and thinking options** from the selected ready/stale provider, show disabled reasons, and normalize incompatible saved values through the existing versioned preference update.

- [ ] **Step 5: Run UI tests and commit**

Commit: `feat: add dedicated reliable settings shell`

### Task 4: Viewport containment and color hierarchy

**Files:**
- Modify: `apps/web/src/styles.css`
- Modify: `apps/web/src/styles/product-workspace.css`
- Modify: `apps/web/src/styles/adaptive-shell.css`
- Test: `apps/web/src/App.workspace.test.tsx`

**Interfaces:**
- Produces: `.application-shell`, `.application-sidebar`, `.workspace-content`, `.settings-shell`, and `.settings-content` viewport contracts.

- [ ] **Step 1: Add structure assertions** that the shell and its content panes receive the dedicated classes and no route mounts content outside the shell.

- [ ] **Step 2: Implement containment CSS**

```css
html, body, #root { width: 100%; height: 100%; margin: 0; overflow: hidden; }
.application-shell, .settings-shell { height: 100%; min-height: 0; overflow: hidden; }
.workspace-content, .settings-content { min-height: 0; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.application-sidebar, .settings-sidebar { background: var(--surface-raised); }
.workspace-content { background: var(--surface-deep); }
```

- [ ] **Step 3: Verify responsive tests and production build**

Run: `pnpm --dir apps/web test && pnpm --dir apps/web build`

- [ ] **Step 4: Commit**

Commit: `fix: contain application scrolling and restore depth`

### Task 5: Desktop single-instance and hidden-process acceptance

**Files:**
- Modify: `apps/desktop/src-tauri/src/lib.rs`
- Modify: `apps/desktop/src-tauri/src/main.rs`
- Modify: `apps/desktop/src-tauri/Cargo.toml`
- Test: Rust unit tests in `apps/desktop/src-tauri/src/lib.rs`
- Test: `tests/mvp/test_deployment_desktop.py`

**Interfaces:**
- Produces: `activate_main_window(app: &tauri::AppHandle) -> Result<(), String>`.
- Consumes: existing `tauri-plugin-single-instance` and `CREATE_NO_WINDOW` sidecar launch.

- [ ] **Step 1: Add tests** for the release GUI-subsystem attribute, sidecar creation flags, single-instance plugin registration order, and an activation callback that unminimizes, shows, and focuses `main`.

- [ ] **Step 2: Run source-level and Rust tests**

Run: `uv run pytest tests/mvp/test_deployment_desktop.py -q`

Run when allowed by Windows Application Control or CI: `cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml`

- [ ] **Step 3: Implement activation without spawning a second sidecar**

```rust
fn activate_main_window(app: &tauri::AppHandle) -> Result<(), String> {
    let window = app.get_webview_window("main").ok_or("main_window_missing")?;
    window.unminimize().map_err(|e| e.to_string())?;
    window.show().map_err(|e| e.to_string())?;
    window.set_focus().map_err(|e| e.to_string())
}
```

Register the plugin before `.setup()` and call only this function from the second-instance callback.

- [ ] **Step 4: Verify and commit**

Commit: `fix: enforce one quiet desktop instance`

### Task 6: Foundation verification checkpoint

**Files:**
- Modify: `openapi/corvus-mvp.json`
- Modify: `apps/web/src/generated/api.ts`
- Modify: `HACKATHON_STATUS.md`

- [ ] **Step 1: Regenerate OpenAPI and TypeScript**

Run: `uv run python -m corvus.mvp.openapi --output openapi/corvus-mvp.json`

Run: `pnpm --dir apps/web generate:api`

- [ ] **Step 2: Run foundation gates**

Run: `uv run ruff check corvus tests`

Run: `uv run mypy corvus`

Run: `uv run pytest tests/mvp tests/security -q`

Run: `pnpm --dir apps/web test && pnpm --dir apps/web build`

- [ ] **Step 3: Record Smart App Control evidence if local Cargo remains blocked**, while requiring the GitHub desktop workflow to pass before release.

- [ ] **Step 4: Commit generated contracts and checkpoint**

Commit: `test: verify build week foundation`
