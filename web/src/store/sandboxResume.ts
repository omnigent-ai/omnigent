// One resume per session, shared by every surface that offers it: the composer's
// host badge, the terminal, Files, Changes, Pull Requests, and ⌘K. It outlives the
// view that started it, so switching sessions or panels doesn't drop the attempt.

import { useEffect } from "react";
import { toast } from "sonner";
import { create } from "zustand";

import { useSessionHostOnline } from "@/hooks/RunnerHealthProvider";
import { refreshRunnerHealth } from "@/hooks/useRunnerHealth";
import {
  isSandboxResumeActive,
  reduceSandboxResume,
  type SandboxResumeEvent,
  type SandboxResumeState,
} from "@/lib/sandboxResume";
import { retrySession } from "@/lib/sessionsApi";
import type { SandboxStatus } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";

/** How often a running resume checks its time limits. */
export const SANDBOX_RESUME_TICK_MS = 5_000;

interface SandboxResumeStore {
  bySession: Record<string, SandboxResumeState>;
  /** When each session's last resume saw its host come online, for views that reconnect after it. */
  resumedAt: Record<string, number>;
}

export const useSandboxResumeStore = create<SandboxResumeStore>(() => ({
  bySession: {},
  resumedAt: {},
}));

let ticker: ReturnType<typeof setInterval> | null = null;
// Outlives each session's state, so a request whose state was cleared can't match a later attempt.
let lastAttempt = 0;

function syncTicker(): void {
  const running = Object.values(useSandboxResumeStore.getState().bySession).some(
    isSandboxResumeActive,
  );
  if (running && ticker === null) {
    ticker = setInterval(() => {
      for (const sessionId of Object.keys(useSandboxResumeStore.getState().bySession)) {
        dispatch(sessionId, { type: "tick", now: Date.now() });
      }
    }, SANDBOX_RESUME_TICK_MS);
  } else if (!running && ticker !== null) {
    clearInterval(ticker);
    ticker = null;
  }
}

function dispatch(sessionId: string, event: SandboxResumeEvent): void {
  const { bySession, resumedAt } = useSandboxResumeStore.getState();
  const previous = bySession[sessionId];
  const next = reduceSandboxResume(previous, event);
  if (next === previous) return;
  const { [sessionId]: _previous, ...rest } = bySession;
  useSandboxResumeStore.setState({
    bySession: next ? { ...rest, [sessionId]: next } : rest,
    // A host back after a running or failed resume (a timed-out request can
    // still bring it up) tells every pane to redial; nothing restarts the agent.
    resumedAt:
      event.type === "host_online" && previous !== undefined && next === undefined
        ? { ...resumedAt, [sessionId]: Date.now() }
        : resumedAt,
  });
  syncTicker();
  if (next?.phase === "failed" && previous?.phase !== "failed") {
    // The failure also shows on the surface the user resumed from; the toast
    // covers a resume started from ⌘K or a panel that has since closed.
    toast.error(next.message, {
      id: `sandbox-resume:${sessionId}`,
      action: { label: "Retry", onClick: () => void resumeSandbox(sessionId) },
    });
  }
  if (isSandboxResumeActive(next) || next === undefined) {
    toast.dismiss(`sandbox-resume:${sessionId}`);
  }
}

function launchFor(sessionId: string): SandboxStatus | null {
  const chat = useChatStore.getState();
  return chat.conversationId === sessionId ? chat.sandboxStatus : null;
}

// Each session's resume request while it is out, so a joining caller waits for its answer.
const pendingRequests = new Map<string, Promise<void>>();

/**
 * Resume `sessionId`'s sandbox. The server starts it and the agent, as it does
 * for a message. A second call while one is running joins it, and settles when
 * that request answers.
 */
export function resumeSandbox(sessionId: string): Promise<void> {
  if (isSandboxResumeActive(useSandboxResumeStore.getState().bySession[sessionId])) {
    return pendingRequests.get(sessionId) ?? Promise.resolve();
  }
  const request = requestResume(sessionId).finally(() => {
    if (pendingRequests.get(sessionId) === request) pendingRequests.delete(sessionId);
  });
  pendingRequests.set(sessionId, request);
  return request;
}

async function requestResume(sessionId: string): Promise<void> {
  const attempt = ++lastAttempt;
  dispatch(sessionId, { type: "start", attempt, now: Date.now(), launch: launchFor(sessionId) });
  try {
    await retrySession(sessionId);
    const current = useSandboxResumeStore.getState().bySession[sessionId];
    dispatch(sessionId, { type: "request_succeeded", attempt, now: Date.now() });
    if (current?.phase === "requesting" && current.attempt === attempt) refreshRunnerHealth();
  } catch (error) {
    dispatch(sessionId, {
      type: "request_failed",
      attempt,
      message:
        error instanceof Error && error.message
          ? `Couldn't resume the sandbox: ${error.message}`
          : "Couldn't resume the sandbox. Try again.",
    });
  }
}

/** Report each session's host liveness; a host online ends its resume. */
export function observeSandboxHostHealth(hostOnline: ReadonlyMap<string, boolean | null>): void {
  for (const sessionId of Object.keys(useSandboxResumeStore.getState().bySession)) {
    if (hostOnline.get(sessionId) === true) dispatch(sessionId, { type: "host_online" });
  }
}

/** Report the open session's launch stage. */
export function observeSandboxLaunch(sessionId: string | null, launch: SandboxStatus | null): void {
  if (sessionId === null || !(sessionId in useSandboxResumeStore.getState().bySession)) return;
  dispatch(sessionId, { type: "launch", launch, now: Date.now() });
}

/** Test seam: forget every resume and stop the clock. */
export function resetSandboxResumes(): void {
  pendingRequests.clear();
  useSandboxResumeStore.setState({ bySession: {}, resumedAt: {} });
  syncTicker();
}

/** The session's resume, or `undefined` while none is running or failed. */
export function useSandboxResumeState(
  sessionId: string | null | undefined,
): SandboxResumeState | undefined {
  return useSandboxResumeStore((state) => (sessionId ? state.bySession[sessionId] : undefined));
}

/** When the session's last resume brought its host online, or `undefined`. */
export function useSandboxResumedAt(sessionId: string | null | undefined): number | undefined {
  return useSandboxResumeStore((state) => (sessionId ? state.resumedAt[sessionId] : undefined));
}

/**
 * Feed a session's resume its host liveness, and its launch stage while it's the open
 * session. Every surface that offers Resume mounts it, through `useSandboxResume`.
 */
export function useSandboxResumeObserver(sessionId: string | null | undefined): void {
  const hostOnline = useSessionHostOnline(sessionId ?? undefined);
  // Re-check when the resume moves on, since the host may already read online.
  const phase = useSandboxResumeStore((state) =>
    sessionId ? state.bySession[sessionId]?.phase : undefined,
  );
  useEffect(() => {
    if (sessionId && phase && hostOnline === true) {
      observeSandboxHostHealth(new Map([[sessionId, true]]));
    }
  }, [sessionId, phase, hostOnline]);
  const open = useChatStore((state) => !!sessionId && state.conversationId === sessionId);
  const launch = useChatStore((state) => (open ? state.sandboxStatus : null));
  useEffect(() => {
    if (open && sessionId) observeSandboxLaunch(sessionId, launch);
  }, [open, sessionId, launch]);
}
