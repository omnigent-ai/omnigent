import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, wakeSession } from "@/lib/sessionsApi";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import type * as LivenessModule from "@/hooks/useSessionLiveness";
import type { PostEventResponse } from "@/lib/sessionsApi";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import {
  resetSandboxWakesForTesting,
  SANDBOX_WAKE_OPEN_DWELL_MS,
  SANDBOX_WAKE_REQUEST_MS,
  SANDBOX_WAKE_SETTLE_MS,
  endPassiveSandboxWake,
  RECONNECT_RECOVERY_HELD_LABEL_KEY,
  useSandboxPassivelyWoken,
  useSandboxWake,
  useSandboxWakeOnSessionOpen,
  useSandboxWakeWhenOpened,
} from "./useSandboxAutoWake";

// What the session's hooks report, per test.
const state: {
  liveness: SessionLiveness["kind"];
  runnerOnline: boolean | undefined;
  permissionLevel: number | null | undefined;
  eventTypes: string[] | "loading";
  labels: Record<string, string>;
} = {
  liveness: "host_asleep",
  runnerOnline: false,
  permissionLevel: 2,
  eventTypes: [],
  labels: {},
};

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  wakeSession: vi.fn(),
}));
vi.mock("@/hooks/useSessionLiveness", async (importOriginal) => ({
  ...(await importOriginal<typeof LivenessModule>()),
  useSessionLiveness: () => ({ kind: state.liveness }),
}));
// False: the session snapshot hasn't loaded.
let sessionLoaded = true;
vi.mock("@/hooks/useSession", () => ({
  useSession: (id: string | undefined) => ({
    // Like production, a loading snapshot is null.
    session:
      id && sessionLoaded
        ? { id, permissionLevel: state.permissionLevel, labels: state.labels }
        : null,
  }),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useSessionActive: () => false,
  useWorkspaceAsleep: () => state.liveness === "host_asleep",
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: () => state.runnerOnline,
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () =>
    state.eventTypes === "loading" ? "loading" : { session_event_types: state.eventTypes },
}));

const wakeMock = vi.mocked(wakeSession);
const recovered: PostEventResponse = { queued: false, recovered: true };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function setVisibility(value: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  Object.assign(state, {
    liveness: "host_asleep",
    runnerOnline: false,
    permissionLevel: 2,
    eventTypes: ["retry_session", "wake_session"],
    labels: {},
  });
  sessionLoaded = true;
  wakeMock.mockReset();
  wakeMock.mockResolvedValue(recovered);
  setVisibility("visible");
});

afterEach(() => {
  cleanup();
  resetSandboxWakesForTesting();
  vi.useRealTimers();
});

