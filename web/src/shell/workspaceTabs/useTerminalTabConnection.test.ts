import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider, QueryObserver } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as UseRunnerHealthModule from "@/hooks/useRunnerHealth";
import { refreshRunnerHealth } from "@/hooks/useRunnerHealth";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { wakeSandboxSession } from "@/hooks/useSessionReconnect";
import { fetchTerminals, TERMINAL_LIST_TIMEOUT_MS, useTerminals } from "@/hooks/useTerminals";
import { SANDBOX_RESUME_TOTAL_LIMIT_MS } from "@/lib/sandboxResume";
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
vi.mock("@/hooks/useRunnerHealth", async (importOriginal) => ({
  ...(await importOriginal<typeof UseRunnerHealthModule>()),
  refreshRunnerHealth: vi.fn(),
}));

let sessionCount = 0;

describe("Resume session from a shell tab", () => {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  // Each test resumes its own session, so a request an earlier test left unanswered isn't joined.
  let sessionId = "";
  beforeEach(() => {
    sessionId = `s-resume-${++sessionCount}`;
  });
  const renderConnections = (shellWaits = createShellWaits()) =>
    renderHook(
      () =>
        useTerminalTabConnections(sessionId, shellWaits, {
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
    vi.mocked(refreshRunnerHealth).mockReset();
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

  it("doesn't let a late success end a newer Resume or start its wait over", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const shellWaits = createShellWaits();
    const restart = vi.spyOn(shellWaits, "restart");
    const first = heldRequest();
    const { result } = renderConnections(shellWaits);
    act(() => void result.current.resumeSession());
    await act(async () => {
      vi.advanceTimersByTime(SESSION_RESUME_REQUEST_LIMIT_MS);
    });
    heldRequest();
    act(() => void result.current.resumeSession());
    const restartsBefore = restart.mock.calls.length;

    await act(async () => first.resolve());
    expect(result.current.resumingSession).toBe(true);
    expect(restart).toHaveBeenCalledTimes(restartsBefore);
  });

  it("keeps one Resume and its state while the tab showing it is replaced", async () => {
    const held = heldRequest();
    const first = renderConnections();
    act(() => void first.result.current.resumeSession());
    first.unmount();

    const second = renderConnections();
    expect(second.result.current.resumingSession).toBe(true);
    act(() => void second.result.current.resumeSession());
    expect(retrySession).toHaveBeenCalledTimes(1);

    // Once the server has started the runner, its health is read again now, not at the next poll.
    expect(refreshRunnerHealth).not.toHaveBeenCalled();
    await act(async () => held.resolve());
    expect(second.result.current.resumingSession).toBe(false);
    expect(toast.error).not.toHaveBeenCalled();
    expect(refreshRunnerHealth).toHaveBeenCalledTimes(1);
  });
});

describe("Resume session from a shell tab, while Reconnect host wakes the sandbox", () => {
  afterEach(() => {
    vi.mocked(retrySession).mockReset();
    vi.mocked(refreshRunnerHealth).mockReset();
    resetSessionResumes();
  });

  it("joins that wake rather than asking again", async () => {
    let answer: () => void = () => {};
    vi.mocked(retrySession).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answer = () => resolve({} as never);
        }),
    );
    const client = new QueryClient();
    const reconnecting = wakeSandboxSession("s-joined");
    const { result } = renderHook(
      () =>
        useTerminalTabConnections("s-joined", createShellWaits(), {
          liveness: "runner_asleep",
          listFailed: false,
          shells: [],
        }),
      { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) },
    );
    let resumed!: Promise<void>;
    act(() => {
      resumed = result.current.resumeSession();
    });
    expect(result.current.resumingSession).toBe(true);
    expect(retrySession).toHaveBeenCalledTimes(1);

    await act(async () => {
      answer();
      await Promise.all([reconnecting, resumed]);
    });
    expect(result.current.resumingSession).toBe(false);
    expect(refreshRunnerHealth).toHaveBeenCalledTimes(1);
    client.clear();
  });
});

