import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { retrySession } from "@/lib/sessionsApi";
import type { PostEventResponse } from "@/lib/sessionsApi";
import {
  resetSandboxWakesForTesting,
  SANDBOX_WAKE_SETTLE_MS,
  useSandboxAutoWake,
} from "./useSandboxAutoWake";

vi.mock("@/lib/sessionsApi", () => ({ retrySession: vi.fn() }));

const retryMock = vi.mocked(retrySession);
const recovered: PostEventResponse = { queued: false, recovered: true };

type Props = Parameters<typeof useSandboxAutoWake>[0];
const asleepAndOpen: Props = {
  conversationId: "conv_sleepy",
  asleep: true,
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
  retryMock.mockReset();
  retryMock.mockResolvedValue(recovered);
});

afterEach(() => {
  cleanup();
  resetSandboxWakesForTesting();
  vi.useRealTimers();
});

describe("useSandboxAutoWake", () => {
  it("wakes an asleep sandbox once when the view opens and waits for it to come back", async () => {
    const request = deferred<PostEventResponse>();
    retryMock.mockReturnValue(request.promise);
    const { result, rerender } = renderWake(asleepAndOpen);

    expect(retryMock).toHaveBeenCalledTimes(1);
    expect(retryMock).toHaveBeenCalledWith("conv_sleepy", { wakeOnly: true });
    expect(result.current.phase).toBe("waking");

    await act(async () => request.resolve(recovered));
    // Liveness hasn't caught up yet: still starting, and no second request.
    expect(result.current.phase).toBe("waking");
    rerender({ ...asleepAndOpen });
    expect(retryMock).toHaveBeenCalledTimes(1);

    rerender({ ...asleepAndOpen, ...online });
    expect(result.current.phase).toBe("idle");
  });

  it("waits for liveness when the server finds the runner already connected", async () => {
    retryMock.mockResolvedValue({ queued: false, recovered: false, recovery: "already_connected" });
    const { result, rerender } = renderWake(asleepAndOpen);
    await act(async () => {});

    expect(result.current.phase).toBe("waking");
    rerender({ ...asleepAndOpen, ...online });
    expect(result.current.phase).toBe("idle");
  });

  it("ignores a pending wake's outcome after switching sessions", async () => {
    const first = deferred<PostEventResponse>();
    const second = deferred<PostEventResponse>();
    retryMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result, rerender } = renderWake(asleepAndOpen);

    await act(async () => rerender({ ...asleepAndOpen, conversationId: "conv_other" }));
    expect(retryMock).toHaveBeenLastCalledWith("conv_other", { wakeOnly: true });
    await act(async () => first.reject(new Error("old session failed")));
    expect(result.current.phase).toBe("waking");
    expect(result.current.error).toBeNull();

    await act(async () => second.resolve(recovered));
    rerender({ ...asleepAndOpen, conversationId: "conv_other", ...online });
    expect(result.current.phase).toBe("idle");
    expect(retryMock).toHaveBeenCalledTimes(2);
  });

  it("joins a wake that answered while hidden instead of waking again on reopen", async () => {
    const request = deferred<PostEventResponse>();
    retryMock.mockReturnValueOnce(request.promise);
    const { result, rerender } = renderWake(asleepAndOpen);

    rerender({ ...asleepAndOpen, active: false });
    await act(async () => request.resolve(recovered));

    // Still asleep on reopen: liveness hasn't caught up with that wake yet.
    await act(async () => rerender(asleepAndOpen));
    expect(retryMock).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("waking");
  });

  it("shows the failure of a wake that fails after switching away and back", async () => {
    const request = deferred<PostEventResponse>();
    retryMock.mockReturnValueOnce(request.promise);
    const { result, rerender } = renderWake(asleepAndOpen);

    rerender({ ...asleepAndOpen, conversationId: "conv_awake", ...online });
    await act(async () => rerender(asleepAndOpen));
    expect(retryMock).toHaveBeenCalledTimes(1);
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
    expect(retryMock).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("waking");

    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_SETTLE_MS / 2));
    expect(result.current.phase).toBe("failed");
    expect(retryMock).toHaveBeenCalledTimes(1);
  });

  it("does not wake again when reopened while the earlier wake is still settling", async () => {
    vi.useFakeTimers();
    const { result, rerender } = renderWake(asleepAndOpen);
    await act(async () => {});
    expect(result.current.phase).toBe("waking");

    rerender({ ...asleepAndOpen, active: false });
    await act(async () => rerender(asleepAndOpen));
    expect(retryMock).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("waking");

    // The reopening joined that wake, so its timeout doesn't trigger another.
    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_SETTLE_MS));
    expect(result.current.phase).toBe("failed");
    expect(retryMock).toHaveBeenCalledTimes(1);
  });

  it.each<[string, Partial<Props>]>([
    ["hidden", { active: false }],
    ["awake", online],
    ["read-only", { canWake: false }],
  ])("does not wake when the view is %s", (_label, override) => {
    const { result } = renderWake({ ...asleepAndOpen, ...override });

    expect(retryMock).not.toHaveBeenCalled();
    expect(result.current.phase).toBe("idle");
  });

  it("wakes when a hidden pre-mounted view becomes visible", async () => {
    const { rerender } = renderWake({ ...asleepAndOpen, active: false });
    expect(retryMock).not.toHaveBeenCalled();

    await act(async () => rerender(asleepAndOpen));
    expect(retryMock).toHaveBeenCalledTimes(1);
  });

  it("does not wake a sandbox that idles back to sleep while the view stays open", async () => {
    const { result, rerender } = renderWake(asleepAndOpen);
    await act(async () => {});
    rerender({ ...asleepAndOpen, ...online });
    expect(result.current.phase).toBe("idle");

    rerender(asleepAndOpen);
    expect(retryMock).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("idle");

    // Reopening the view is a new opening and wakes it again.
    rerender({ ...asleepAndOpen, active: false });
    await act(async () => rerender(asleepAndOpen));
    expect(retryMock).toHaveBeenCalledTimes(2);
  });

  it("does not wake a sandbox that was awake when the view opened", async () => {
    const { result, rerender } = renderWake({ ...asleepAndOpen, ...online });
    await act(async () => rerender(asleepAndOpen));

    expect(retryMock).not.toHaveBeenCalled();
    expect(result.current.phase).toBe("idle");
  });

  it("reports a failed wake without retrying, and wakes again on request", async () => {
    retryMock.mockRejectedValueOnce(new Error("Sandbox resume timed out"));
    const { result, rerender } = renderWake(asleepAndOpen);
    await act(async () => {});

    expect(result.current.phase).toBe("failed");
    expect(result.current.error).toBe("Couldn't start the sandbox: Sandbox resume timed out");
    rerender({ ...asleepAndOpen });
    expect(retryMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.wake();
    });
    expect(retryMock).toHaveBeenCalledTimes(2);
    expect(result.current.phase).toBe("waking");
    expect(result.current.error).toBeNull();
  });

  it("fails when the sandbox stays asleep after the server reports the wake", async () => {
    vi.useFakeTimers();
    const { result } = renderWake(asleepAndOpen);
    await act(async () => {});
    expect(result.current.phase).toBe("waking");

    act(() => vi.advanceTimersByTime(SANDBOX_WAKE_SETTLE_MS));
    expect(result.current.phase).toBe("failed");
    expect(result.current.error).toBe("The sandbox didn't come back online.");
  });

  it("starts fresh for another session in the same view", async () => {
    retryMock.mockRejectedValueOnce(new Error("boom"));
    const { result, rerender } = renderWake(asleepAndOpen);
    await act(async () => {});
    expect(result.current.phase).toBe("failed");

    await act(async () => rerender({ ...asleepAndOpen, conversationId: "conv_other" }));
    expect(retryMock).toHaveBeenLastCalledWith("conv_other", { wakeOnly: true });
    expect(result.current.phase).toBe("waking");
  });
});
