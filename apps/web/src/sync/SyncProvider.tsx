import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";

import { useAuth } from "../auth/AuthProvider";
import {
  AuthApiError,
  createAuthApi,
  type OnboardingResponse,
  type PlatformApi,
  type Workspace,
  type WorkspaceCreate
} from "../auth/authApi";
import type { components } from "../generated/api";

type AccountProfile = components["schemas"]["AccountProfile"];
type SyncPage = components["schemas"]["SyncPage"];
type WorkspaceProfile = components["schemas"]["WorkspaceProfile"];

const ROOT_DIGEST = "0".repeat(64);

export interface SyncLedger {
  accountProfile: AccountProfile | null;
  cursor: number;
  digests: ReadonlyMap<number, string>;
  highWatermark: number;
  lastDigest: string;
  workspaceProfile: WorkspaceProfile | null;
}

export interface SyncProvenance {
  accountId: string;
  principalId: string;
  workspaceId: string;
}

export class SyncIntegrityError extends Error {
  readonly code: string;

  constructor(code: string) {
    super(code);
    this.name = "SyncIntegrityError";
    this.code = code;
  }
}

export function emptySyncLedger(): SyncLedger {
  return {
    accountProfile: null,
    cursor: 0,
    digests: new Map(),
    highWatermark: 0,
    lastDigest: ROOT_DIGEST,
    workspaceProfile: null
  };
}

function accountProfile(payload: AccountProfile | WorkspaceProfile): AccountProfile {
  if (!("experience_kind" in payload)) throw new SyncIntegrityError("sync_payload_invalid");
  return payload;
}

function workspaceProfile(payload: AccountProfile | WorkspaceProfile): WorkspaceProfile {
  if (!("workspace_kind" in payload)) throw new SyncIntegrityError("sync_payload_invalid");
  return payload;
}

export function reduceSyncPage(
  ledger: SyncLedger,
  page: SyncPage,
  provenance: SyncProvenance
): SyncLedger {
  if (page.requested_cursor !== ledger.cursor) {
    throw new SyncIntegrityError("sync_cursor_changed");
  }
  if (page.high_watermark < ledger.highWatermark || page.next_cursor < ledger.cursor) {
    throw new SyncIntegrityError("sync_cursor_rewind");
  }

  let cursor = ledger.cursor;
  let lastDigest = ledger.lastDigest;
  let currentAccount = ledger.accountProfile;
  let currentWorkspace = ledger.workspaceProfile;
  const digests = new Map(ledger.digests);
  let changed = false;

  for (const change of page.changes) {
    if (
      change.account_id !== provenance.accountId ||
      change.principal_id !== provenance.principalId ||
      change.workspace_id !== provenance.workspaceId
    ) {
      throw new SyncIntegrityError("sync_provenance_invalid");
    }

    if (change.sequence <= cursor) {
      if (digests.get(change.sequence) !== change.change_digest) {
        throw new SyncIntegrityError("sync_digest_changed");
      }
      continue;
    }
    if (change.sequence !== cursor + 1) throw new SyncIntegrityError("sync_gap_detected");
    if (cursor > 0 && digests.size === 0 && lastDigest === ROOT_DIGEST) {
      lastDigest = change.previous_digest;
    }
    if (change.previous_digest !== lastDigest) {
      throw new SyncIntegrityError("sync_digest_chain_invalid");
    }

    if (change.kind === "account_profile" && change.operation === "set_experience") {
      currentAccount = accountProfile(change.payload);
    } else if (change.kind === "workspace_profile" && change.operation === "update") {
      currentWorkspace = workspaceProfile(change.payload);
    } else {
      throw new SyncIntegrityError("sync_command_unknown");
    }
    cursor = change.sequence;
    lastDigest = change.change_digest;
    digests.set(change.sequence, change.change_digest);
    changed = true;
  }

  if (page.next_cursor !== cursor || page.high_watermark < cursor) {
    throw new SyncIntegrityError("sync_cursor_invalid");
  }
  if (!changed && page.high_watermark === ledger.highWatermark) return ledger;

  return {
    accountProfile: currentAccount,
    cursor,
    digests,
    highWatermark: page.high_watermark,
    lastDigest,
    workspaceProfile: currentWorkspace
  };
}