describe("useSandboxWake", () => {
  it.each<[string, PostEventResponse]>([
    ["relaunches it", recovered],
    [
      "finds the runner already connected",
      { queued: false, recovered: false, recovery: "already_connected" },
    ],
  ])("wakes once when the server %s, and stays waking until the runner is up", async (_l, r) => {
    const request = deferred<PostEventResponse>();
    wakeMock.mockReturnValue(request.promise);
    const { result, rerender } = renderHook(() => useSandboxWake("conv_sleepy"));

    act(() => result.current.wakeIfAsleep());
    act(() => result.current.wakeIfAsleep());
    expect(wakeMock).toHaveBeenCalledTimes(1);
    expect(wakeMock).toHaveBeenCalledWith("conv_sleepy");
    expect(result.current.waking).toBe(true);

    await act(async () => request.resolve(r));
    expect(result.current.waking).toBe(true);

    state.runnerOnline = true;
    rerender();
    expect(result.current.waking).toBe(false);
  });

  it.each<[string, Partial<typeof state>]>([
    ["the sandbox isn't asleep", { liveness: "online" }],
    ["liveness is still unknown", { liveness: "unknown" }],
    ["the viewer can only read", { permissionLevel: 1 }],
    ["the permission level isn't known", { permissionLevel: undefined }],
    ["the server can't wake without resuming work", { eventTypes: ["retry_session"] }],
    ["server info is loading", { eventTypes: "loading" }],
  ])("does nothing when %s", (_label, overrides) => {
    Object.assign(state, overrides);
    const { result } = renderHook(() => useSandboxWake("conv_sleepy"));
    act(() => result.current.wakeIfAsleep());
    expect(wakeMock).not.toHaveBeenCalled();
    expect(result.current.waking).toBe(false);
  });

  it("ends quietly when the wake fails, so the next action can try again", async () => {
    wakeMock.mockRejectedValueOnce(new Error("resume failed"));
    const { result } = renderHook(() => useSandboxWake("conv_sleepy"));

    await act(async () => result.current.wakeIfAsleep());
    expect(result.current.waking).toBe(false);

    await act(async () => result.current.wakeIfAsleep());
    expect(wakeMock).toHaveBeenCalledTimes(2);
  });

  it("stops trying once a server rejects wake_session as unknown", async () => {
    wakeMock.mockRejectedValueOnce(
      new ApiError("Unknown event type: wake_session", 400, "invalid_input"),
    );
    const { result } = renderHook(() => useSandboxWake("conv_sleepy"));

    await act(async () => result.current.wakeIfAsleep());
    await act(async () => result.current.wakeIfAsleep());
    expect(wakeMock).toHaveBeenCalledTimes(1);
    expect(result.current.waking).toBe(false);
  });

  it.each([
    ["never answers", SANDBOX_WAKE_REQUEST_MS, false],
    ["answers but the runner never connects", SANDBOX_WAKE_SETTLE_MS, true],
  ])("stops waking when the server %s", async (_label, ms, answers) => {
    vi.useFakeTimers();
    const request = deferred<PostEventResponse>();
    wakeMock.mockReturnValue(request.promise);
    const { result } = renderHook(() => useSandboxWake("conv_sleepy"));

    act(() => result.current.wakeIfAsleep());
    if (answers) await act(async () => request.resolve(recovered));
    act(() => vi.advanceTimersByTime(ms - 1));
    expect(result.current.waking).toBe(true);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current.waking).toBe(false);
  });

  it.each([
    [
      "fails",
      (request: ReturnType<typeof deferred<PostEventResponse>>) => request.reject(new Error("x")),
    ],
    ["times out", () => vi.advanceTimersByTime(SANDBOX_WAKE_REQUEST_MS)],
  ])(
    "keeps automatic recovery off after a wake that %s, through a late-connecting runner",
    async (_label, end) => {
      vi.useFakeTimers();
      const request = deferred<PostEventResponse>();
      wakeMock.mockReturnValue(request.promise);
      const { result, rerender } = renderHook(() => ({
        wake: useSandboxWake("conv_sleepy"),
        passive: useSandboxPassivelyWoken("conv_sleepy"),
      }));
      act(() => result.current.wake.wakeIfAsleep());
      expect(result.current.passive).toBe(true);

      await act(async () => end(request));
      expect(result.current.wake.waking).toBe(false);
      expect(result.current.passive).toBe(true);

      // The runner the failed wake launched connects later.
      state.liveness = "online";
      state.runnerOnline = true;
      rerender();
      expect(result.current.passive).toBe(true);

      // Only explicit work ends it.
      act(() => endPassiveSandboxWake("conv_sleepy"));
      expect(result.current.passive).toBe(false);
    },
  );

  it("keeps automatic recovery off on a fresh page while the server holds recovery", () => {
    state.liveness = "online";
    state.runnerOnline = true;
    state.labels = { [RECONNECT_RECOVERY_HELD_LABEL_KEY]: "true" };
    const { result, rerender } = renderHook(() => useSandboxPassivelyWoken("conv_sleepy"));
    expect(result.current).toBe(true);

    // A message or Resume releases the hold server-side.
    state.labels = {};
    rerender();
    expect(result.current).toBe(false);
  });

  it("ends the passive wake when the sandbox sleeps again after coming online", () => {
    const { result, rerender } = renderHook(() => ({
      wake: useSandboxWake("conv_sleepy"),
      passive: useSandboxPassivelyWoken("conv_sleepy"),
    }));
    act(() => result.current.wake.wakeIfAsleep());
    state.liveness = "online";
    state.runnerOnline = true;
    rerender();
    expect(result.current.passive).toBe(true);

    state.liveness = "host_asleep";
    state.runnerOnline = false;
    rerender();
    expect(result.current.passive).toBe(false);
  });

  it("shares one wake across every view of the session", () => {
    const first = renderHook(() => useSandboxWake("conv_sleepy"));
    const second = renderHook(() => useSandboxWake("conv_sleepy"));

    act(() => first.result.current.wakeIfAsleep());
    act(() => second.result.current.wakeIfAsleep());
    expect(wakeMock).toHaveBeenCalledTimes(1);
    expect(second.result.current.waking).toBe(true);
  });
});

