import type * as SessionsApiModule from "@/lib/sessionsApi";
import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { retrySession } from "@/lib/sessionsApi";
import type { SandboxStatus } from "@/lib/types";
import { SandboxResumeNotice } from "@/components/SandboxResumeNotice";
import { SANDBOX_RESUME_TIMED_OUT, SANDBOX_RESUME_TOTAL_LIMIT_MS } from "@/lib/sandboxResume";
import { useChatStore } from "@/store/chatStore";
import {
  resetSandboxResumes,
  resumeSandbox,
  SANDBOX_RESUME_TICK_MS,
  useSandboxResumeStore,
} from "@/store/sandboxResume";

import { useSandboxResume } from "./useSandboxResume";

const fixture = vi.hoisted(() => ({
  liveness: "host_asleep" as string,
  hostOnline: undefined as boolean | null | undefined,
  turnActive: false,
  livenessTurnActive: undefined as boolean | undefined,
  session: { hostResumable: true, sandboxStatus: null as SandboxStatus | null },
}));

vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { id: "session", ...fixture.session } }),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useSessionActive: () => fixture.turnActive,
}));
vi.mock("@/hooks/useSessionLiveness", () => ({
  livenessRowFromSession: () => null,
  useSessionLiveness: (_id: string, _row: unknown, options: { turnActive: boolean }) => {
    fixture.livenessTurnActive = options.turnActive;
    return { kind: fixture.liveness };
  },
}));
vi.mock("@/hooks/useRunnerHealth", () => ({ refreshRunnerHealth: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionHostOnline: () => fixture.hostOnline,
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  retrySession: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), dismiss: vi.fn() } }));

const provisioning: SandboxStatus = { stage: "provisioning" };

beforeEach(() => {
  fixture.liveness = "host_asleep";
  fixture.hostOnline = undefined;
  fixture.turnActive = false;
  fixture.session = { hostResumable: true, sandboxStatus: null };
  useChatStore.setState({ conversationId: null, sandboxStatus: null });
});
afterEach(() => {
  resetSandboxResumes();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("useSandboxResume", () => {
  it("reads an asleep sandbox with no launch as asleep, with Resume", () => {
    const { result } = renderHook(() => useSandboxResume("session"));
    expect(result.current.status).toBe("asleep");
    expect(result.current.resumeStatus).toBe("asleep");
  });

  it.each([
    ["the open session's chat store", "store"],
    ["the session snapshot", "snapshot"],
  ] as const)(
    "shows a launch the server reports in %s as resuming, without sending a resume",
    (_label, source) => {
      // A wake from another tab or before a reload: no local resume, only the stage.
      if (source === "store") {
        useChatStore.setState({ conversationId: "session" });
        useChatStore.setState({ sandboxStatus: provisioning });
      } else {
        fixture.session = { hostResumable: true, sandboxStatus: provisioning };
      }
      const { result } = renderHook(() => useSandboxResume("session"));
      expect(result.current.status).toBe("resuming");
      // The stage can go stale, so menus still offer Resume from the shared resume's status.
      expect(result.current.resumeStatus).toBe("asleep");
      expect(retrySession).not.toHaveBeenCalled();
    },
  );

  it.each(["the request fails", "the stage stalls past the time limit"] as const)(
    "shows the session's own failure with Retry when %s under a reported stage",
    async (failure) => {
      fixture.session = { hostResumable: true, sandboxStatus: provisioning };
      if (failure === "the request fails") {
        vi.mocked(retrySession).mockRejectedValue(new Error("managed host wake timed out"));
      } else {
        vi.useFakeTimers();
        vi.mocked(retrySession).mockReturnValue(new Promise(() => {}));
      }
      render(<SandboxResumeNotice sessionId="session" purpose="browse files" />);
      await act(async () => {
        await Promise.race([resumeSandbox("session"), Promise.resolve()]);
      });
      if (failure !== "the request fails") {
        await act(async () => {
          vi.advanceTimersByTime(SANDBOX_RESUME_TOTAL_LIMIT_MS + SANDBOX_RESUME_TICK_MS);
        });
      }
      const notice = screen.getByTestId("sandbox-resume-notice");
      expect(notice).toHaveAttribute("data-status", "failed");
      expect(notice).toHaveTextContent(
        failure === "the request fails"
          ? "Couldn't resume the sandbox: managed host wake timed out"
          : SANDBOX_RESUME_TIMED_OUT,
      );
      expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    },
  );

  it("shows the launch while a sent message's turn starts the sandbox", () => {
    fixture.liveness = "starting";
    fixture.session = { hostResumable: true, sandboxStatus: provisioning };
    const { result } = renderHook(() => useSandboxResume("session"));
    expect(result.current.status).toBe("resuming");
  });

  it("stops counting a launch once it stops, failed or waiting on the user", () => {
    fixture.session = { hostResumable: true, sandboxStatus: provisioning };
    const { result, rerender } = renderHook(() => useSandboxResume("session"));
    expect(result.current.status).toBe("resuming");
    fixture.session = { hostResumable: true, sandboxStatus: { stage: "failed", error: "x" } };
    rerender();
    expect(result.current.status).toBe("asleep");
  });

  it("ignores a launch stage for a session without a resumable host", () => {
    fixture.liveness = "starting";
    fixture.session = { hostResumable: false, sandboxStatus: provisioning };
    const { result } = renderHook(() => useSandboxResume("session"));
    expect(result.current.status).toBe("none");
  });

  it("follows the session's own resume while it runs", async () => {
    vi.mocked(retrySession).mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useSandboxResume("session"));
    await act(async () => {
      void resumeSandbox("session");
    });
    expect(result.current.status).toBe("resuming");
    expect(result.current.resumeStatus).toBe("resuming");
    expect(retrySession).toHaveBeenCalledOnce();
  });

  it("ends the resume once the session's host reads online, and tells panes to redial", async () => {
    vi.mocked(retrySession).mockResolvedValue(undefined as never);
    const { result, rerender } = renderHook(() => useSandboxResume("session"));
    await act(async () => {
      await resumeSandbox("session");
    });
    expect(result.current.status).toBe("resuming");
    fixture.hostOnline = true;
    fixture.liveness = "online";
    rerender();
    expect(useSandboxResumeStore.getState().bySession.session).toBeUndefined();
    expect(useSandboxResumeStore.getState().resumedAt.session).toBeTypeOf("number");
    expect(result.current.status).toBe("none");
  });

  it("sees the open session's launch fail while its resume runs", async () => {
    vi.mocked(retrySession).mockReturnValue(new Promise(() => {}));
    useChatStore.setState({ conversationId: "session" });
    renderHook(() => useSandboxResume("session"));
    await act(async () => {
      void resumeSandbox("session");
    });
    act(() => useChatStore.setState({ sandboxStatus: { stage: "failed", error: "No capacity" } }));
    expect(useSandboxResumeStore.getState().bySession.session?.phase).toBe("failed");
  });

  it("reads a running turn from the app's one useSessionActive", () => {
    fixture.turnActive = true;
    renderHook(() => useSandboxResume("session"));
    expect(fixture.livenessTurnActive).toBe(true);
  });
});
