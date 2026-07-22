import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth } from "../auth/AuthProvider";
import { AuthApiError, type PlatformApi, type SessionResponse } from "../auth/authApi";
import type { components } from "../generated/api";
import {
  emptySyncLedger,
  reduceSyncPage,
  SyncProvider,
  SyncIntegrityError,
  useWorkspaceSync
} from "./SyncProvider";

type SyncPage = components["schemas"]["SyncPage"];
type WorkspaceChange = components["schemas"]["WorkspaceChange"];

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PRINCIPAL_ID = "22222222-2222-4222-8222-222222222222";
const WORKSPACE_ID = "33333333-3333-4333-8333-333333333333";
const DEVICE_ID = "44444444-4444-4444-8444-444444444444";
const ZERO_DIGEST = "0".repeat(64);
const SESSION: SessionResponse = {
  account_id: ACCOUNT_ID,
  principal_id: PRINCIPAL_ID,
  email: "person@example.com",
  experience_kind: "developer",
  account_version: 2,
  session_version: 1,
  csrf_token: "csrf-opaque"
};
const WORKSPACE: components["schemas"]["Workspace"] = {
  id: WORKSPACE_ID,
  name: "Corvus field desk",
  workspace_kind: "individual",
  status: "active",
  created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
  version: 1
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function change(
  sequence: number,
  digestMarker: string,
  overrides: Partial<WorkspaceChange> = {}
): WorkspaceChange {
  const accountChange = sequence === 1;
  return {
    workspace_id: WORKSPACE_ID,
    workspace_version: 1,
    sequence,
    previous_digest: sequence === 1 ? ZERO_DIGEST : "1".repeat(64),
    change_digest: digestMarker.repeat(64),
    kind: accountChange ? "account_profile" : "workspace_profile",
    operation: accountChange ? "set_experience" : "update",
    entity_id: accountChange ? ACCOUNT_ID : WORKSPACE_ID,
    entity_version: accountChange ? 2 : 3,
    payload: accountChange
      ? { entity_id: ACCOUNT_ID, experience_kind: "developer", version: 2 }
      : {
          entity_id: WORKSPACE_ID,
          name: "Corvus field desk",
          workspace_kind: "individual",
          status: "active",
          version: 3
        },
    account_id: ACCOUNT_ID,
    principal_id: PRINCIPAL_ID,
    membership_version: 1,
    device_id: DEVICE_ID,
    device_version: 1,
    created_at: `2026-07-17T00:00:0${sequence}Z`,
    ...overrides
  };
}

function page(changes: WorkspaceChange[], overrides: Partial<SyncPage> = {}): SyncPage {
  const next = changes.at(-1)?.sequence ?? 0;
  return {
    requested_cursor: 0,
    next_cursor: next,
    high_watermark: next,
    earliest_retained_sequence: 1,
    changes,
    has_more: false,
    ...overrides
  };
}

function completeApi(overrides: Partial<PlatformApi> = {}): PlatformApi {
  return {
    applySync: vi.fn().mockResolvedValue({ acknowledged_cursor: 0, results: [] }),
    createWorkspace: vi.fn(),
    getSession: vi.fn().mockResolvedValue(SESSION),
    getSyncPage: vi.fn().mockResolvedValue(page([])),
    getWorkspace: vi.fn().mockResolvedValue(WORKSPACE),
    listWorkspaces: vi.fn().mockResolvedValue([WORKSPACE]),
    logout: vi.fn(),
    refreshSession: vi.fn(),
    startGoogle: vi.fn(),
    updateOnboarding: vi.fn(),
    ...overrides
  };
}

function SyncProbe() {
  const sync = useWorkspaceSync();
  const auth = useAuth();
  const [selectionOutcome, setSelectionOutcome] = useState("idle");
  return (
    <div>
      <output aria-label="sync status">{sync.status}</output>
      <output aria-label="selected workspace">{sync.selectedWorkspace?.name ?? "none"}</output>
      <output aria-label="sync cursor">{sync.cursor}</output>
      <output aria-label="mutation authority">{sync.canMutate ? "enabled" : "blocked"}</output>
      <output aria-label="conflict versions">
        {sync.conflict == null
          ? "none"
          : `${sync.conflict.submittedExpectedVersion}:${sync.conflict.currentVersion}`}
      </output>
      <output aria-label="selection outcome">{selectionOutcome}</output>
      <output aria-label="authorized workspaces">{sync.workspaces.map((workspace) => workspace.name).join(",")}</output>
      <output aria-label="provider auth status">{auth.status}</output>
      <button
        onClick={() => {
          setSelectionOutcome("pending");
          void sync.selectWorkspace(WORKSPACE_ID).then(
            () => setSelectionOutcome("selected"),
            () => setSelectionOutcome("failed")
          );
        }}
        type="button"
      >Select field desk</button>
      <button
        onClick={() => void sync.selectWorkspace(DEVICE_ID).then(
          () => setSelectionOutcome("selected-second"),
          () => setSelectionOutcome("failed-second")
        )}
        type="button"
      >Select second workspace</button>
      <button onClick={() => void sync.refresh()} type="button">Refresh workspace</button>
      <button onClick={() => void sync.updateWorkspaceProfile("Renamed field desk")} type="button">Rename workspace</button>
      <button onClick={() => void sync.saveExperience("everyday", 2).catch(() => undefined)} type="button">Save experience</button>
      <button onClick={() => void sync.reloadConflict().catch(() => undefined)} type="button">Reload conflict</button>
      <button onClick={() => void sync.retryConflict().catch(() => undefined)} type="button">Retry conflict</button>
    </div>
  );
}

function renderSync(api: PlatformApi) {
  return render(
    <AuthProvider api={api}>
      <SyncProvider api={api}>
        <SyncProbe />
      </SyncProvider>
    </AuthProvider>
  );
}

describe("ordered workspace synchronization", () => {
  it("requires onboarding before selecting an existing workspace when experience is unset", async () => {
    const getWorkspace = vi.fn().mockResolvedValue(WORKSPACE);
    renderSync(completeApi({
      getSession: vi.fn().mockResolvedValue({ ...SESSION, experience_kind: null }),
      getWorkspace
    }));

    await waitFor(() => {
      expect(screen.getByLabelText("sync status")).toHaveTextContent("onboarding_required");
    });
    expect(getWorkspace).not.toHaveBeenCalled();
  });

  it("reduces contiguous typed changes and advances the cursor monotonically", () => {
    const first = change(1, "1");
    const second = change(2, "2");

    const reduced = reduceSyncPage(
      emptySyncLedger(),
      page([first, second]),
      { accountId: ACCOUNT_ID, principalId: PRINCIPAL_ID, workspaceId: WORKSPACE_ID }
    );

    expect(reduced.cursor).toBe(2);
    expect(reduced.highWatermark).toBe(2);
    expect(reduced.accountProfile).toEqual(first.payload);
    expect(reduced.workspaceProfile).toEqual(second.payload);
    expect(reduced.digests.get(2)).toBe("2".repeat(64));
  });

  it("ignores an exact already-applied sequence and digest", () => {
    const first = change(1, "1");
    const initial = reduceSyncPage(
      emptySyncLedger(),
      page([first]),
      { accountId: ACCOUNT_ID, principalId: PRINCIPAL_ID, workspaceId: WORKSPACE_ID }
    );

    const duplicate = reduceSyncPage(
      initial,
      page([first], { requested_cursor: 1, next_cursor: 1, high_watermark: 1 }),
      { accountId: ACCOUNT_ID, principalId: PRINCIPAL_ID, workspaceId: WORKSPACE_ID }
    );

    expect(duplicate).toEqual(initial);
  });

  it.each([
    ["sync_gap_detected", [change(2, "2")]],
    ["sync_provenance_invalid", [change(1, "1", { principal_id: DEVICE_ID })]]
  ])("rejects %s without advancing last-good state", (code, changes) => {
    const initial = emptySyncLedger();
    expect(() =>
      reduceSyncPage(initial, page(changes), {
        accountId: ACCOUNT_ID,
        principalId: PRINCIPAL_ID,
        workspaceId: WORKSPACE_ID
      })
    ).toThrow(new SyncIntegrityError(code));
    expect(initial.cursor).toBe(0);
  });

  it("rejects a changed digest for an already-applied sequence", () => {
    const initial = reduceSyncPage(emptySyncLedger(), page([change(1, "1")]), {
      accountId: ACCOUNT_ID,
      principalId: PRINCIPAL_ID,
      workspaceId: WORKSPACE_ID
    });

    expect(() =>
      reduceSyncPage(
        initial,
        page([change(1, "9")], { requested_cursor: 1, next_cursor: 1, high_watermark: 1 }),
        { accountId: ACCOUNT_ID, principalId: PRINCIPAL_ID, workspaceId: WORKSPACE_ID }
      )
    ).toThrow(new SyncIntegrityError("sync_digest_changed"));
  });

  it("rejects a changed previous digest before acknowledging the page", () => {
    const first = change(1, "1");
    const invalidSecond = change(2, "2", { previous_digest: "8".repeat(64) });

    expect(() =>
      reduceSyncPage(emptySyncLedger(), page([first, invalidSecond]), {
        accountId: ACCOUNT_ID,
        principalId: PRINCIPAL_ID,
        workspaceId: WORKSPACE_ID
      })
    ).toThrow(new SyncIntegrityError("sync_digest_chain_invalid"));
  });
});

describe("SyncProvider", () => {
  it("auto-selects exactly one authorized workspace and acknowledges only reduced changes", async () => {
    const changes = [change(1, "1"), change(2, "2")];
    const applySync = vi.fn().mockResolvedValue({ acknowledged_cursor: 2, results: [] });
    const api = completeApi({
      applySync,
      getSyncPage: vi.fn().mockResolvedValue(page(changes))
    });

    renderSync(api);

    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Corvus field desk");
    expect(screen.getByLabelText("sync cursor")).toHaveTextContent("2");
    expect(applySync).toHaveBeenCalledWith(
      WORKSPACE_ID,
      { acknowledged_cursor: 2, mutations: [] },
      "csrf-opaque"
    );
  });

  it("requires explicit selection when multiple workspaces are authorized", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    const getWorkspace = vi.fn().mockResolvedValue(WORKSPACE);
    const api = completeApi({
      getWorkspace,
      listWorkspaces: vi.fn().mockResolvedValue([WORKSPACE, second])
    });
    renderSync(api);

    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("selection_required"));
    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("none");
    expect(getWorkspace).not.toHaveBeenCalled();

    await userEvent.setup().click(screen.getByRole("button", { name: "Select field desk" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    expect(getWorkspace).toHaveBeenCalledWith(WORKSPACE_ID);
  });

  it("rejects a failed explicit selection so the shell cannot announce success", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    renderSync(completeApi({
      getWorkspace: vi.fn().mockRejectedValue(new AuthApiError(403, "membership_forbidden")),
      listWorkspaces: vi.fn().mockResolvedValue([WORKSPACE, second])
    }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("selection_required"));

    await userEvent.setup().click(screen.getByRole("button", { name: "Select field desk" }));

    await waitFor(() => expect(screen.getByLabelText("selection outcome")).toHaveTextContent("failed"));
    expect(screen.getByLabelText("sync status")).toHaveTextContent("forbidden");
    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("none");
  });

  it("rejects stale A after a 403 refresh authorizes only B, then permits explicit B", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    const getSyncPage = vi.fn().mockResolvedValueOnce(page([])).mockRejectedValueOnce(
      new AuthApiError(403, "membership_forbidden")
    ).mockResolvedValue(page([]));
    const getWorkspace = vi.fn().mockImplementation(async (id: string) => id === DEVICE_ID ? second : WORKSPACE);
    const listWorkspaces = vi.fn().mockResolvedValueOnce([WORKSPACE]).mockResolvedValueOnce([second]);
    renderSync(completeApi({ getSyncPage, getWorkspace, listWorkspaces }));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    await user.click(screen.getByRole("button", { name: "Refresh workspace" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("forbidden"));

    await user.click(screen.getByRole("button", { name: "Select field desk" }));
    await waitFor(() => expect(screen.getByLabelText("selection outcome")).toHaveTextContent("failed"));
    expect(screen.getByLabelText("authorized workspaces")).toHaveTextContent("Second workspace");
    await user.click(screen.getByRole("button", { name: "Select second workspace" }));

    await waitFor(() => expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace"));
  });

  it("ignores a late A selection completion after B has completed", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    const first = deferred<typeof WORKSPACE>();
    const next = deferred<typeof WORKSPACE>();
    const getWorkspace = vi.fn().mockImplementation((id: string) => id === WORKSPACE_ID ? first.promise : next.promise);
    renderSync(completeApi({ getWorkspace, listWorkspaces: vi.fn().mockResolvedValue([WORKSPACE, second]) }));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("selection_required"));
    await user.click(screen.getByRole("button", { name: "Select field desk" }));
    await user.click(screen.getByRole("button", { name: "Select second workspace" }));
    await act(async () => { next.resolve(second); });
    await waitFor(() => expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace"));
    await act(async () => { first.resolve(WORKSPACE); });

    await waitFor(() => expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace"));
  });

  it("propagates onboarding mutation 401 to the central signed-out boundary", async () => {
    renderSync(completeApi({ updateOnboarding: vi.fn().mockRejectedValue(new AuthApiError(401, "session_invalid")) }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));

    await userEvent.setup().click(screen.getByRole("button", { name: "Save experience" }));

    await waitFor(() => expect(screen.getByLabelText("provider auth status")).toHaveTextContent("unauthenticated"));
  });

  it("refreshes authorized workspaces and preserves last-safe display on mutation 403", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    const listWorkspaces = vi.fn().mockResolvedValueOnce([WORKSPACE]).mockResolvedValueOnce([second]);
    renderSync(completeApi({
      applySync: vi.fn().mockRejectedValue(new AuthApiError(403, "membership_forbidden")),
      listWorkspaces
    }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));

    await userEvent.setup().click(screen.getByRole("button", { name: "Rename workspace" }));

    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("forbidden"));
    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Corvus field desk");
    expect(screen.getByLabelText("authorized workspaces")).toHaveTextContent("Second workspace");
    expect(screen.getByLabelText("mutation authority")).toHaveTextContent("blocked");
  });

  it("preserves last-good display while offline without claiming mutation authority", async () => {
    const getSyncPage = vi
      .fn()
      .mockResolvedValueOnce(page([]))
      .mockRejectedValueOnce(new AuthApiError(0, "network_unavailable"));
    renderSync(completeApi({ getSyncPage }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));

    await userEvent.setup().click(screen.getByRole("button", { name: "Refresh workspace" }));

    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("offline"));
    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Corvus field desk");
    expect(screen.getByLabelText("mutation authority")).toHaveTextContent("blocked");
  });

  it("invalidates mutation authority on 403 and requires explicit selection from a fresh list", async () => {
    const getSyncPage = vi
      .fn()
      .mockResolvedValueOnce(page([]))
      .mockRejectedValueOnce(new AuthApiError(403, "membership_forbidden"))
      .mockResolvedValueOnce(page([]));
    const listWorkspaces = vi.fn().mockResolvedValue([WORKSPACE]);
    renderSync(completeApi({ getSyncPage, listWorkspaces }));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));

    await user.click(screen.getByRole("button", { name: "Refresh workspace" }));

    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("forbidden"));
    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Corvus field desk");
    expect(screen.getByLabelText("mutation authority")).toHaveTextContent("blocked");
    expect(listWorkspaces).toHaveBeenCalledTimes(2);

    await user.click(screen.getByRole("button", { name: "Select field desk" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
  });

  it("shows both submitted and current versions for an explicit sync conflict", async () => {
    const applySync = vi.fn().mockRejectedValue(
      new AuthApiError(409, "sync_version_conflict", "conflict-correlation", {
        code: "sync_version_conflict",
        mutation_index: 0,
        submitted_expected_version: 1,
        current_version: 3,
        current_profile: {
          entity_id: WORKSPACE_ID,
          name: "Current server name",
          workspace_kind: "individual",
          status: "active",
          version: 3
        }
      })
    );
    renderSync(completeApi({ applySync }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));

    await userEvent.setup().click(screen.getByRole("button", { name: "Rename workspace" }));

    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("conflict"));
    expect(screen.getByLabelText("conflict versions")).toHaveTextContent("1:3");
    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Corvus field desk");
    expect(screen.getByLabelText("mutation authority")).toHaveTextContent("blocked");
  });

  it("retries an acknowledged conflict only after the user chooses the current version", async () => {
    const applySync = vi.fn()
      .mockRejectedValueOnce(new AuthApiError(409, "sync_version_conflict", "conflict-correlation", {
        code: "sync_version_conflict",
        mutation_index: 0,
        submitted_expected_version: 1,
        current_version: 3,
        current_profile: {
          entity_id: WORKSPACE_ID,
          name: "Current server name",
          workspace_kind: "individual",
          status: "active",
          version: 3
        }
      }))
      .mockResolvedValue({ acknowledged_cursor: 0, results: [] });
    renderSync(completeApi({ applySync }));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    await user.click(screen.getByRole("button", { name: "Rename workspace" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("conflict"));

    await user.click(screen.getByRole("button", { name: "Retry conflict" }));

    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    expect(applySync).toHaveBeenLastCalledWith(
      WORKSPACE_ID,
      expect.objectContaining({
        mutations: [expect.objectContaining({ expected_version: 3, payload: { name: "Renamed field desk" } })]
      }),
      "csrf-opaque"
    );
  });

  it("ignores a late workspace A profile update after workspace B is selected", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    const mutation = deferred<Awaited<ReturnType<PlatformApi["applySync"]>>>();
    const applySync = vi.fn().mockReturnValue(mutation.promise);
    const getWorkspace = vi.fn().mockImplementation(async (id: string) => id === DEVICE_ID ? second : WORKSPACE);
    renderSync(completeApi({ applySync, getWorkspace, listWorkspaces: vi.fn().mockResolvedValue([WORKSPACE, second]) }));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("selection_required"));
    await user.click(screen.getByRole("button", { name: "Select field desk" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    await user.click(screen.getByRole("button", { name: "Rename workspace" }));
    await user.click(screen.getByRole("button", { name: "Select second workspace" }));
    await waitFor(() => expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace"));

    await act(async () => { mutation.resolve({ acknowledged_cursor: 0, results: [] }); });

    await waitFor(() => expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace"));
    expect(screen.getByLabelText("sync status")).toHaveTextContent("ready");
  });

  it("ignores a late workspace A mutation 403 after workspace B is selected", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    const mutation = deferred<Awaited<ReturnType<PlatformApi["applySync"]>>>();
    const listWorkspaces = vi.fn().mockResolvedValue([WORKSPACE, second]);
    renderSync(completeApi({
      applySync: vi.fn().mockReturnValue(mutation.promise),
      getWorkspace: vi.fn().mockImplementation(async (id: string) => id === DEVICE_ID ? second : WORKSPACE),
      listWorkspaces
    }));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("selection_required"));
    await user.click(screen.getByRole("button", { name: "Select field desk" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    await user.click(screen.getByRole("button", { name: "Rename workspace" }));
    await user.click(screen.getByRole("button", { name: "Select second workspace" }));
    await waitFor(() => expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace"));

    await act(async () => { mutation.reject(new AuthApiError(403, "membership_forbidden")); });

    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace");
    expect(screen.getByLabelText("sync status")).toHaveTextContent("ready");
    expect(listWorkspaces).toHaveBeenCalledTimes(1);
  });

  it("ignores a late workspace A conflict retry after workspace B is selected", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    const retry = deferred<Awaited<ReturnType<PlatformApi["applySync"]>>>();
    const applySync = vi.fn()
      .mockRejectedValueOnce(new AuthApiError(409, "sync_version_conflict", "conflict-correlation", {
        submitted_expected_version: 1,
        current_version: 3,
        current_profile: { ...WORKSPACE, entity_id: WORKSPACE_ID, version: 3 }
      }))
      .mockReturnValueOnce(retry.promise);
    renderSync(completeApi({
      applySync,
      getWorkspace: vi.fn().mockImplementation(async (id: string) => id === DEVICE_ID ? second : WORKSPACE),
      listWorkspaces: vi.fn().mockResolvedValue([WORKSPACE, second])
    }));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("selection_required"));
    await user.click(screen.getByRole("button", { name: "Select field desk" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    await user.click(screen.getByRole("button", { name: "Rename workspace" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("conflict"));
    await user.click(screen.getByRole("button", { name: "Retry conflict" }));
    await user.click(screen.getByRole("button", { name: "Select second workspace" }));
    await waitFor(() => expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace"));

    await act(async () => { retry.resolve({ acknowledged_cursor: 0, results: [] }); });

    await waitFor(() => expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace"));
    expect(screen.getByLabelText("sync status")).toHaveTextContent("ready");
  });

  it("ignores a late workspace A conflict reload after workspace B is selected", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    const reload = deferred<typeof WORKSPACE>();
    let firstWorkspaceLoads = 0;
    const getWorkspace = vi.fn().mockImplementation((id: string) => {
      if (id === DEVICE_ID) return Promise.resolve(second);
      firstWorkspaceLoads += 1;
      return firstWorkspaceLoads === 1 ? Promise.resolve(WORKSPACE) : reload.promise;
    });
    const applySync = vi.fn().mockRejectedValue(new AuthApiError(409, "sync_version_conflict", "conflict-correlation", {
      submitted_expected_version: 1,
      current_version: 3,
      current_profile: { ...WORKSPACE, entity_id: WORKSPACE_ID, version: 3 }
    }));
    renderSync(completeApi({
      applySync,
      getWorkspace,
      listWorkspaces: vi.fn().mockResolvedValue([WORKSPACE, second])
    }));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("selection_required"));
    await user.click(screen.getByRole("button", { name: "Select field desk" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    await user.click(screen.getByRole("button", { name: "Rename workspace" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("conflict"));
    await user.click(screen.getByRole("button", { name: "Reload conflict" }));
    await user.click(screen.getByRole("button", { name: "Select second workspace" }));
    await waitFor(() => expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace"));

    await act(async () => { reload.resolve(WORKSPACE); });

    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace");
    expect(screen.getByLabelText("sync status")).toHaveTextContent("ready");
  });

  it("routes retry-conflict 403 through fresh membership and explicit re-selection", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    const applySync = vi.fn()
      .mockRejectedValueOnce(new AuthApiError(409, "sync_version_conflict", "conflict-correlation", {
        submitted_expected_version: 1,
        current_version: 3,
        current_profile: { ...WORKSPACE, entity_id: WORKSPACE_ID, version: 3 }
      }))
      .mockRejectedValueOnce(new AuthApiError(403, "membership_forbidden"));
    const listWorkspaces = vi.fn().mockResolvedValueOnce([WORKSPACE]).mockResolvedValueOnce([second]);
    renderSync(completeApi({ applySync, listWorkspaces }));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    await user.click(screen.getByRole("button", { name: "Rename workspace" }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("conflict"));

    await user.click(screen.getByRole("button", { name: "Retry conflict" }));

    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("forbidden"));
    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Corvus field desk");
    expect(screen.getByLabelText("authorized workspaces")).toHaveTextContent("Second workspace");
    expect(screen.getByLabelText("mutation authority")).toHaveTextContent("blocked");
  });

  it("does not acknowledge an explicit resync page after workspace B supersedes workspace A", async () => {
    const second = { ...WORKSPACE, id: DEVICE_ID, name: "Second workspace" };
    const staleBoundary = deferred<SyncPage>();
    const applySync = vi.fn().mockResolvedValue({ acknowledged_cursor: 2, results: [] });
    const getWorkspace = vi.fn().mockImplementation(
      async (workspaceId: string) => workspaceId === DEVICE_ID ? second : WORKSPACE
    );
    const getSyncPage = vi.fn().mockImplementation(
      async (workspaceId: string, cursor: number) => {
        if (workspaceId === DEVICE_ID) return page([]);
        if (cursor === 1) return staleBoundary.promise;
        throw new AuthApiError(409, "sync_resync_required", "resync-correlation", {
          code: "sync_resync_required",
          earliest_available: 2,
          latest_sequence: 2,
          resume_cursor: 1,
          resources: ["/api/v2/session", `/api/v2/workspaces/${WORKSPACE_ID}`]
        });
      }
    );
    renderSync(completeApi({
      applySync,
      getSyncPage,
      getWorkspace,
      listWorkspaces: vi.fn().mockResolvedValue([WORKSPACE, second])
    }));
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("selection_required"));

    await user.click(screen.getByRole("button", { name: "Select field desk" }));
    await waitFor(() => expect(getSyncPage).toHaveBeenCalledWith(WORKSPACE_ID, 1));
    await user.click(screen.getByRole("button", { name: "Select second workspace" }));
    await waitFor(() => expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace"));

    await act(async () => {
      staleBoundary.resolve(page([
        change(2, "2", { previous_digest: ZERO_DIGEST })
      ], {
        requested_cursor: 1,
        next_cursor: 2,
        high_watermark: 2,
        earliest_retained_sequence: 2
      }));
    });

    expect(applySync).not.toHaveBeenCalled();
    expect(screen.getByLabelText("selected workspace")).toHaveTextContent("Second workspace");
    expect(screen.getByLabelText("sync status")).toHaveTextContent("ready");
    expect(screen.getByLabelText("sync cursor")).toHaveTextContent("0");
    expect(screen.getByLabelText("mutation authority")).toHaveTextContent("enabled");
  });

  it("performs explicit resync by refetching session and workspace before the boundary", async () => {
    const boundary = page([], {
      requested_cursor: 1,
      next_cursor: 1,
      high_watermark: 1,
      earliest_retained_sequence: 2
    });
    const getSyncPage = vi
      .fn()
      .mockResolvedValueOnce(page([]))
      .mockRejectedValueOnce(
        new AuthApiError(409, "sync_resync_required", "resync-correlation", {
          code: "sync_resync_required",
          earliest_available: 2,
          latest_sequence: 1,
          resume_cursor: 1,
          resources: ["/api/v2/session", `/api/v2/workspaces/${WORKSPACE_ID}`]
        })
      )
      .mockResolvedValueOnce(boundary);
    const getSession = vi.fn().mockResolvedValue(SESSION);
    const getWorkspace = vi.fn().mockResolvedValue(WORKSPACE);
    renderSync(completeApi({ getSession, getSyncPage, getWorkspace }));
    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));

    await userEvent.setup().click(screen.getByRole("button", { name: "Refresh workspace" }));

    await waitFor(() => expect(screen.getByLabelText("sync status")).toHaveTextContent("ready"));
    expect(getSession).toHaveBeenCalledTimes(2);
    expect(getWorkspace).toHaveBeenCalledTimes(3);
    expect(getSyncPage).toHaveBeenLastCalledWith(WORKSPACE_ID, 1);
    expect(screen.getByLabelText("sync cursor")).toHaveTextContent("1");
  });
});
