import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, wakeSession } from "@/lib/sessionsApi";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import type { PostEventResponse } from "@/lib/sessionsApi";
import {
  resetSandboxWakesForTesting,
  SANDBOX_WAKE_REQUEST_MS,
  SANDBOX_WAKE_SETTLE_MS,
  useSandboxAutoWake,
} from "./useSandboxAutoWake";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  wakeSession: vi.fn(),
}));

const wakeMock = vi.mocked(wakeSession);
const recovered: PostEventResponse = { queued: false, recovered: true };

type Props = Parameters<typeof useSandboxAutoWake>[0];
const asleepAndOpen: Props = {
  conversationId: "conv_sleepy",
  asleep: true,
  needsSandbox: true,
  awake: false,
  active: true,
  canWake: true,
};
const online: Partial<Props> = { asleep: false, awake: true };

function renderWake(initial: Props) {
  return renderHook((props: Props) => useSandboxAutoWake(props), { initialProps: initial });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  wakeMock.mockReset();
  wakeMock.mockResolvedValue(recovered);
});

afterEach(() => {
  cleanup();
  resetSandboxWakesForTesting();
  vi.useRealTimers();
});

describe("useSandboxAutoWake", () => {
  it.each<[string, PostEventResponse]>([
    ["relaunches it", recovered],
    [
      "finds the runner already connected",
      { queued: false, recovered: false, recovery: "already_connected" },
    ],
  ])(
    "wakes an asleep sandbox once when the server %s, and waits for it to come back",
    async (_label, response) => {
      const request = deferred<PostEventResponse>();
      wakeMock.mockReturnValue(request.promise);
      const { result, rerender } = renderWake(asleepAndOpen);

      expect(wakeMock).toHaveBeenCalledTimes(1);
      expect(wakeMock).toHaveBeenCalledWith("conv_sleepy");
      expect(result.current.phase).toBe("waking");

      await act(async () => request.resolve(response));
      // Liveness hasn't caught up yet: still starting, and no second request.
      expect(result.current.phase).toBe("waking");
      rerender({ ...asleepAndOpen });
      expect(wakeMock).toHaveBeenCalledTimes(1);

      rerender({ ...asleepAndOpen, ...online });
      expect(result.current.phase).toBe("idle");
    },
  );

  it.each(["pending", "failed"])(
    "starts fresh for another session while the old wake is %s",
    async (oldWake) => {
      const first = deferred<PostEventResponse>();
      const second = deferred<PostEventResponse>();
      wakeMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
      const { result, rerender } = renderWake(asleepAndOpen);
      if (oldWake === "failed") {
        await act(async () => first.reject(new Error("old session failed")));
        expect(result.current.phase).toBe("failed");
      }

      await act(async () => rerender({ ...asleepAndOpen, conversationId: "conv_other" }));
      expect(wakeMock).toHaveBeenLastCalledWith("conv_other");
      expect(result.current.phase).toBe("waking");
      if (oldWake === "pending") {
        await act(async () => first.reject(new Error("old session failed")));
      }
      expect(result.current.phase).toBe("waking");
      expect(result.current.error).toBeNull();

      await act(async () => second.resolve(recovered));
      rerender({ ...asleepAndOpen, conversationId: "conv_other", ...online });
      expect(result.current.phase).toBe("idle");
      expect(wakeMock).toHaveBeenCalledTimes(2);
    },
  );

  it.each([
    ["answered while hidden", true],
    ["answered before hiding", false],
  ])(
    "joins a wake that %s instead of waking again on reopen",
    async (_label, answerWhileHidden) => {
      vi.useFakeTimers();
      const request = deferred<PostEventResponse>();
      wakeMock.mockReturnValueOnce(request.promise);
      const { result, rerender } = renderWake(asleepAndOpen);

      if (!answerWhileHidden) await act(async () => request.resolve(recovered));
      rerender({ ...asleepAndOpen, active: false });
      if (answerWhileHidden) await act(async () => request.resolve(recovered));

      // Still asleep on reopen: liveness hasn't caught up with that wake yet.
      await act(async () => rerender(asleepAndOpen));
      expect(wakeMock).toHaveBeenCalledTimes(1);
      expect(result.current.phase).toBe("waking");

      // The reopening joined that wake, so its timeout doesn't trigger another.
      act(() => vi.advanceTimersByTime(SANDBOX_WAKE_SETTLE_MS));
      expect(result.current.phase).toBe("failed");
      expect(wakeMock).toHaveBeenCalledTimes(1);
    },
  );

  it("shows the failure of a wake that fails after switching away and back", async () => {
    const request = deferred<PostEventResponse>();
    wakeMock.mockReturnValueOnce(request.promise);
    const { result, rerender } = renderWake(asleepAndOpen);

    rerender({ ...asleepAndOpen, conversationId: "conv_awake", ...online });
    await act(async () => rerender(asleepAndOpen));
    expect(wakeMock).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("waking");

    await act(async () => request.reject(new Error("Sandbox resume timed out")));
    expect(result.current.phase).toBe("failed");
    expect(result.current.error).toBe("Couldn't start the sandbox: Sandbox resume timed out");
  });

  it("joins the wake and keeps its deadline when the view remounts", async () => {
    vi.useFakeTimers();
    const first = renderWake(asleepAndOpen);
    await act(async () => {});
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_SETTLE_MS / 2));
    first.unmount();

    const { result } = renderWake(asleepAndOpen);
    await act(async () => {});
    expect(wakeMock).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("waking");

    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_SETTLE_MS / 2));
    expect(result.current.phase).toBe("failed");
    expect(wakeMock).toHaveBeenCalledTimes(1);
  });

  it.each<[string, Partial<Props>]>([
    ["read-only", { canWake: false }],
    ["showing a cached list", { needsSandbox: false }],
  ])("does not wake when the view is %s", (_label, override) => {
    const { result } = renderWake({ ...asleepAndOpen, ...override });

    expect(wakeMock).not.toHaveBeenCalled();
    expect(result.current.phase).toBe("idle");
  });

  it("wakes when a hidden pre-mounted view becomes visible", async () => {
    const { rerender } = renderWake({ ...asleepAndOpen, active: false });
    expect(wakeMock).not.toHaveBeenCalled();

    await act(async () => rerender(asleepAndOpen));
    expect(wakeMock).toHaveBeenCalledTimes(1);
  });

  it("does not wake a sandbox that idles back to sleep while the view stays open", async () => {
    const { result, rerender } = renderWake(asleepAndOpen);
    await act(async () => {});
    rerender({ ...asleepAndOpen, ...online });
    expect(result.current.phase).toBe("idle");

    rerender(asleepAndOpen);
    expect(wakeMock).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("idle");

    // Reopening the view is a new opening and wakes it again.
    rerender({ ...asleepAndOpen, active: false });
    await act(async () => rerender(asleepAndOpen));
    expect(wakeMock).toHaveBeenCalledTimes(2);
  });

  it("forgets a wake still in flight once the runner comes online", async () => {
    const request = deferred<PostEventResponse>();
    wakeMock.mockReturnValueOnce(request.promise);
    const { result, rerender } = renderWake(asleepAndOpen);
    rerender({ ...asleepAndOpen, ...online });
    expect(result.current.phase).toBe("idle");

    // Asleep again before the first request answers: Start sandbox sends a new wake.
    rerender(asleepAndOpen);
    expect(result.current.phase).toBe("idle");
    await act(async () => {
      await result.current.wake();
    });
    expect(wakeMock).toHaveBeenCalledTimes(2);
    await act(async () => request.reject(new Error("stale")));
    expect(result.current.phase).toBe("waking");
    expect(result.current.error).toBeNull();
  });

  it("does not wake a sandbox that was awake when the view opened", async () => {
    const { result, rerender } = renderWake({ ...asleepAndOpen, ...online });
    await act(async () => rerender(asleepAndOpen));

    expect(wakeMock).not.toHaveBeenCalled();
    expect(result.current.phase).toBe("idle");
  });

  it("reports a failed wake without retrying, and wakes again on request", async () => {
    wakeMock.mockRejectedValueOnce(new Error("Sandbox resume timed out"));
    const { result, rerender } = renderWake(asleepAndOpen);
    await act(async () => {});

    expect(result.current.phase).toBe("failed");
    expect(result.current.error).toBe("Couldn't start the sandbox: Sandbox resume timed out");
    rerender({ ...asleepAndOpen });
    expect(wakeMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.wake();
    });
    expect(wakeMock).toHaveBeenCalledTimes(2);
    expect(result.current.phase).toBe("waking");
    expect(result.current.error).toBeNull();
  });

  it.each(["settling", "failed"])(
    "keeps a %s wake while a cached list shows and the sandbox still sleeps",
    async (step) => {
      vi.useFakeTimers();
      const { result, rerender } = renderWake(asleepAndOpen);
      await act(async () => {});
      const elapsed = step === "failed" ? SANDBOX_WAKE_SETTLE_MS : SANDBOX_WAKE_SETTLE_MS / 2;
      act(() => vi.advanceTimersByTime(elapsed));
      const phase = result.current.phase;

      // Another view of the session, or a switch to a cached tab, can stop needing the sandbox.
      const cached = renderWake({ ...asleepAndOpen, needsSandbox: false });
      rerender({ ...asleepAndOpen, needsSandbox: false });
      expect(result.current.phase).toBe(phase);
      expect(cached.result.current.phase).toBe(phase);

      // The wake still fails at its original settle deadline.
      act(() => vi.advanceTimersByTime(SANDBOX_WAKE_SETTLE_MS - elapsed));
      for (const view of [result, cached.result]) {
        expect(view.current.phase).toBe("failed");
        expect(view.current.error).toBe("The sandbox didn't come back online.");
      }
      expect(wakeMock).toHaveBeenCalledTimes(1);
    },
  );

  it("stops waking for good when the server rejects the wake event", async () => {
    wakeMock.mockRejectedValueOnce(
      new ApiError("Unknown event type: 'wake_session'", 400, "invalid_input"),
    );
    const { result, rerender } = renderWake(asleepAndOpen);
    await act(async () => {});
    expect(result.current.phase).toBe("unsupported");
    expect(result.current.error).toBeNull();

    await act(async () => {
      await result.current.wake();
    });
    rerender({ ...asleepAndOpen, active: false });
    await act(async () => rerender(asleepAndOpen));
    expect(wakeMock).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("unsupported");
  });

  it("fails a wake whose request never answers, and wakes again on request", async () => {
    vi.useFakeTimers();
    wakeMock.mockReturnValueOnce(new Promise<PostEventResponse>(() => {}));
    const { result } = renderWake(asleepAndOpen);
    expect(result.current.phase).toBe("waking");

    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_REQUEST_MS));
    expect(result.current.phase).toBe("failed");
    expect(result.current.error).toBe("The sandbox didn't come back online.");

    await act(async () => {
      await result.current.wake();
    });
    expect(wakeMock).toHaveBeenCalledTimes(2);
    expect(result.current.phase).toBe("waking");
  });

  it.each<[string, Partial<Props>]>([
    ["stays asleep", {}],
    ["leaves sleep but its runner never comes online", { asleep: false }],
  ])("fails when the sandbox %s after the server reports the wake", async (_label, liveness) => {
    vi.useFakeTimers();
    const { result, rerender } = renderWake(asleepAndOpen);
    await act(async () => {});
    rerender({ ...asleepAndOpen, ...liveness });
    expect(result.current.phase).toBe("waking");

    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_SETTLE_MS));
    rerender(asleepAndOpen);
    expect(result.current.phase).toBe("failed");
    expect(result.current.error).toBe("The sandbox didn't come back online.");
    expect(wakeMock).toHaveBeenCalledTimes(1);
  });
});
