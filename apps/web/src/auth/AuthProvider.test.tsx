import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth } from "./AuthProvider";
import {
  AuthApiError,
  type AuthApi,
  type SessionResponse
} from "./authApi";

const SESSION: SessionResponse = {
  account_id: "11111111-1111-4111-8111-111111111111",
  principal_id: "22222222-2222-4222-8222-222222222222",
  email: "person@example.com",
  experience_kind: "developer",
  account_version: 3,
  session_version: 7,
  csrf_token: "csrf-opaque"
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

function completeApi(overrides: Partial<AuthApi> = {}): AuthApi {
  return {
    getSession: vi.fn(),
    logout: vi.fn(),
    refreshSession: vi.fn(),
    startGoogle: vi.fn(),
    ...overrides
  };
}

function Probe({ children }: { children?: ReactNode }) {
  const auth = useAuth();
  return (
    <div>
      <output aria-label="auth status">{auth.status}</output>
      <output aria-label="session email">{auth.session?.email ?? "none"}</output>
      <output aria-label="session version">{auth.session?.session_version ?? "none"}</output>
      <button onClick={auth.startGoogle} type="button">Continue with Google</button>
      <button onClick={() => void auth.refresh()} type="button">Refresh session</button>
      <button onClick={() => void auth.logout()} type="button">Log out</button>
      <button onClick={() => void auth.retry()} type="button">Retry session</button>
      <button onClick={() => void auth.reloadSession().catch(() => undefined)} type="button">Reload session truth</button>
      <button onClick={() => auth.invalidateAuthority()} type="button">Invalidate authority</button>
      {children}
    </div>
  );
}

describe("AuthProvider", () => {
  it("moves from checking to unauthenticated on 401 and starts Google sign-in", async () => {
    const request = deferred<never>();
    const startGoogle = vi.fn();
    const api = completeApi({ getSession: vi.fn(() => request.promise), startGoogle });

    render(
      <AuthProvider api={api}>
        <Probe />
      </AuthProvider>
    );

    expect(screen.getByLabelText("auth status")).toHaveTextContent("checking");
    request.reject(new AuthApiError(401, "session_required", "correlation-1"));
    await waitFor(() => expect(screen.getByLabelText("auth status")).toHaveTextContent("unauthenticated"));

    await userEvent.setup().click(screen.getByRole("button", { name: "Continue with Google" }));
    expect(startGoogle).toHaveBeenCalledOnce();
  });

  it("exposes an authenticated session and rotates only in-memory session material", async () => {
    const api = completeApi({
      getSession: vi.fn().mockResolvedValue(SESSION),
      refreshSession: vi.fn().mockResolvedValue({ csrf_token: "csrf-rotated", session_version: 8 })
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem");

    render(<AuthProvider api={api}><Probe /></AuthProvider>);

    await waitFor(() => expect(screen.getByLabelText("auth status")).toHaveTextContent("authenticated"));
    expect(screen.getByLabelText("session email")).toHaveTextContent("person@example.com");
    await userEvent.setup().click(screen.getByRole("button", { name: "Refresh session" }));
    await waitFor(() => expect(screen.getByLabelText("session version")).toHaveTextContent("8"));
    expect(api.refreshSession).toHaveBeenCalledWith("csrf-opaque");
    expect(setItem).not.toHaveBeenCalled();
  });

  it("logs out with the in-memory CSRF token and clears authority", async () => {
    const api = completeApi({
      getSession: vi.fn().mockResolvedValue(SESSION),
      logout: vi.fn().mockResolvedValue(undefined)
    });
    render(<AuthProvider api={api}><Probe /></AuthProvider>);
    await waitFor(() => expect(screen.getByLabelText("auth status")).toHaveTextContent("authenticated"));

    await userEvent.setup().click(screen.getByRole("button", { name: "Log out" }));

    await waitFor(() => expect(screen.getByLabelText("auth status")).toHaveTextContent("unauthenticated"));
    expect(api.logout).toHaveBeenCalledWith("csrf-opaque");
    expect(screen.getByLabelText("session email")).toHaveTextContent("none");
  });

  it.each([
    new AuthApiError(503, "platform_identity_unavailable", "correlation-503"),
    new AuthApiError(0, "network_unavailable")
  ])("keeps retryable failures explicit and retries session discovery", async (failure) => {
    const getSession = vi.fn().mockRejectedValueOnce(failure).mockResolvedValueOnce(SESSION);
    render(<AuthProvider api={completeApi({ getSession })}><Probe /></AuthProvider>);

    await waitFor(() => expect(screen.getByLabelText("auth status")).toHaveTextContent("retryable_error"));
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry session" }));

    await waitFor(() => expect(screen.getByLabelText("auth status")).toHaveTextContent("authenticated"));
    expect(getSession).toHaveBeenCalledTimes(2);
  });

  it("treats a 401 during logout as signed out and clears stale session authority", async () => {
    const api = completeApi({
      getSession: vi.fn().mockResolvedValue(SESSION),
      logout: vi.fn().mockRejectedValue(new AuthApiError(401, "session_invalid"))
    });
    render(<AuthProvider api={api}><Probe /></AuthProvider>);
    await waitFor(() => expect(screen.getByLabelText("auth status")).toHaveTextContent("authenticated"));

    await userEvent.setup().click(screen.getByRole("button", { name: "Log out" }));

    await waitFor(() => expect(screen.getByLabelText("auth status")).toHaveTextContent("unauthenticated"));
    expect(screen.getByLabelText("session email")).toHaveTextContent("none");
  });

  it("ignores an older boot completion after a newer retry establishes authority", async () => {
    const first = deferred<SessionResponse>();
    const second = deferred<SessionResponse>();
    const getSession = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<AuthProvider api={completeApi({ getSession })}><Probe /></AuthProvider>);

    await userEvent.setup().click(screen.getByRole("button", { name: "Retry session" }));
    await act(async () => { second.resolve({ ...SESSION, email: "new@example.com" }); });
    await waitFor(() => expect(screen.getByLabelText("session email")).toHaveTextContent("new@example.com"));
    await act(async () => { first.resolve({ ...SESSION, email: "stale@example.com" }); });

    await waitFor(() => expect(screen.getByLabelText("session email")).toHaveTextContent("new@example.com"));
  });

  it("lets child mutation boundaries centrally invalidate session authority", async () => {
    render(<AuthProvider api={completeApi({ getSession: vi.fn().mockResolvedValue(SESSION) })}><Probe /></AuthProvider>);
    await waitFor(() => expect(screen.getByLabelText("auth status")).toHaveTextContent("authenticated"));

    await userEvent.setup().click(screen.getByRole("button", { name: "Invalidate authority" }));

    expect(screen.getByLabelText("auth status")).toHaveTextContent("unauthenticated");
    expect(screen.getByLabelText("session email")).toHaveTextContent("none");
  });

  it("ignores a stale reload 401 after a newer reload establishes authority", async () => {
    const firstReload = deferred<SessionResponse>();
    const secondReload = deferred<SessionResponse>();
    const getSession = vi.fn()
      .mockResolvedValueOnce(SESSION)
      .mockReturnValueOnce(firstReload.promise)
      .mockReturnValueOnce(secondReload.promise);
    render(<AuthProvider api={completeApi({ getSession })}><Probe /></AuthProvider>);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("auth status")).toHaveTextContent("authenticated"));

    await user.click(screen.getByRole("button", { name: "Reload session truth" }));
    await user.click(screen.getByRole("button", { name: "Reload session truth" }));
    await act(async () => { secondReload.resolve({ ...SESSION, email: "new@example.com" }); });
    await waitFor(() => expect(screen.getByLabelText("session email")).toHaveTextContent("new@example.com"));
    await act(async () => { firstReload.reject(new AuthApiError(401, "session_invalid")); });

    expect(screen.getByLabelText("auth status")).toHaveTextContent("authenticated");
    expect(screen.getByLabelText("session email")).toHaveTextContent("new@example.com");
  });
});
