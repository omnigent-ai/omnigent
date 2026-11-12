import type * as SessionsApiModule from "@/lib/sessionsApi";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import { refreshRunnerHealth } from "@/hooks/useRunnerHealth";
import type { SessionLiveness } from "@/hooks/useRunnerHealth";
import { controlHost, getHostIdentity, isElectronShell } from "@/lib/nativeBridge";
import type { HostActionResult, HostIdentity } from "@/lib/nativeBridge";
import { retrySession } from "@/lib/sessionsApi";
import type { PostEventResponse } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import {
  SANDBOX_WAKE_ONLINE_GRACE_POLLS,
  SANDBOX_WAKE_STAGE_LIMIT_MS,
  useSandboxStarting,
  useSessionReconnect,
  wakeSandboxSession,
} from "./useSessionReconnect";

const healthPollListeners = vi.hoisted(
  () => new Set<(liveness: Map<string, SessionLiveness>, scheduled: boolean) => void>(),
);
vi.mock("@/hooks/useRunnerHealth", () => ({
  onRunnerHealthPolled: (
    listener: (liveness: Map<string, SessionLiveness>, scheduled: boolean) => void,
  ) => {
    healthPollListeners.add(listener);
    return () => healthPollListeners.delete(listener);
  },
  refreshRunnerHealth: vi.fn(),
}));
vi.mock("@/lib/nativeBridge", () => ({
  controlHost: vi.fn(),
  getHostIdentity: vi.fn(),
  isElectronShell: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  retrySession: vi.fn(),
}));
vi.mock("sonner", () => ({
  toast: { loading: vi.fn(() => "progress"), success: vi.fn(), error: vi.fn(), dismiss: vi.fn() },
}));

const localSession = { sessionId: "session", hostId: "this-mac", isOwner: true };
const asleepSandbox = {
  sessionId: "session",
  hostId: "sandbox-host",
  hostResumable: true,
  isOwner: false,
};

/** Finishes `count` `/health` polls that report `sessionId`'s host as `hostOnline`. */
function finishHealthPolls(
  count: number,
  {
    hostOnline = false,
    sessionId = "session",
    scheduled = true,
  }: { hostOnline?: boolean | null; sessionId?: string; scheduled?: boolean } = {},
) {
  const liveness = new Map([
    [sessionId, { runner_online: false, host_online: hostOnline, host_version: null }],
  ]);
  for (let poll = 0; poll < count; poll += 1) {
    act(() => {
      for (const listener of [...healthPollListeners]) listener(liveness, scheduled);
    });
  }
}

// A test that leaves a wake request unanswered would leave it shared with the
// next test's wakes of the same session, so every one is settled after each test.
const unsettled = new Set<() => void>();

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = (value) => {
      unsettled.delete(settle);
      done(value);
    };
    reject = (error) => {
      unsettled.delete(settle);
      fail(error);
    };
  });
  promise.catch(() => {});
  const settle = () => reject(new Error("test ended"));
  unsettled.add(settle);
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(isElectronShell).mockReturnValue(true);
  vi.mocked(getHostIdentity).mockResolvedValue({ cliInstalled: true, hostId: "this-mac" });
  vi.mocked(controlHost).mockResolvedValue({ ok: true });
  useChatStore.setState({ conversationId: null, sandboxStatus: null });
  conversationRegistry.clear();
});
afterEach(async () => {
  cleanup();
  for (const settle of [...unsettled]) settle();
  // Let the shared wake requests see their answers and drop out.
  await new Promise<void>((done) => {
    setTimeout(done, 0);
  });
});

