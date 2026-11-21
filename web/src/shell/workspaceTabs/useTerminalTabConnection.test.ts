import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider, QueryObserver } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as SessionsApiModule from "@/lib/sessionsApi";
import { fetchTerminals, TERMINAL_LIST_TIMEOUT_MS } from "@/hooks/useTerminals";
import { retrySession } from "@/lib/sessionsApi";
import { terminalsQueryKey } from "@/lib/terminals";

import { createShellWaits } from "./shellWaits";
import { SHELL_LIST_LIMIT_MS } from "./terminalTabConnection";
import {
  resetSessionResumes,
  SESSION_RESUME_REQUEST_LIMIT_MS,
  SESSION_RESUME_TIMED_OUT,
  useTerminalTabConnections,
} from "./useTerminalTabConnection";

// The session's own facts have their suites; these tests cover the Resume request's lifecycle.
vi.mock("@/hooks/useSession", () => ({ useSession: () => ({ session: null }) }));
vi.mock("@/shell/commandContext", () => ({ useCommandSession: () => null }));
vi.mock("@/hooks/useSandboxResume", () => ({
  useSandboxResume: () => ({
    status: "none",
    resumeStatus: "none",
    failure: null,
    resume: vi.fn(),
  }),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  retrySession: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));

describe("Resume session from a shell tab", () => {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  const renderConnections = () =>
    renderHook(
      () =>
        useTerminalTabConnections("s-resume", createShellWaits(), {
          liveness: "runner_asleep",
          listFailed: false,
          shells: [],
        }),
      { wrapper },
    );
  /** A Resume request the test answers. */
  function heldRequest() {
    const held = { resolve: () => {}, reject: (_error: Error) => {} };
    vi.mocked(retrySession).mockImplementationOnce(
      () =>
        new Promise((resolve, reject) => {
          held.resolve = () => resolve({} as never);
          held.reject = reject;
        }),
    );
    return held;
  }
  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(retrySession).mockReset();
    vi.mocked(toast.error).mockReset();
    resetSessionResumes();
  });

  it("shows why a rejected Resume failed, and can be tried again", async () => {
    const first = heldRequest();
    const { result } = renderConnections();
    let resumed!: Promise<void>;
    act(() => {
      resumed = result.current.resumeSession();
    });
    expect(result.current.resumingSession).toBe(true);
    await act(async () => {
      first.reject(new Error("The host refused."));
      await resumed;
    });
    expect(toast.error).toHaveBeenCalledWith("The host refused.");
    expect(result.current.resumingSession).toBe(false);

    heldRequest();
    act(() => void result.current.resumeSession());
    expect(retrySession).toHaveBeenCalledTimes(2);
  });

  it("gives up on a Resume with no answer in time, and ignores that answer once it's late", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const first = heldRequest();
    const { result } = renderConnections();
    act(() => void result.current.resumeSession());
    await act(async () => {
      vi.advanceTimersByTime(SESSION_RESUME_REQUEST_LIMIT_MS);
    });
    expect(toast.error).toHaveBeenCalledWith(SESSION_RESUME_TIMED_OUT);
    expect(result.current.resumingSession).toBe(false);

    // A second try is on its way when the first one's answer finally arrives.
    heldRequest();
    act(() => void result.current.resumeSession());
    expect(retrySession).toHaveBeenCalledTimes(2);
    await act(async () => first.reject(new Error("late")));
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(result.current.resumingSession).toBe(true);
  });
});

describe("Try Again from a shell tab", () => {
  it("asks afresh once a list request that never answered has failed at its deadline", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() => new Promise<never>(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const queryKey = terminalsQueryKey("s-hung");
    const observer = new QueryObserver(client, {
      queryKey,
      queryFn: () => fetchTerminals("s-hung"),
    });
    const unsubscribe = observer.subscribe(() => {});
    const { result } = renderHook(
      () =>
        useTerminalTabConnections("s-hung", createShellWaits(), {
          liveness: "online",
          listFailed: false,
          shells: [],
        }),
      { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) },
    );
    try {
      await vi.advanceTimersByTimeAsync(SHELL_LIST_LIMIT_MS);
      // A Try Again when the tab gives up joins the request still on its way.
      act(() => result.current.tryAgain());
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(TERMINAL_LIST_TIMEOUT_MS - SHELL_LIST_LIMIT_MS);
      expect(client.getQueryState(queryKey)?.status).toBe("error");
      act(() => result.current.tryAgain());
      await vi.advanceTimersByTimeAsync(0);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      unsubscribe();
      client.clear();
      vi.unstubAllGlobals();
      vi.useRealTimers();
    }
  });

  it("joins a list request already on its way, so a stale answer can't land after a newer one", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const queryKey = terminalsQueryKey("s-retry");
    // Shells the event stream already put in the cache, while the list request hangs.
    client.setQueryData(queryKey, [{ id: "t1", name: "zsh", session: "s-retry", running: true }]);
    let answer: (list: unknown[]) => void = () => {};
    const queryFn = vi.fn(
      () =>
        new Promise<unknown[]>((resolve) => {
          answer = resolve;
        }),
    );
    const observer = new QueryObserver(client, { queryKey, queryFn, staleTime: 0 });
    const unsubscribe = observer.subscribe(() => {});
    await vi.waitFor(() => expect(queryFn).toHaveBeenCalledTimes(1));
    const pending = client.getQueryCache().find({ queryKey })!.promise;

    const { result } = renderHook(
      () =>
        useTerminalTabConnections("s-retry", createShellWaits(), {
          liveness: "online",
          listFailed: false,
          shells: [],
        }),
      { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) },
    );
    act(() => result.current.tryAgain());
    expect(queryFn).toHaveBeenCalledTimes(1);
    expect(client.getQueryCache().find({ queryKey })!.promise).toBe(pending);
    answer([]);
    await vi.waitFor(() => expect(client.getQueryData(queryKey)).toEqual([]));
    unsubscribe();
    client.clear();
  });
});
