import type * as SessionsApiModule from "@/lib/sessionsApi";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import { refreshRunnerHealth } from "@/hooks/useRunnerHealth";
import { SANDBOX_RESUME_QUIET_LIMIT_MS, SANDBOX_RESUME_TIMED_OUT } from "@/lib/sandboxResume";
import { retrySession, type PostEventResponse } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";

import {
  observeSandboxHostHealth,
  observeSandboxLaunch,
  resetSandboxResumes,
  resumeSandbox,
  SANDBOX_RESUME_TICK_MS,
  useSandboxResumeStore,
} from "./sandboxResume";

// Spreads the real module: the chat store reads its other exports at import.
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  retrySession: vi.fn(),
}));
vi.mock("@/hooks/useRunnerHealth", () => ({ refreshRunnerHealth: vi.fn() }));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), dismiss: vi.fn() } }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const recovered: PostEventResponse = { queued: false, recovered: true };
const phase = (sessionId: string) => useSandboxResumeStore.getState().bySession[sessionId]?.phase;

beforeEach(() => {
  vi.mocked(retrySession).mockResolvedValue(recovered);
  useChatStore.setState({ conversationId: null, sandboxStatus: null });
});

afterEach(() => {
  resetSandboxResumes();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("resumeSandbox", () => {
  it("resumes once for every surface, and ends when the host reads online", async () => {
    const pending = deferred<PostEventResponse>();
    vi.mocked(retrySession).mockReturnValue(pending.promise);
    const fromBadge = resumeSandbox("s1");
    let terminalSettled = false;
    const fromTerminal = resumeSandbox("s1").then(() => {
      terminalSettled = true;
    });
    expect(phase("s1")).toBe("requesting");
    expect(retrySession).toHaveBeenCalledOnce();
    // A joining surface waits for the request's own answer.
    await Promise.resolve();
    expect(terminalSettled).toBe(false);

    pending.resolve(recovered);
    await Promise.all([fromBadge, fromTerminal]);
    expect(terminalSettled).toBe(true);
    expect(phase("s1")).toBe("awaiting_online");
    expect(refreshRunnerHealth).toHaveBeenCalledOnce();

    observeSandboxHostHealth(new Map([["s1", false]]));
    expect(phase("s1")).toBe("awaiting_online");
    observeSandboxHostHealth(new Map([["s1", true]]));
    expect(phase("s1")).toBeUndefined();
    expect(useSandboxResumeStore.getState().resumedAt.s1).toBeTypeOf("number");
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("keeps each session's resume separate", async () => {
    await resumeSandbox("s1");
    await resumeSandbox("s2");
    observeSandboxHostHealth(new Map([["s2", true]]));
    expect(phase("s1")).toBe("awaiting_online");
    expect(phase("s2")).toBeUndefined();
    expect(useSandboxResumeStore.getState().resumedAt.s1).toBeUndefined();
  });

  it("reports a failed request with Retry, which resumes again", async () => {
    vi.mocked(retrySession).mockRejectedValueOnce(new Error("managed host wake timed out"));
    await resumeSandbox("s1");
    const message = "Couldn't resume the sandbox: managed host wake timed out";
    expect(useSandboxResumeStore.getState().bySession.s1).toMatchObject({
      phase: "failed",
      message,
    });
    expect(toast.error).toHaveBeenCalledExactlyOnceWith(
      message,
      expect.objectContaining({ action: expect.objectContaining({ label: "Retry" }) }),
    );
    expect(refreshRunnerHealth).not.toHaveBeenCalled();

    const retry = vi.mocked(toast.error).mock.calls[0]![1] as unknown as {
      action: { onClick: () => void };
    };
    retry.action.onClick();
    await vi.waitFor(() => expect(phase("s1")).toBe("awaiting_online"));
    expect(retrySession).toHaveBeenCalledTimes(2);
    expect(toast.dismiss).toHaveBeenCalledWith("sandbox-resume:s1");
  });

  it("fails on the open session's launch failure, ignoring the late answer", async () => {
    const pending = deferred<PostEventResponse>();
    vi.mocked(retrySession).mockReturnValue(pending.promise);
    useChatStore.setState({ conversationId: "s1", sandboxStatus: { stage: "provisioning" } });
    const resume = resumeSandbox("s1");
    observeSandboxLaunch("s1", { stage: "failed", error: "No capacity" });
    expect(useSandboxResumeStore.getState().bySession.s1).toMatchObject({
      phase: "failed",
      message: "The sandbox couldn't start: No capacity",
    });
    pending.resolve(recovered);
    await resume;
    expect(phase("s1")).toBe("failed");
    expect(refreshRunnerHealth).not.toHaveBeenCalled();
  });

  it("doesn't let a cleared attempt's late answer fail the next one", async () => {
    const first = deferred<PostEventResponse>();
    const second = deferred<PostEventResponse>();
    vi.mocked(retrySession).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    useChatStore.setState({ conversationId: "s1", sandboxStatus: null });
    const resumeA = resumeSandbox("s1");
    // The launch waits on the user, which clears A while its request is out.
    observeSandboxLaunch("s1", { stage: "action_required", details: "Approve it" });
    expect(phase("s1")).toBeUndefined();
    const resumeB = resumeSandbox("s1");
    expect(phase("s1")).toBe("requesting");

    first.reject(new Error("late failure"));
    await resumeA;
    expect(phase("s1")).toBe("requesting");
    expect(toast.error).not.toHaveBeenCalled();
    second.resolve(recovered);
    await resumeB;
    expect(phase("s1")).toBe("awaiting_online");
  });

  it.each(["the request failed", "it timed out"] as const)(
    "tells panes to redial when the host comes online after %s",
    async (failure) => {
      vi.useFakeTimers();
      if (failure === "the request failed") {
        vi.mocked(retrySession).mockRejectedValueOnce(new Error("gateway timeout"));
        await resumeSandbox("s1");
      } else {
        await resumeSandbox("s1");
        vi.advanceTimersByTime(SANDBOX_RESUME_QUIET_LIMIT_MS);
      }
      expect(phase("s1")).toBe("failed");
      expect(useSandboxResumeStore.getState().resumedAt.s1).toBeUndefined();

      observeSandboxHostHealth(new Map([["s1", true]]));
      expect(phase("s1")).toBeUndefined();
      expect(useSandboxResumeStore.getState().resumedAt.s1).toBeTypeOf("number");
      // Only the one resume request; coming online doesn't send another.
      expect(retrySession).toHaveBeenCalledOnce();
    },
  );

  it("publishes nothing for a host online with no resume", () => {
    observeSandboxHostHealth(new Map([["s1", true]]));
    expect(useSandboxResumeStore.getState().resumedAt).toEqual({});
  });

  it("ignores a launch stage for a session with no resume", () => {
    observeSandboxLaunch("s1", { stage: "failed", error: "No capacity" });
    expect(phase("s1")).toBeUndefined();
  });

  it("times out on its own clock, without relying on health polls", async () => {
    vi.useFakeTimers();
    await resumeSandbox("s1");
    vi.advanceTimersByTime(SANDBOX_RESUME_QUIET_LIMIT_MS - SANDBOX_RESUME_TICK_MS);
    expect(phase("s1")).toBe("awaiting_online");
    vi.advanceTimersByTime(SANDBOX_RESUME_TICK_MS);
    expect(useSandboxResumeStore.getState().bySession.s1).toMatchObject({
      phase: "failed",
      message: SANDBOX_RESUME_TIMED_OUT,
    });
    expect(toast.error).toHaveBeenCalledOnce();
    // The clock stops once nothing is running.
    expect(vi.getTimerCount()).toBe(0);
  });
});