describe("a shell tab whose wait changes", () => {
  it("keeps timing the new wait when the session comes online on a tick", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    try {
      const client = new QueryClient();
      const shellWaits = createShellWaits();
      const shell = { key: "terminal:t1", inventory: "unknown" } as const;
      const { result, rerender } = renderHook(
        ({ liveness }: { liveness: "starting" | "online" }) =>
          useTerminalTabConnections("s-online", shellWaits, {
            liveness,
            listFailed: false,
            shells: [shell],
          }),
        {
          initialProps: { liveness: "starting" },
          wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children),
        },
      );
      // Starting for 30 s, past the shell list's own limit; it comes online right on a tick.
      await act(async () => vi.advanceTimersByTime(30_000));
      rerender({ liveness: "online" });
      expect(result.current.connectionOf(shell).kind).toBe("connecting");
      // The shell list's wait starts then, and runs out on its own.
      await act(async () => vi.advanceTimersByTime(SHELL_LIST_LIMIT_MS + 1_000));
      expect(result.current.connectionOf(shell).kind).toBe("unreachable");
      client.clear();
    } finally {
      vi.useRealTimers();
    }
  });

  /** A panel showing one unlisted shell, timed by `shellWaits`, for the session's `liveness`. */
  function renderShellPanel(
    shellWaits: ReturnType<typeof createShellWaits>,
    liveness: "starting" | "online",
    client: QueryClient,
  ) {
    const shell = { key: "terminal:t1", inventory: "unknown" } as const;
    const view = renderHook(
      () =>
        useTerminalTabConnections("s-replaced", shellWaits, {
          liveness,
          listFailed: false,
          shells: [shell],
        }),
      { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) },
    );
    return { ...view, kind: () => view.result.current.connectionOf(shell).kind };
  }

  it.each([
    ["a while", 30_000],
    ["until its own limit", SANDBOX_RESUME_TOTAL_LIMIT_MS],
  ])(
    "times the shell list's wait in a panel that replaces one that waited for the start %s",
    async (_case, startingFor) => {
      vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
      try {
        const client = new QueryClient();
        const shellWaits = createShellWaits();
        const starting = renderShellPanel(shellWaits, "starting", client);
        await act(async () => vi.advanceTimersByTime(startingFor));
        starting.unmount();

        // The new panel mounts on the same clock reading, with the session now online.
        const online = renderShellPanel(shellWaits, "online", client);
        expect(online.kind()).toBe("connecting");
        await act(async () => vi.advanceTimersByTime(SHELL_LIST_LIMIT_MS - 1_000));
        expect(online.kind()).toBe("connecting");
        await act(async () => vi.advanceTimersByTime(1_000));
        expect(online.kind()).toBe("unreachable");
        client.clear();
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it("wakes only when a wait reaches its limit, and not at all once every wait has", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    try {
      const client = new QueryClient();
      const shell = { key: "terminal:t1", inventory: "unknown" } as const;
      const shellWaits = createShellWaits();
      let renders = 0;
      const { result } = renderHook(
        () => {
          renders += 1;
          return useTerminalTabConnections("s-idle", shellWaits, {
            liveness: "online",
            listFailed: false,
            shells: [shell],
          });
        },
        { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) },
      );
      // One timer, set for the shell list's limit, rather than one each second.
      expect(vi.getTimerCount()).toBe(1);
      const rendersWhileWaiting = renders;
      await act(async () => vi.advanceTimersByTime(SHELL_LIST_LIMIT_MS - 1_000));
      expect(renders).toBe(rendersWhileWaiting);
      expect(result.current.connectionOf(shell).kind).toBe("connecting");

      await act(async () => vi.advanceTimersByTime(1_000));
      expect(result.current.connectionOf(shell).kind).toBe("unreachable");
      // The shell still waits, but nothing it shows changes any more, so no timer is left.
      expect(vi.getTimerCount()).toBe(0);
      const rendersAfterLimit = renders;
      await act(async () => vi.advanceTimersByTime(60_000));
      expect(renders).toBe(rendersAfterLimit);
      client.clear();
    } finally {
      vi.useRealTimers();
    }
  });

  it("still reaches the limit when the system clock is set back during the wait", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    try {
      vi.setSystemTime(1_000);
      const client = new QueryClient();
      const shell = { key: "terminal:t1", inventory: "unknown" } as const;
      const shellWaits = createShellWaits();
      const { result } = renderHook(
        () =>
          useTerminalTabConnections("s-clock", shellWaits, {
            liveness: "online",
            listFailed: false,
            shells: [shell],
          }),
        { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) },
      );
      // The clock moves back a second, so the timer fires a second short of the wall deadline.
      vi.setSystemTime(0);
      await act(async () => vi.advanceTimersByTime(SHELL_LIST_LIMIT_MS));
      expect(result.current.connectionOf(shell).kind).toBe("connecting");

      await act(async () => vi.advanceTimersByTime(1_000));
      expect(result.current.connectionOf(shell).kind).toBe("unreachable");
      client.clear();
    } finally {
      vi.useRealTimers();
    }
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

describe("the shell list a shell tab reads", () => {
  it("fails after the hook's one retry, about a minute in, and Try Again asks afresh", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn((_url: string) => new Promise<never>(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    const listRequests = () =>
      fetchMock.mock.calls.filter(([url]) => String(url).includes("/resources/terminals")).length;
    // The client wouldn't retry; the list's own retry: 1 does.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const shell = { key: "terminal:t1", inventory: "unknown" } as const;
    const shellWaits = createShellWaits();
    const { result } = renderHook(
      () => {
        const list = useTerminals("s-list-retry");
        const connections = useTerminalTabConnections("s-list-retry", shellWaits, {
          liveness: "online",
          listFailed: list.error !== null && !list.isFetching,
          shells: [shell],
        });
        return { list, connections };
      },
      { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) },
    );
    try {
      await vi.advanceTimersByTimeAsync(TERMINAL_LIST_TIMEOUT_MS);
      expect(listRequests()).toBe(1);
      // The retry asks again after its backoff, and gets the same bounded wait.
      await vi.advanceTimersByTimeAsync(1_000);
      expect(listRequests()).toBe(2);
      await vi.advanceTimersByTimeAsync(TERMINAL_LIST_TIMEOUT_MS - 1);
      expect(result.current.list.error).toBeNull();
      // The second request starts a moment after the backoff, so it times out just past 61 s.
      await vi.advanceTimersByTimeAsync(1_000);
      expect(result.current.list.error).not.toBeNull();
      expect(result.current.list.isFetching).toBe(false);
      expect(result.current.connections.connectionOf(shell).kind).toBe("unreachable");

      act(() => result.current.connections.tryAgain());
      await vi.advanceTimersByTimeAsync(0);
      expect(listRequests()).toBe(3);
      expect(result.current.list.isFetching).toBe(true);
      expect(result.current.connections.connectionOf(shell).kind).toBe("connecting");
    } finally {
      client.clear();
      vi.unstubAllGlobals();
      vi.useRealTimers();
    }
  });
});
