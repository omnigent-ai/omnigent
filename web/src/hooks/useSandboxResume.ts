// What a surface shows for a session's stopped sandbox: asleep with a Resume
// action, resuming, or failed with Retry. Every surface reads the same state
// from the session's shared resume, so they move together.

import { useCallback } from "react";

import { useSession } from "@/hooks/useSession";
import { livenessRowFromSession, useSessionLiveness } from "@/hooks/useSessionLiveness";
import { useSessionActive } from "@/hooks/useWorkspaceChangedFiles";
import { sandboxResumeDisplay, type SandboxResumeStatus } from "@/lib/sandboxResume";
import { isSandboxLaunchStopped } from "@/lib/sandboxStatus";
import { useChatStore } from "@/store/chatStore";
import { type CommandAvailability, HIDDEN } from "@/store/paletteCommands";
import {
  resumeSandbox,
  useSandboxResumeObserver,
  useSandboxResumeState,
} from "@/store/sandboxResume";
import { useCommandSession } from "@/shell/commandContext";
import { resumeSandboxAvailability } from "@/shell/resumeSandboxAvailability";

export type { SandboxResumeStatus };

export interface SandboxResumeView {
  status: SandboxResumeStatus;
  /** Why the last resume failed, while `status` is `failed`. */
  failure: string | null;
  /**
   * The session's own resume, ignoring a launch the server reports: a stage can
   * go stale, so menus keep offering Resume while panels show the progress.
   */
  resumeStatus: SandboxResumeStatus;
  /** Whether this viewer may resume it; a reason greys the action out. */
  availability: CommandAvailability;
  resume: () => void;
}

/** The session's sandbox as every Resume surface presents it. */
export function useSandboxResume(sessionId: string | null | undefined): SandboxResumeView {
  const id = sessionId ?? undefined;
  const { session } = useSession(id);
  const commandSession = useCommandSession(sessionId);
  const turnActive = useSessionActive(id);
  const liveness = useSessionLiveness(id, livenessRowFromSession(session), { turnActive });
  // The resume ends, or fails on a launch error, from what this session's host reports.
  useSandboxResumeObserver(sessionId);
  const resume = useSandboxResumeState(sessionId);
  const launch = useChatStore((state) =>
    state.conversationId === sessionId ? state.sandboxStatus : null,
  );
  const reportedLaunch = launch ?? session?.sandboxStatus ?? null;
  const launchInProgress =
    !!session?.hostResumable && reportedLaunch !== null && !isSandboxLaunchStopped(reportedLaunch);
  const { status, resumeStatus, failure } = sandboxResumeDisplay(
    resume,
    liveness.kind,
    launchInProgress,
  );
  const availability = commandSession
    ? resumeSandboxAvailability(commandSession, resumeStatus)
    : HIDDEN;
  const run = useCallback(() => {
    if (sessionId) void resumeSandbox(sessionId);
  }, [sessionId]);
  return {
    status,
    resumeStatus,
    failure,
    availability,
    resume: run,
  };
}

/**
 * Whether the session's workspace is blocked on its sandbox: asleep, or resuming
 * and not yet online. Panels show the shared Resume notice instead of loading.
 */
export function useSandboxBlocksWorkspace(sessionId: string | null | undefined): boolean {
  return useSandboxResume(sessionId).status !== "none";
}
