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

import {
  AuthApiError,
  createAuthApi,
  type AuthApi,
  type SessionResponse
} from "./authApi";

export type AuthStatus = "checking" | "unauthenticated" | "authenticated" | "retryable_error";

export interface AuthState {
  error: AuthApiError | null;
  invalidateAuthority(): void;
  logout(): Promise<void>;
  reloadSession(): Promise<SessionResponse | null>;
  refresh(): Promise<void>;
  retry(): Promise<void>;
  session: SessionResponse | null;
  startGoogle(): void;
  status: AuthStatus;
}

interface AuthProviderProps {
  api?: AuthApi;
  children: ReactNode;
}

interface AuthSnapshot {
  error: AuthApiError | null;
  session: SessionResponse | null;
  status: AuthStatus;
}

const browserAuthApi = createAuthApi();
const AuthContext = createContext<AuthState | null>(null);

function normalizedError(error: unknown): AuthApiError {
  return error instanceof AuthApiError
    ? error
    : new AuthApiError(0, "network_unavailable");
}

export function AuthProvider({ api = browserAuthApi, children }: AuthProviderProps) {
  const generationRef = useRef(0);
  const [snapshot, setSnapshot] = useState<AuthSnapshot>({
    error: null,
    session: null,
    status: "checking"
  });

  const loadSession = useCallback(async () => {
    const generation = ++generationRef.current;
    setSnapshot((current) => ({ ...current, error: null, status: "checking" }));
    try {
      const session = await api.getSession();
      if (generation === generationRef.current) {
        setSnapshot({ error: null, session, status: "authenticated" });
      }
    } catch (reason) {
      const error = normalizedError(reason);
      if (generation === generationRef.current) {
        setSnapshot({
          error: error.status === 401 ? null : error,
          session: null,
          status: error.status === 401 ? "unauthenticated" : "retryable_error"
        });
      }
    }
  }, [api]);

  useEffect(() => {
    void loadSession();
    return () => {
      generationRef.current += 1;
    };
  }, [loadSession]);

  const invalidateAuthority = useCallback(() => {
    generationRef.current += 1;
    setSnapshot({ error: null, session: null, status: "unauthenticated" });
  }, []);

  const reloadSession = useCallback(async () => {
    const generation = ++generationRef.current;
    try {
      const session = await api.getSession();
      if (generation === generationRef.current) {
        setSnapshot({ error: null, session, status: "authenticated" });
        return session;
      }
      return null;
    } catch (reason) {
      const error = normalizedError(reason);
      if (generation !== generationRef.current) return null;
      if (error.status === 401) invalidateAuthority();
      else if (generation === generationRef.current) {
        setSnapshot((current) => ({ ...current, error, status: "retryable_error" }));
      }
      throw error;
    }
  }, [api, invalidateAuthority]);

  const refresh = useCallback(async () => {
    if (snapshot.session === null) return;
    const generation = ++generationRef.current;
    try {
      const refreshed = await api.refreshSession(snapshot.session.csrf_token);
      if (generation !== generationRef.current) return;
      setSnapshot((current) =>
        current.session === null
          ? current
          : {
              error: null,
              session: {
                ...current.session,
                csrf_token: refreshed.csrf_token,
                session_version: refreshed.session_version
              },
              status: "authenticated"
            }
      );
    } catch (reason) {
      const error = normalizedError(reason);
      if (generation !== generationRef.current) return;
      setSnapshot((current) => ({
        error: error.status === 401 ? null : error,
        session: error.status === 401 ? null : current.session,
        status: error.status === 401 ? "unauthenticated" : "retryable_error"
      }));
    }
  }, [api, snapshot.session]);

  const logout = useCallback(async () => {
    if (snapshot.session === null) return;
    const generation = ++generationRef.current;
    try {
      await api.logout(snapshot.session.csrf_token);
      if (generation === generationRef.current) invalidateAuthority();
    } catch (reason) {
      const error = normalizedError(reason);
      if (generation !== generationRef.current) return;
      setSnapshot((current) =>
        error.status === 401
          ? { error: null, session: null, status: "unauthenticated" }
          : { ...current, error, status: "retryable_error" }
      );
    }
  }, [api, invalidateAuthority, snapshot.session]);

  const value = useMemo<AuthState>(
    () => ({
      ...snapshot,
      invalidateAuthority,
      logout,
      reloadSession,
      refresh,
      retry: loadSession,
      startGoogle: api.startGoogle
    }),
    [api.startGoogle, invalidateAuthority, loadSession, logout, refresh, reloadSession, snapshot]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useOptionalAuth(): AuthState | null {
  return useContext(AuthContext);
}

export function useAuth(): AuthState {
  const state = useOptionalAuth();
  if (state === null) throw new Error("auth_provider_missing");
  return state;
}