export type WorkspaceSyncStatus =
  | "idle"
  | "loading"
  | "onboarding_required"
  | "selection_required"
  | "ready"
  | "offline"
  | "forbidden"
  | "conflict"
  | "resyncing"
  | "error";

export interface WorkspaceSyncState {
  accountProfile: AccountProfile | null;
  canMutate: boolean;
  conflict: SyncConflictView | null;
  createWorkspace(body: WorkspaceCreate, idempotencyKey: string): Promise<Workspace>;
  cursor: number;
  error: AuthApiError | SyncIntegrityError | null;
  refresh(): Promise<void>;
  reloadConflict(): Promise<void>;
  retryConflict(): Promise<void>;
  saveExperience(
    experienceKind: components["schemas"]["ExperienceKind"],
    expectedVersion: number
  ): Promise<OnboardingResponse>;
  selectedWorkspace: Workspace | null;
  selectWorkspace(workspaceId: string): Promise<void>;
  status: WorkspaceSyncStatus;
  updateWorkspaceProfile(name: string): Promise<void>;
  workspaceProfile: WorkspaceProfile | null;
  workspaces: readonly Workspace[];
}

export interface SyncConflictView {
  currentProfile: Readonly<Record<string, unknown>>;
  currentVersion: number;
  submittedExpectedVersion: number;
}

interface SyncProviderProps {
  api?: PlatformApi;
  children: ReactNode;
}

interface SyncSnapshot {
  canMutate: boolean;
  conflict: SyncConflictView | null;
  error: AuthApiError | SyncIntegrityError | null;
  ledger: SyncLedger;
  selectedWorkspace: Workspace | null;
  status: WorkspaceSyncStatus;
  workspaces: readonly Workspace[];
}

const browserPlatformApi = createAuthApi();
const WorkspaceSyncContext = createContext<WorkspaceSyncState | null>(null);

function syncError(reason: unknown): AuthApiError | SyncIntegrityError {
  if (reason instanceof AuthApiError || reason instanceof SyncIntegrityError) return reason;
  return new AuthApiError(0, "network_unavailable");
}