describe("useSandboxWakeWhenOpened", () => {
  function renderOpened(initial: { open: boolean; id: string }, mountIsOpen?: boolean) {
    const wake = vi.fn();
    const hook = renderHook(
      ({ open, id }: { open: boolean; id: string }) =>
        useSandboxWakeWhenOpened(open, id, wake, { mountIsOpen }),
      { initialProps: initial },
    );
    return { wake, ...hook };
  }

  it("wakes when the view opens for the same session", () => {
    const { wake, rerender } = renderOpened({ open: false, id: "a" });
    expect(wake).not.toHaveBeenCalled();
    rerender({ open: true, id: "a" });
    expect(wake).toHaveBeenCalledTimes(1);
    rerender({ open: true, id: "a" });
    expect(wake).toHaveBeenCalledTimes(1);
  });

  it.each([
    [
      "it is already open when the session changes",
      { open: true, id: "a" },
      { open: true, id: "b" },
    ],
    ["it opens as the session changes", { open: false, id: "a" }, { open: true, id: "b" }],
  ])("doesn't wake when %s", (_label, initial, next) => {
    const { wake, rerender } = renderOpened(initial);
    rerender(next);
    expect(wake).not.toHaveBeenCalled();
  });

  it("counts mounting open only for a view that mounts when opened", () => {
    expect(renderOpened({ open: true, id: "a" }).wake).not.toHaveBeenCalled();
    expect(renderOpened({ open: true, id: "a" }, true).wake).toHaveBeenCalledTimes(1);
  });
});

describe("useSandboxWakeOnSessionOpen", () => {
  function renderOpen(id: string) {
    return renderHook(({ id: current }: { id: string }) => useSandboxWakeOnSessionOpen(current), {
      initialProps: { id },
    });
  }

  it("wakes the opened session's sandbox after the dwell", () => {
    vi.useFakeTimers();
    renderOpen("conv_sleepy");
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_OPEN_DWELL_MS - 1));
    expect(wakeMock).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(wakeMock).toHaveBeenCalledWith("conv_sleepy");
  });

  it("wakes none of the sessions passed over", () => {
    vi.useFakeTimers();
    const { rerender } = renderOpen("conv_a");
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_OPEN_DWELL_MS / 2));
    rerender({ id: "conv_b" });
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_OPEN_DWELL_MS / 2));
    rerender({ id: "conv_c" });
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_OPEN_DWELL_MS));
    expect(wakeMock).toHaveBeenCalledTimes(1);
    expect(wakeMock).toHaveBeenCalledWith("conv_c");
  });

  it("waits for liveness when it resolves after the dwell", () => {
    vi.useFakeTimers();
    state.liveness = "unknown";
    const { rerender } = renderOpen("conv_sleepy");
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_OPEN_DWELL_MS));
    expect(wakeMock).not.toHaveBeenCalled();
    state.liveness = "host_asleep";
    rerender({ id: "conv_sleepy" });
    expect(wakeMock).toHaveBeenCalledTimes(1);
  });

  it.each<[string, Partial<typeof state>]>([
    ["server capabilities", { eventTypes: "loading" }],
    // Sidebar health alone already resolves liveness (as stranded) before the snapshot.
    ["the session", { permissionLevel: undefined, liveness: "local_stranded" }],
  ])("waits for %s that load after the dwell", (_label, loading) => {
    vi.useFakeTimers();
    const loaded = {
      eventTypes: state.eventTypes,
      permissionLevel: state.permissionLevel,
      liveness: state.liveness,
    };
    Object.assign(state, loading);
    if ("permissionLevel" in loading) sessionLoaded = false;
    const { rerender } = renderOpen("conv_sleepy");
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_OPEN_DWELL_MS));
    expect(wakeMock).not.toHaveBeenCalled();

    Object.assign(state, loaded);
    sessionLoaded = true;
    rerender({ id: "conv_sleepy" });
    expect(wakeMock).toHaveBeenCalledTimes(1);
  });

  it("leaves a session alone that falls asleep while it stays open", () => {
    vi.useFakeTimers();
    state.liveness = "online";
    const { rerender } = renderOpen("conv_sleepy");
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_OPEN_DWELL_MS));
    state.liveness = "host_asleep";
    rerender({ id: "conv_sleepy" });
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_OPEN_DWELL_MS * 10));
    expect(wakeMock).not.toHaveBeenCalled();
  });

  it("dwells from when a background tab is shown", () => {
    vi.useFakeTimers();
    setVisibility("hidden");
    renderOpen("conv_sleepy");
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_OPEN_DWELL_MS * 5));
    expect(wakeMock).not.toHaveBeenCalled();

    act(() => setVisibility("visible"));
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_OPEN_DWELL_MS));
    expect(wakeMock).toHaveBeenCalledTimes(1);
  });
});