describe("useSessionReconnect", () => {
  it("starts the local host on the first action without opening the dialog", async () => {
    const pending = deferred<HostActionResult>();
    vi.mocked(controlHost).mockReturnValue(pending.promise);
    const { result } = renderHook(() => useSessionReconnect(localSession));

    let reconnect!: Promise<void>;
    await act(async () => {
      reconnect = result.current.reconnect();
    });
    expect(controlHost).toHaveBeenCalledExactlyOnceWith("start");
    expect(result.current.dialogOpen).toBe(false);
    expect(result.current.localReconnect?.reconnecting).toBe(true);
    expect(toast.loading).toHaveBeenCalledWith("Reconnecting this machine…");
    expect(toast.success).not.toHaveBeenCalled();

    await act(async () => {
      pending.resolve({ ok: true });
      await reconnect;
    });
    expect(result.current.dialogOpen).toBe(false);
    expect(result.current.localReconnect?.reconnecting).toBe(false);
    expect(toast.success).toHaveBeenCalledWith("This machine is back online.");
    expect(toast.dismiss).toHaveBeenCalledWith("progress");
  });

  it("ignores repeated clicks during both identity lookup and host startup", async () => {
    const identity = deferred<HostIdentity>();
    const start = deferred<HostActionResult>();
    vi.mocked(getHostIdentity).mockReturnValue(identity.promise);
    vi.mocked(controlHost).mockReturnValue(start.promise);
    const { result } = renderHook(() => useSessionReconnect(localSession));
    let first!: Promise<void>;
    await act(async () => {
      first = result.current.reconnect();
      await result.current.reconnect();
    });
    expect(getHostIdentity).toHaveBeenCalledOnce();
    await act(async () => {
      identity.resolve({ cliInstalled: true, hostId: "this-mac" });
    });
    await act(async () => {
      await result.current.reconnect();
    });
    expect(controlHost).toHaveBeenCalledOnce();
    await act(async () => {
      start.resolve({ ok: true });
      await first;
    });
  });

  it.each([
    { label: "another machine", identity: { cliInstalled: true, hostId: "other" } },
    { label: "missing CLI", identity: { cliInstalled: false, hostId: "this-mac" } },
    { label: "missing identity", identity: null },
  ])("opens the fallback for $label without starting a host", async ({ identity }) => {
    vi.mocked(getHostIdentity).mockResolvedValue(identity);
    const { result } = renderHook(() => useSessionReconnect(localSession));
    await act(async () => {
      await result.current.reconnect();
    });
    expect(result.current.dialogOpen).toBe(true);
    expect(result.current.localReconnect).toBeUndefined();
    expect(controlHost).not.toHaveBeenCalled();
  });

  it("offers dialog recovery for another remote host", async () => {
    const { result } = renderHook(() => useSessionReconnect({ ...localSession, hostId: "devbox" }));

    await act(async () => {
      await result.current.reconnect();
    });

    expect(controlHost).not.toHaveBeenCalled();
    expect(result.current.dialogOpen).toBe(true);
  });

  it.each([
    { label: "browser", desktop: false, isOwner: true },
    { label: "non-owner", desktop: true, isOwner: false },
  ])("leaves a $label on the dialog without starting the host", async ({ desktop, isOwner }) => {
    vi.mocked(isElectronShell).mockReturnValue(desktop);
    const { result } = renderHook(() =>
      useSessionReconnect({
        ...localSession,
        hostId: "remote-host",
        isOwner,
      }),
    );

    await act(async () => {
      await result.current.reconnect();
    });

    expect(result.current.dialogOpen).toBe(true);
  });

  it.each([
    { queued: false, recovered: true, recovery: "runner_relaunched" },
    { queued: false, recovered: false, recovery: "already_connected" },
  ] satisfies PostEventResponse[])(
    "wakes an asleep sandbox for an editor in a browser with no toast: $recovery",
    async (response) => {
      vi.mocked(isElectronShell).mockReturnValue(false);
      const pending = deferred<PostEventResponse>();
      vi.mocked(retrySession).mockReturnValue(pending.promise);
      const { result, rerender } = renderHook(useSessionReconnect, {
        initialProps: { ...asleepSandbox, hostOnline: false as boolean | null },
      });
      expect(result.current.sandboxStarting).toBe(false);

      let reconnect!: Promise<void>;
      await act(async () => {
        reconnect = result.current.reconnect();
        await result.current.reconnect();
      });
      expect(retrySession).toHaveBeenCalledExactlyOnceWith("session");
      expect(result.current.sandboxStarting).toBe(true);
      expect(result.current.dialogOpen).toBe(false);
      expect(getHostIdentity).not.toHaveBeenCalled();

      await act(async () => {
        pending.resolve(response);
        await reconnect;
      });
      // Liveness hasn't caught up yet: still starting, with a re-poll requested.
      expect(result.current.sandboxStarting).toBe(true);
      expect(refreshRunnerHealth).toHaveBeenCalledOnce();

      rerender({ ...asleepSandbox, hostOnline: true });
      expect(result.current.sandboxStarting).toBe(false);
      expect(toast.loading).not.toHaveBeenCalled();
      expect(toast.success).not.toHaveBeenCalled();
      expect(toast.error).not.toHaveBeenCalled();
      expect(result.current.dialogOpen).toBe(false);
    },
  );

  it("says so when health polls keep reporting a woken sandbox offline", async () => {
    vi.useFakeTimers();
    try {
      const pending = deferred<PostEventResponse>();
      vi.mocked(retrySession).mockReturnValue(pending.promise);
      const { result, rerender } = renderHook(
        (props: { hostOnline: boolean | null | undefined }) =>
          useSessionReconnect({ ...asleepSandbox, hostOnline: props.hostOnline }),
        { initialProps: { hostOnline: false as boolean | null | undefined } },
      );
      let reconnect!: Promise<void>;
      await act(async () => {
        reconnect = result.current.reconnect();
      });
      // The count starts when the server answers, not at the click.
      finishHealthPolls(3);
      expect(result.current.sandboxStarting).toBe(true);
      await act(async () => {
        pending.resolve({ queued: false, recovered: true });
        await reconnect;
      });
      // Polls backed off to the 60s ceiling don't run out a deadline between them.
      act(() => vi.advanceTimersByTime(3 * 60_000));
      finishHealthPolls(3, { sessionId: "other-session" });
      // Extra polls from a refresh or a dialog don't use up the count.
      finishHealthPolls(3, { scheduled: false });
      expect(result.current.sandboxStarting).toBe(true);
      // Two offline polls keep it starting; the third ends it.
      finishHealthPolls(2);
      // Liveness settling to another not-online value must not restart the count.
      rerender({ hostOnline: null });
      expect(result.current.sandboxStarting).toBe(true);
      expect(toast.error).not.toHaveBeenCalled();
      finishHealthPolls(1, { hostOnline: null });
      expect(result.current.sandboxStarting).toBe(false);
      expect(toast.error).toHaveBeenCalledExactlyOnceWith(
        "The sandbox didn't come online. Try Reconnect host again.",
      );

      // Retrying after the timeout gets its own full count.
      await act(async () => {
        await result.current.reconnect();
      });
      expect(retrySession).toHaveBeenCalledTimes(2);
      finishHealthPolls(2);
      expect(result.current.sandboxStarting).toBe(true);
      expect(toast.error).toHaveBeenCalledOnce();
      finishHealthPolls(1);
      expect(result.current.sandboxStarting).toBe(false);
      expect(toast.error).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["the host comes online", "the user opens another session"] as const)(
    "doesn't report a late timeout once %s",
    async (ending) => {
      vi.mocked(retrySession).mockResolvedValue({ queued: false, recovered: true });
      const { result, rerender } = renderHook(useSessionReconnect, {
        initialProps: { ...asleepSandbox, hostOnline: false as boolean | null },
      });
      await act(async () => {
        await result.current.reconnect();
      });
      finishHealthPolls(SANDBOX_WAKE_ONLINE_GRACE_POLLS - 1);
      expect(result.current.sandboxStarting).toBe(true);
      if (ending === "the host comes online") {
        // The poll that reads online reaches the hook before the liveness it feeds.
        finishHealthPolls(1, { hostOnline: true });
        rerender({ ...asleepSandbox, hostOnline: true });
      } else {
        rerender({ ...asleepSandbox, sessionId: "other-session", hostOnline: false });
      }
      expect(result.current.sandboxStarting).toBe(false);
      finishHealthPolls(SANDBOX_WAKE_ONLINE_GRACE_POLLS * 2);
      expect(result.current.sandboxStarting).toBe(false);
      expect(toast.error).not.toHaveBeenCalled();
    },
  );

  it("holds the count while the launch reports progress, then counts from zero", async () => {
    useChatStore.setState({ conversationId: "session", sandboxStatus: null });
    vi.mocked(retrySession).mockResolvedValue({ queued: false, recovered: true });
    const { result } = renderHook(() =>
      useSessionReconnect({ ...asleepSandbox, hostOnline: false }),
    );
    await act(async () => {
      await result.current.reconnect();
    });
    finishHealthPolls(SANDBOX_WAKE_ONLINE_GRACE_POLLS - 1);

    for (const stage of ["provisioning", "connecting"] as const) {
      act(() => useChatStore.setState({ sandboxStatus: { stage } }));
      finishHealthPolls(SANDBOX_WAKE_ONLINE_GRACE_POLLS);
    }
    expect(result.current.sandboxStarting).toBe(true);
    expect(toast.error).not.toHaveBeenCalled();

    // `ready` clears the stage, and the host gets a full count to read online.
    act(() => useChatStore.setState({ sandboxStatus: null }));
    finishHealthPolls(SANDBOX_WAKE_ONLINE_GRACE_POLLS - 1);
    expect(result.current.sandboxStarting).toBe(true);
    expect(toast.error).not.toHaveBeenCalled();
    finishHealthPolls(1);
    expect(result.current.sandboxStarting).toBe(false);
    expect(toast.error).toHaveBeenCalledExactlyOnceWith(
      "The sandbox didn't come online. Try Reconnect host again.",
    );
  });

  it("sends a fresh wake after the stage limit even if the old answer never arrives", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(retrySession).mockReturnValueOnce(deferred<PostEventResponse>().promise);
      useChatStore.setState({ conversationId: "session", sandboxStatus: null });
      const { result } = renderHook(() =>
        useSessionReconnect({ ...asleepSandbox, hostOnline: false }),
      );
      await act(async () => {
        void result.current.reconnect();
      });
      act(() => useChatStore.setState({ sandboxStatus: { stage: "provisioning" } }));
      act(() => vi.advanceTimersByTime(SANDBOX_WAKE_STAGE_LIMIT_MS));
      expect(result.current.sandboxStarting).toBe(false);

      vi.mocked(retrySession).mockResolvedValue({ queued: false, recovered: true });
      await act(async () => {
        await result.current.reconnect();
      });
      expect(retrySession).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ["after the server answers", true],
    ["while the server hasn't answered", false],
  ] as const)(
    "lets a wake go quietly when its launch stage never finishes %s",
    async (_when, answered) => {
      vi.useFakeTimers();
      try {
        const pending = deferred<PostEventResponse>();
        vi.mocked(retrySession).mockReturnValue(pending.promise);
        useChatStore.setState({ conversationId: "session", sandboxStatus: null });
        const { result } = renderHook(() =>
          useSessionReconnect({ ...asleepSandbox, hostOnline: false }),
        );
        let reconnect!: Promise<void>;
        await act(async () => {
          reconnect = result.current.reconnect();
        });
        if (answered) {
          await act(async () => {
            pending.resolve({ queued: false, recovered: true });
            await reconnect;
          });
        }
        act(() => useChatStore.setState({ sandboxStatus: { stage: "provisioning" } }));
        act(() => vi.advanceTimersByTime(SANDBOX_WAKE_STAGE_LIMIT_MS / 2));
        // Moving on to another in-progress stage doesn't restart the limit.
        act(() => useChatStore.setState({ sandboxStatus: { stage: "connecting" } }));
        act(() => vi.advanceTimersByTime(SANDBOX_WAKE_STAGE_LIMIT_MS / 2 - 1));
        expect(result.current.sandboxStarting).toBe(true);
        act(() => vi.advanceTimersByTime(1));
        expect(result.current.sandboxStarting).toBe(false);
        expect(toast.error).not.toHaveBeenCalled();

        if (!answered) {
          // The late answer belongs to the released wake.
          await act(async () => {
            pending.resolve({ queued: false, recovered: true });
            await reconnect;
          });
          expect(result.current.sandboxStarting).toBe(false);
        }
        // Reconnect host works again.
        vi.mocked(retrySession).mockResolvedValue({ queued: false, recovered: true });
        await act(async () => {
          await result.current.reconnect();
        });
        expect(retrySession).toHaveBeenCalledTimes(2);
        expect(result.current.sandboxStarting).toBe(true);
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it("keeps the stage limit's deadline when the server answers partway through", async () => {
    vi.useFakeTimers();
    try {
      const pending = deferred<PostEventResponse>();
      vi.mocked(retrySession).mockReturnValue(pending.promise);
      useChatStore.setState({
        conversationId: "session",
        sandboxStatus: { stage: "provisioning" },
      });
      const { result } = renderHook(() =>
        useSessionReconnect({ ...asleepSandbox, hostOnline: false }),
      );
      let reconnect!: Promise<void>;
      await act(async () => {
        reconnect = result.current.reconnect();
      });
      act(() => vi.advanceTimersByTime(SANDBOX_WAKE_STAGE_LIMIT_MS - 60_000));
      await act(async () => {
        pending.resolve({ queued: false, recovered: true });
        await reconnect;
      });
      expect(result.current.sandboxStarting).toBe(true);
      act(() => vi.advanceTimersByTime(60_000));
      expect(result.current.sandboxStarting).toBe(false);
      expect(toast.error).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("drops the stage limit once the launch stops reporting progress", async () => {
    vi.useFakeTimers();
    try {
      useChatStore.setState({
        conversationId: "session",
        sandboxStatus: { stage: "provisioning" },
      });
      vi.mocked(retrySession).mockResolvedValue({ queued: false, recovered: true });
      const { result } = renderHook(() =>
        useSessionReconnect({ ...asleepSandbox, hostOnline: false }),
      );
      await act(async () => {
        await result.current.reconnect();
      });
      act(() => useChatStore.setState({ sandboxStatus: null }));
      act(() => vi.advanceTimersByTime(SANDBOX_WAKE_STAGE_LIMIT_MS * 2));
      // Only the health-poll count ends it from here.
      expect(result.current.sandboxStarting).toBe(true);
      finishHealthPolls(SANDBOX_WAKE_ONLINE_GRACE_POLLS);
      expect(result.current.sandboxStarting).toBe(false);
      expect(toast.error).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  const timedOut = { stage: "failed", error: "Sandbox launch timed out" } as const;
  const approve = {
    stage: "action_required",
    details: "Approve the sandbox in its console",
  } as const;

  it.each([
    ["a user action", timedOut, [approve]],
    ["the same failure again", timedOut, [{ stage: "provisioning" }, timedOut]],
    ["the same user action again", approve, [{ stage: "provisioning" }, approve]],
  ] as const)(
    "ends a sandbox wake past an earlier stop when it stops on %s, and wakes again",
    async (_stop, before, stages) => {
      useChatStore.setState({ conversationId: "session", sandboxStatus: before });
      vi.mocked(retrySession).mockResolvedValue({ queued: false, recovered: true });
      const { result } = renderHook(() =>
        useSessionReconnect({ ...asleepSandbox, hostOnline: false }),
      );
      await act(async () => {
        await result.current.reconnect();
      });
      expect(result.current.sandboxStarting).toBe(true);

      for (const sandboxStatus of stages) act(() => useChatStore.setState({ sandboxStatus }));
      expect(result.current.sandboxStarting).toBe(false);
      // The stopped wake leaves no count behind.
      finishHealthPolls(SANDBOX_WAKE_ONLINE_GRACE_POLLS);
      expect(toast.error).not.toHaveBeenCalled();

      await act(async () => {
        await result.current.reconnect();
      });
      expect(retrySession).toHaveBeenCalledTimes(2);
      expect(result.current.sandboxStarting).toBe(true);
      finishHealthPolls(SANDBOX_WAKE_ONLINE_GRACE_POLLS);
      expect(toast.error).toHaveBeenCalledExactlyOnceWith(
        "The sandbox didn't come online. Try Reconnect host again.",
      );
    },
  );

  it("ends a pending sandbox wake when its launch fails, ignoring the late answer", async () => {
    const pending = deferred<PostEventResponse>();
    vi.mocked(retrySession).mockReturnValue(pending.promise);
    useChatStore.setState({ conversationId: "session", sandboxStatus: { stage: "provisioning" } });
    const { result } = renderHook(() =>
      useSessionReconnect({ ...asleepSandbox, hostOnline: false }),
    );
    let reconnect!: Promise<void>;
    await act(async () => {
      reconnect = result.current.reconnect();
    });
    act(() => useChatStore.setState({ sandboxStatus: timedOut }));
    expect(result.current.sandboxStarting).toBe(false);

    await act(async () => {
      pending.resolve({ queued: false, recovered: true });
      await reconnect;
    });
    expect(result.current.sandboxStarting).toBe(false);
    expect(refreshRunnerHealth).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it.each([
    ["failure", timedOut],
    ["user action", approve],
  ] as const)(
    "keeps a pending sandbox wake when a snapshot rebuilds the earlier %s",
    async (_stop, before) => {
      const pending = deferred<PostEventResponse>();
      vi.mocked(retrySession).mockReturnValue(pending.promise);
      useChatStore.setState({ conversationId: "session", sandboxStatus: before });
      const { result } = renderHook(() =>
        useSessionReconnect({ ...asleepSandbox, hostOnline: false }),
      );
      let reconnect!: Promise<void>;
      await act(async () => {
        reconnect = result.current.reconnect();
      });
      act(() => useChatStore.setState({ sandboxStatus: { ...before } }));
      expect(result.current.sandboxStarting).toBe(true);

      await act(async () => {
        pending.resolve({ queued: false, recovered: true });
        await reconnect;
      });
      expect(result.current.sandboxStarting).toBe(true);
      expect(refreshRunnerHealth).toHaveBeenCalledOnce();
    },
  );

  it("shows why a sandbox wake failed and lets the next action retry it", async () => {
    vi.mocked(retrySession).mockRejectedValueOnce(new Error("managed host wake timed out"));
    vi.mocked(retrySession).mockResolvedValueOnce({ queued: false, recovered: true });
    const { result } = renderHook(() =>
      useSessionReconnect({ ...asleepSandbox, hostOnline: false }),
    );
    await act(async () => {
      await result.current.reconnect();
    });
    expect(toast.error).toHaveBeenCalledExactlyOnceWith(
      "Couldn't reconnect the sandbox: managed host wake timed out",
    );
    expect(result.current.sandboxStarting).toBe(false);
    expect(refreshRunnerHealth).not.toHaveBeenCalled();
    expect(result.current.dialogOpen).toBe(false);

    await act(async () => {
      await result.current.reconnect();
    });
    expect(retrySession).toHaveBeenCalledTimes(2);
    expect(result.current.sandboxStarting).toBe(true);
    expect(toast.loading).not.toHaveBeenCalled();
  });

  it.each(["resolves", "rejects"] as const)(
    "shares one wake request that %s between Reconnect host and other callers",
    async (outcome) => {
      const pending = deferred<PostEventResponse>();
      vi.mocked(retrySession).mockReturnValue(pending.promise);
      const { result } = renderHook(() =>
        useSessionReconnect({ ...asleepSandbox, hostOnline: false }),
      );
      let reconnect!: Promise<void>;
      let joined!: Promise<PostEventResponse>;
      await act(async () => {
        reconnect = result.current.reconnect();
        joined = wakeSandboxSession("session");
      });
      expect(retrySession).toHaveBeenCalledOnce();

      await act(async () => {
        if (outcome === "resolves") pending.resolve({ queued: false, recovered: true });
        else pending.reject(new Error("managed host wake timed out"));
        await reconnect;
      });
      // The joining caller gets the same answer.
      if (outcome === "resolves") await expect(joined).resolves.toMatchObject({ recovered: true });
      else await expect(joined).rejects.toThrow("managed host wake timed out");

      // Once answered, a new wake sends a new request.
      vi.mocked(retrySession).mockResolvedValue({ queued: false, recovered: true });
      await act(async () => {
        await wakeSandboxSession("session");
      });
      expect(retrySession).toHaveBeenCalledTimes(2);
    },
  );

  it("counts a launch the server reports in progress as starting", () => {
    useChatStore.setState({ conversationId: "session", sandboxStatus: { stage: "provisioning" } });
    const { result, rerender } = renderHook(() => useSandboxStarting("session"));
    // A wake from before a session switch, another tab, or a sent message.
    expect(result.current).toBe(true);
    act(() => useChatStore.setState({ sandboxStatus: { stage: "failed", error: "x" } }));
    rerender();
    expect(result.current).toBe(false);
  });

  it("tells other panels while Reconnect host wakes the shown session's sandbox", async () => {
    vi.mocked(retrySession).mockReturnValue(deferred<PostEventResponse>().promise);
    const { result, rerender } = renderHook(
      (props: typeof asleepSandbox) => ({
        reconnect: useSessionReconnect({ ...props, hostOnline: false }),
        starting: useSandboxStarting("session"),
      }),
      { initialProps: asleepSandbox },
    );
    expect(result.current.starting).toBe(false);
    await act(async () => {
      void result.current.reconnect.reconnect();
    });
    expect(result.current.starting).toBe(true);
    // Wake view state belongs to the shown session.
    rerender({ ...asleepSandbox, sessionId: "other-session", hostId: "other-host" });
    expect(result.current.starting).toBe(false);
  });

  it.each(["resolves", "rejects"] as const)(
    "ignores a sandbox wake that %s after navigating to another session",
    async (outcome) => {
      const pending = deferred<PostEventResponse>();
      vi.mocked(retrySession).mockReturnValue(pending.promise);
      const { result, rerender } = renderHook(useSessionReconnect, { initialProps: asleepSandbox });
      let reconnect!: Promise<void>;
      await act(async () => {
        reconnect = result.current.reconnect();
      });
      expect(result.current.sandboxStarting).toBe(true);
      rerender({ ...asleepSandbox, sessionId: "other-session", hostId: "other-host" });
      expect(result.current.sandboxStarting).toBe(false);

      await act(async () => {
        if (outcome === "resolves") pending.resolve({ queued: false, recovered: true });
        else pending.reject(new Error("managed host wake timed out"));
        await reconnect;
      });
      expect(result.current.sandboxStarting).toBe(false);
      expect(refreshRunnerHealth).not.toHaveBeenCalled();
      expect(toast.error).not.toHaveBeenCalled();
    },
  );

  it.each([
    { label: "browser", desktop: false, hostId: "this-mac", isOwner: true },
    { label: "non-owner", desktop: true, hostId: "this-mac", isOwner: false },
    { label: "unbound session", desktop: true, hostId: null, isOwner: true },
  ])("uses the dialog for a $label without querying the desktop", async ({ desktop, ...props }) => {
    vi.mocked(isElectronShell).mockReturnValue(desktop);
    const { result } = renderHook(() => useSessionReconnect({ ...localSession, ...props }));
    await act(async () => {
      await result.current.reconnect();
    });
    expect(result.current.dialogOpen).toBe(true);
    expect(getHostIdentity).not.toHaveBeenCalled();
    expect(controlHost).not.toHaveBeenCalled();
  });

  it.each([
    { failure: { ok: false, authError: true }, message: /finish signing in/i },
    { failure: { ok: false }, message: /Try again or run the command/ },
    { failure: { ok: false, error: "Connection timed out" }, message: /Connection timed out/ },
  ])(
    "opens recovery on failure and lets a retry succeed: $failure",
    async ({ failure, message }) => {
      vi.mocked(controlHost).mockResolvedValueOnce(failure);
      const { result } = renderHook(() => useSessionReconnect(localSession));
      await act(async () => {
        await result.current.reconnect();
      });
      expect(result.current.dialogOpen).toBe(true);
      expect(result.current.localReconnect?.error).toMatch(message);
      expect(toast.success).not.toHaveBeenCalled();
      await act(async () => {
        await result.current.reconnect();
      });
      expect(result.current.dialogOpen).toBe(false);
      expect(result.current.localReconnect?.error).toBeNull();
      expect(controlHost).toHaveBeenCalledTimes(2);
      expect(toast.success).toHaveBeenCalledOnce();
    },
  );

  it("rechecks the machine identity before retrying", async () => {
    vi.mocked(controlHost).mockResolvedValue({ ok: false });
    const { result } = renderHook(() => useSessionReconnect(localSession));
    await act(async () => {
      await result.current.reconnect();
    });
    vi.mocked(getHostIdentity).mockResolvedValue({ cliInstalled: true, hostId: "new-host-id" });
    await act(async () => {
      await result.current.reconnect();
    });
    expect(controlHost).toHaveBeenCalledOnce();
    expect(result.current.localReconnect).toBeUndefined();
    expect(result.current.dialogOpen).toBe(true);
  });

  it("does not start a host after navigating away during identity lookup", async () => {
    const identity = deferred<HostIdentity>();
    vi.mocked(getHostIdentity).mockReturnValue(identity.promise);
    const { result, rerender } = renderHook(useSessionReconnect, { initialProps: localSession });
    let reconnect!: Promise<void>;
    await act(async () => {
      reconnect = result.current.reconnect();
    });
    rerender({ ...localSession, sessionId: "other-session" });
    await act(async () => {
      identity.resolve({ cliInstalled: true, hostId: "this-mac" });
      await reconnect;
    });
    expect(controlHost).not.toHaveBeenCalled();
    expect(result.current.dialogOpen).toBe(false);
  });

  it("opens another host's recovery while the previous session is still reconnecting", async () => {
    const start = deferred<HostActionResult>();
    vi.mocked(controlHost).mockReturnValue(start.promise);
    const { result, rerender } = renderHook(useSessionReconnect, { initialProps: localSession });
    let first!: Promise<void>;
    await act(async () => {
      first = result.current.reconnect();
    });

    rerender({ ...localSession, sessionId: "other-session", hostId: "other-host" });
    expect(toast.dismiss).toHaveBeenCalledWith("progress");
    await act(async () => {
      await result.current.reconnect();
    });
    expect(result.current.dialogOpen).toBe(true);
    expect(result.current.localReconnect).toBeUndefined();
    expect(controlHost).toHaveBeenCalledOnce();

    await act(async () => {
      start.resolve({ ok: true });
      await first;
    });
    expect(result.current.dialogOpen).toBe(true);
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("does not let an old reconnect clear a newer session's progress or lock", async () => {
    const oldStart = deferred<HostActionResult>();
    const newStart = deferred<HostActionResult>();
    vi.mocked(controlHost)
      .mockReturnValueOnce(oldStart.promise)
      .mockReturnValueOnce(newStart.promise);
    vi.mocked(toast.loading)
      .mockReturnValueOnce("old-progress")
      .mockReturnValueOnce("new-progress");
    const { result, rerender } = renderHook(useSessionReconnect, { initialProps: localSession });
    let first!: Promise<void>;
    let second!: Promise<void>;
    await act(async () => {
      first = result.current.reconnect();
    });
    rerender({ ...localSession, sessionId: "other-session" });
    await act(async () => {
      second = result.current.reconnect();
    });
    expect(controlHost).toHaveBeenCalledTimes(2);
    expect(toast.dismiss).toHaveBeenCalledExactlyOnceWith("old-progress");

    await act(async () => {
      oldStart.resolve({ ok: false, error: "Old failure" });
      await first;
      await result.current.reconnect();
    });
    expect(controlHost).toHaveBeenCalledTimes(2);
    expect(result.current.localReconnect?.reconnecting).toBe(true);
    expect(result.current.localReconnect?.error).toBeNull();
    expect(result.current.dialogOpen).toBe(false);
    expect(toast.dismiss).not.toHaveBeenCalledWith("new-progress");

    await act(async () => {
      newStart.resolve({ ok: true });
      await second;
    });
    expect(result.current.localReconnect?.reconnecting).toBe(false);
    expect(toast.dismiss).toHaveBeenCalledWith("new-progress");
    expect(toast.success).toHaveBeenCalledOnce();
  });
});