export function SyncProvider({ api = browserPlatformApi, children }: SyncProviderProps) {
  const auth = useAuth();
  const operationRef = useRef(0);
  const pendingConflictNameRef = useRef<string | null>(null);
  const [snapshot, setSnapshot] = useState<SyncSnapshot>({
    canMutate: false,
    conflict: null,
    error: null,
    ledger: emptySyncLedger(),
    selectedWorkspace: null,
    status: "idle",
    workspaces: []
  });

  const refreshForbiddenMembership = useCallback(
    async (error: AuthApiError, operation: number) => {
      try {
        const workspaces = await api.listWorkspaces();
        if (operation !== operationRef.current) return false;
        setSnapshot((current) => ({
          ...current,
          canMutate: false,
          conflict: null,
          error,
          status: "forbidden",
          workspaces
        }));
      } catch (listReason) {
        if (operation !== operationRef.current) return false;
        setSnapshot((current) => ({
          ...current,
          canMutate: false,
          conflict: null,
          error: syncError(listReason),
          status: "forbidden",
          workspaces: []
        }));
      }
      return true;
    },
    [api]
  );

  const resyncWorkspace = useCallback(
    async (candidate: Workspace, error: AuthApiError, operation: number) => {
      const resumeCursor = error.detail.resume_cursor;
      const resources = error.detail.resources;
      const expectedWorkspaceResource = `/api/v2/workspaces/${candidate.id}`;
      if (
        typeof resumeCursor !== "number" ||
        !Number.isSafeInteger(resumeCursor) ||
        resumeCursor < 0 ||
        !Array.isArray(resources) ||
        !resources.includes("/api/v2/session") ||
        !resources.includes(expectedWorkspaceResource)
      ) {
        throw new SyncIntegrityError("sync_resync_boundary_invalid");
      }

      if (operation !== operationRef.current) return false;
      setSnapshot((current) => ({
        ...current,
        canMutate: false,
        conflict: null,
        error,
        status: "resyncing"
      }));
      const freshSession = await api.getSession();
      const confirmed = await api.getWorkspace(candidate.id);
      if (operation !== operationRef.current) return false;
      const boundaryLedger: SyncLedger = {
        ...emptySyncLedger(),
        cursor: resumeCursor,
        highWatermark: resumeCursor
      };
      const page = await api.getSyncPage(confirmed.id, resumeCursor);
      if (operation !== operationRef.current) return false;
      const reduced = reduceSyncPage(boundaryLedger, page, {
        accountId: freshSession.account_id,
        principalId: freshSession.principal_id,
        workspaceId: confirmed.id
      });
      if (operation !== operationRef.current) return false;
      if (reduced.cursor > resumeCursor) {
        if (operation !== operationRef.current) return false;
        await api.applySync(
          confirmed.id,
          { acknowledged_cursor: reduced.cursor, mutations: [] },
          freshSession.csrf_token
        );
      }
      if (operation !== operationRef.current) return false;
      setSnapshot((current) => ({
        ...current,
        canMutate: true,
        conflict: null,
        error: null,
        ledger: reduced,
        selectedWorkspace: confirmed,
        status: "ready"
      }));
      return true;
    },
    [api]
  );

  const loadWorkspace = useCallback(
    async (
      candidate: Workspace,
      ledger: SyncLedger,
      operation = ++operationRef.current
    ) => {
      if (auth.session === null) return false;
      if (operation !== operationRef.current) return false;
      setSnapshot((current) => ({ ...current, canMutate: false, error: null, status: "loading" }));
      try {
        const confirmed = await api.getWorkspace(candidate.id);
        if (operation !== operationRef.current) return false;
        let reduced = ledger;
        let page = await api.getSyncPage(confirmed.id, reduced.cursor);
        while (true) {
          const previousCursor = reduced.cursor;
          reduced = reduceSyncPage(reduced, page, {
            accountId: auth.session.account_id,
            principalId: auth.session.principal_id,
            workspaceId: confirmed.id
          });
          if (operation !== operationRef.current) return false;
          if (reduced.cursor > previousCursor) {
            await api.applySync(
              confirmed.id,
              { acknowledged_cursor: reduced.cursor, mutations: [] },
              auth.session.csrf_token
            );
          }
          if (!page.has_more) break;
          page = await api.getSyncPage(confirmed.id, reduced.cursor);
        }
        if (operation !== operationRef.current) return false;
        setSnapshot((current) => ({
          ...current,
          canMutate: true,
          conflict: null,
          error: null,
          ledger: reduced,
          selectedWorkspace: confirmed,
          status: "ready"
        }));
      } catch (reason) {
        const error = syncError(reason);
        if (
          error instanceof AuthApiError &&
          error.status === 409 &&
          error.code === "sync_resync_required"
        ) {
          try {
            return await resyncWorkspace(candidate, error, operation);
          } catch (resyncReason) {
            if (operation !== operationRef.current) return false;
            setSnapshot((current) => ({
              ...current,
              canMutate: false,
              error: syncError(resyncReason),
              status: "error"
            }));
            return false;
          }
        }
        if (error instanceof AuthApiError && error.status === 401) {
          if (operation !== operationRef.current) return false;
          setSnapshot((current) => ({
            ...current,
            canMutate: false,
            error: null,
            selectedWorkspace: null,
            status: "idle"
          }));
          auth.invalidateAuthority();
          return false;
        }
        if (error instanceof AuthApiError && error.status === 403) {
          await refreshForbiddenMembership(error, operation);
          return false;
        }
        if (operation !== operationRef.current) return false;
        setSnapshot((current) => ({
          ...current,
          canMutate: false,
          error,
          status:
            error instanceof AuthApiError && (error.status === 0 || error.status === 503)
              ? "offline"
              : "error"
        }));
        return false;
      }
      return true;
    },
    [api, auth, refreshForbiddenMembership, resyncWorkspace]
  );

  const discoverWorkspaces = useCallback(async () => {
    if (auth.status !== "authenticated" || auth.session === null) return;
    const operation = ++operationRef.current;
    setSnapshot((current) => ({ ...current, canMutate: false, error: null, status: "loading" }));
    try {
      const workspaces = await api.listWorkspaces();
      if (operation !== operationRef.current) return;
      setSnapshot((current) => ({ ...current, workspaces }));
      if (auth.session.experience_kind === null || workspaces.length === 0) {
        setSnapshot((current) => ({ ...current, status: "onboarding_required" }));
      } else if (workspaces.length === 1) {
        await loadWorkspace(workspaces[0], emptySyncLedger(), operation);
      } else {
        setSnapshot((current) => ({
          ...current,
          canMutate: false,
          selectedWorkspace: null,
          status: "selection_required"
        }));
      }
    } catch (reason) {
      if (operation !== operationRef.current) return;
      const error = syncError(reason);
      if (error instanceof AuthApiError && error.status === 401) {
        auth.invalidateAuthority();
        return;
      }
      setSnapshot((current) => ({
        ...current,
        canMutate: false,
        conflict: null,
        error,
        status:
          error instanceof AuthApiError && (error.status === 0 || error.status === 503)
            ? "offline"
            : "error"
      }));
    }
  }, [api, auth, loadWorkspace]);

  useEffect(() => {
    if (auth.status === "authenticated") void discoverWorkspaces();
    else if (auth.status === "unauthenticated") {
      operationRef.current += 1;
      setSnapshot({
        canMutate: false,
        conflict: null,
        error: null,
        ledger: emptySyncLedger(),
        selectedWorkspace: null,
        status: "idle",
        workspaces: []
      });
    }
    return () => {
      operationRef.current += 1;
    };
  }, [auth.status, discoverWorkspaces]);

  const selectWorkspace = useCallback(
    async (workspaceId: string) => {
      pendingConflictNameRef.current = null;
      const candidate = snapshot.workspaces.find((workspace) => workspace.id === workspaceId);
      if (candidate === undefined) {
        operationRef.current += 1;
        setSnapshot((current) => ({
          ...current,
          canMutate: false,
          error: new AuthApiError(403, "workspace_selection_forbidden"),
          status: "forbidden"
        }));
        throw new AuthApiError(403, "workspace_selection_forbidden");
      }
      const selected = await loadWorkspace(candidate, emptySyncLedger());
      if (!selected) throw new AuthApiError(0, "workspace_selection_failed");
    },
    [loadWorkspace, snapshot.workspaces]
  );

  const refresh = useCallback(async () => {
    if (snapshot.selectedWorkspace === null) {
      await discoverWorkspaces();
      return;
    }
    await loadWorkspace(snapshot.selectedWorkspace, snapshot.ledger);
  }, [discoverWorkspaces, loadWorkspace, snapshot.ledger, snapshot.selectedWorkspace]);

  const saveExperience = useCallback(
    async (
      experienceKind: components["schemas"]["ExperienceKind"],
      expectedVersion: number
    ) => {
      if (auth.session === null) throw new AuthApiError(401, "session_required");
      try {
        return await api.updateOnboarding(
          { experience_kind: experienceKind, expected_version: expectedVersion },
          auth.session.csrf_token
        );
      } catch (reason) {
        const error = syncError(reason);
        if (error instanceof AuthApiError && error.status === 401) auth.invalidateAuthority();
        throw error;
      }
    },
    [api, auth.session]
  );

  const createWorkspace = useCallback(
    async (body: WorkspaceCreate, idempotencyKey: string) => {
      if (auth.session === null) throw new AuthApiError(401, "session_required");
      try {
        const workspace = await api.createWorkspace(
          body,
          auth.session.csrf_token,
          idempotencyKey
        );
        const workspaces = await api.listWorkspaces();
        setSnapshot((current) => ({ ...current, workspaces }));
        const selected = await loadWorkspace(workspace, emptySyncLedger());
        if (!selected) throw new AuthApiError(0, "workspace_open_failed");
        return workspace;
      } catch (reason) {
        const error = syncError(reason);
        if (error instanceof AuthApiError && error.status === 401) auth.invalidateAuthority();
        throw error;
      }
    },
    [api, auth.session, loadWorkspace]
  );

  const updateWorkspaceProfile = useCallback(
    async (name: string) => {
      const profile = snapshot.ledger.workspaceProfile ??
        (snapshot.selectedWorkspace === null
          ? null
          : {
              entity_id: snapshot.selectedWorkspace.id,
              name: snapshot.selectedWorkspace.name,
              workspace_kind: snapshot.selectedWorkspace.workspace_kind,
              status: snapshot.selectedWorkspace.status,
              version: snapshot.selectedWorkspace.version
            });
      if (
        !snapshot.canMutate ||
        snapshot.selectedWorkspace === null ||
        auth.session === null ||
        profile === null
      ) {
        throw new AuthApiError(403, "workspace_mutation_blocked");
      }
      const operation = ++operationRef.current;
      const selectedWorkspace = snapshot.selectedWorkspace;
      const ledger = snapshot.ledger;
      const csrfToken = auth.session.csrf_token;
      try {
        pendingConflictNameRef.current = name;
        await api.applySync(
          selectedWorkspace.id,
          {
            acknowledged_cursor: ledger.cursor,
            mutations: [
              {
                idempotency_key: crypto.randomUUID(),
                kind: "workspace_profile",
                operation: "update",
                entity_id: profile.entity_id,
                expected_version: profile.version,
                payload: { name }
              }
            ]
          },
          csrfToken
        );
        if (operation !== operationRef.current) return;
        await loadWorkspace(selectedWorkspace, ledger, operation);
        if (operation === operationRef.current) pendingConflictNameRef.current = null;
      } catch (reason) {
        if (operation !== operationRef.current) return;
        const error = syncError(reason);
        if (
          error instanceof AuthApiError &&
          error.status === 409 &&
          error.code === "sync_version_conflict"
        ) {
          const submitted = error.detail.submitted_expected_version;
          const current = error.detail.current_version;
          const currentProfile = error.detail.current_profile;
          if (
            typeof submitted === "number" &&
            typeof current === "number" &&
            typeof currentProfile === "object" &&
            currentProfile !== null
          ) {
            setSnapshot((value) => ({
              ...value,
              canMutate: false,
              conflict: {
                currentProfile: currentProfile as Readonly<Record<string, unknown>>,
                currentVersion: current,
                submittedExpectedVersion: submitted
              },
              error,
              status: "conflict"
            }));
            return;
          }
        }
        if (
          error instanceof AuthApiError &&
          error.status === 409 &&
          error.code === "sync_resync_required"
        ) {
          await resyncWorkspace(selectedWorkspace, error, operation);
          return;
        }
        if (error instanceof AuthApiError && error.status === 401) {
          auth.invalidateAuthority();
          return;
        }
        if (error instanceof AuthApiError && error.status === 403) {
          await refreshForbiddenMembership(error, operation);
          return;
        }
        setSnapshot((value) => ({
          ...value,
          canMutate: false,
          error,
          status:
            error instanceof AuthApiError && (error.status === 0 || error.status === 503)
              ? "offline"
              : "error"
        }));
      }
    },
    [api, auth, loadWorkspace, refreshForbiddenMembership, resyncWorkspace, snapshot]
  );

  const reloadConflict = useCallback(async () => {
    if (snapshot.selectedWorkspace === null) return;
    const operation = ++operationRef.current;
    pendingConflictNameRef.current = null;
    await loadWorkspace(snapshot.selectedWorkspace, emptySyncLedger(), operation);
  }, [loadWorkspace, snapshot.selectedWorkspace]);

  const retryConflict = useCallback(async () => {
    if (
      snapshot.conflict === null ||
      snapshot.selectedWorkspace === null ||
      auth.session === null ||
      pendingConflictNameRef.current === null
    ) {
      throw new AuthApiError(409, "sync_conflict_retry_unavailable");
    }
    const name = pendingConflictNameRef.current;
    const operation = ++operationRef.current;
    const selectedWorkspace = snapshot.selectedWorkspace;
    const conflict = snapshot.conflict;
    const cursor = snapshot.ledger.cursor;
    const csrfToken = auth.session.csrf_token;
    try {
      await api.applySync(
        selectedWorkspace.id,
        {
          acknowledged_cursor: cursor,
          mutations: [{
            idempotency_key: crypto.randomUUID(),
            kind: "workspace_profile",
            operation: "update",
            entity_id: selectedWorkspace.id,
            expected_version: conflict.currentVersion,
            payload: { name }
          }]
        },
        csrfToken
      );
      if (operation !== operationRef.current) return;
      pendingConflictNameRef.current = null;
      await loadWorkspace(selectedWorkspace, emptySyncLedger(), operation);
    } catch (reason) {
      if (operation !== operationRef.current) return;
      const error = syncError(reason);
      if (error instanceof AuthApiError && error.status === 401) {
        auth.invalidateAuthority();
        return;
      }
      if (error instanceof AuthApiError && error.status === 403) {
        await refreshForbiddenMembership(error, operation);
        return;
      }
      throw error;
    }
  }, [api, auth, loadWorkspace, refreshForbiddenMembership, snapshot.conflict, snapshot.ledger.cursor, snapshot.selectedWorkspace]);

  const value = useMemo<WorkspaceSyncState>(
    () => ({
      accountProfile:
        snapshot.ledger.accountProfile ??
        (auth.session?.experience_kind === null || auth.session === null
          ? null
          : {
              entity_id: auth.session.account_id,
              experience_kind: auth.session.experience_kind,
              version: auth.session.account_version
            }),
      canMutate: snapshot.canMutate,
      conflict: snapshot.conflict,
      createWorkspace,
      cursor: snapshot.ledger.cursor,
      error: snapshot.error,
      refresh,
      reloadConflict,
      retryConflict,
      saveExperience,
      selectedWorkspace: snapshot.selectedWorkspace,
      selectWorkspace,
      status: snapshot.status,
      updateWorkspaceProfile,
      workspaceProfile:
        snapshot.ledger.workspaceProfile ??
        (snapshot.selectedWorkspace === null
          ? null
          : {
              entity_id: snapshot.selectedWorkspace.id,
              name: snapshot.selectedWorkspace.name,
              workspace_kind: snapshot.selectedWorkspace.workspace_kind,
              status: snapshot.selectedWorkspace.status,
              version: snapshot.selectedWorkspace.version
            }),
      workspaces: snapshot.workspaces
    }),
    [
      auth.session,
      createWorkspace,
      refresh,
      reloadConflict,
      retryConflict,
      saveExperience,
      selectWorkspace,
      snapshot,
      updateWorkspaceProfile
    ]
  );

  return <WorkspaceSyncContext.Provider value={value}>{children}</WorkspaceSyncContext.Provider>;
}

export function useOptionalWorkspaceSync(): WorkspaceSyncState | null {
  return useContext(WorkspaceSyncContext);
}

export function useWorkspaceSync(): WorkspaceSyncState {
  const state = useOptionalWorkspaceSync();
  if (state === null) throw new Error("workspace_sync_provider_missing");
  return state;
}
