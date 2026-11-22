// A session's shell tabs' connections, from its liveness, sandbox resume and shell list. Each
// shell's wait is timed by the tab store, so every panel agrees, and Try Again starts it over.

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useState, useSyncExternalStore } from "react";
import { toast } from "sonner";

import type { SandboxResumeNoticeState } from "@/components/SandboxResumeNotice";
import { refreshRunnerHealth } from "@/hooks/useRunnerHealth";
import { useSandboxResume } from "@/hooks/useSandboxResume";
import { useSession } from "@/hooks/useSession";
import { abandonSandboxWake, wakeSandboxSession } from "@/hooks/useSessionReconnect";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import { getCliServerUrl } from "@/lib/host";
import { terminalsQueryKey } from "@/lib/terminals";
import { useCommandSession } from "@/shell/commandContext";
import { buildReconnectCommand } from "@/shell/ReconnectSessionDialog";
import { resumeSandboxAvailability } from "@/shell/resumeSandboxAvailability";

import type { ShellWaits } from "./shellWaits";
import {
  type TerminalTabConnection,
  type TerminalTabInputs,
  terminalTabConnection,
  terminalTabWait,
  terminalTabWaitLimit,
} from "./terminalTabConnection";

/** How long a session Resume may wait for the server before it can be tried again. */
export const SESSION_RESUME_REQUEST_LIMIT_MS = 30_000;
export const SESSION_RESUME_TIMED_OUT =
  "Couldn't reach the server to resume the session. Try again.";

/**
 * Each session's Resume on its way, by attempt, shared by every tab that offers it. Only the
 * current attempt's answer or deadline may end it, so a late answer can't touch a newer one.
 */
const resumeAttempts = new Map<string, number>();
let nextResumeAttempt = 1;
const resumeListeners = new Set<() => void>();
function subscribeResumes(listener: () => void): () => void {
  resumeListeners.add(listener);
  return () => void resumeListeners.delete(listener);
}
function notifyResumes(): void {
  for (const listener of resumeListeners) listener();
}
/** End ``attempt`` if it's still ``sessionId``'s current one; reports whether it was. */
function endResume(sessionId: string, attempt: number): boolean {
  if (resumeAttempts.get(sessionId) !== attempt) return false;
  resumeAttempts.delete(sessionId);
  notifyResumes();
  return true;
}

/** Forget every pending Resume; for tests. */
export function resetSessionResumes(): void {
  resumeAttempts.clear();
  notifyResumes();
}

/** A shell tab, by its shell's key and whether the session's shell list has it. */
export interface TerminalTabShell {
  key: string;
  inventory: TerminalTabInputs["inventory"];
}

export interface TerminalTabConnections {
  /** One shell tab's connection. */
  connectionOf: (shell: TerminalTabShell) => TerminalTabConnection;
  /** The Resume notice for a connection that shows the sandbox, with this session's action. */
  sandboxNotice: (connection: TerminalTabConnection) => SandboxResumeNoticeState | null;
  /** Ask for the shell list again, giving every shell a fresh wait. */
  tryAgain: () => void;
  /**
   * Start the session's runner and give it a fresh wait; a click while one is on its way is
   * ignored. A failure, or no answer within the request limit, is shown as a toast.
   */
  resumeSession: () => Promise<void>;
  /** A session Resume is on its way. */
  resumingSession: boolean;
  /** The command that relaunches the session when it isn't host-bound and its runner stopped. */
  localReconnectCommand: string;
}

/** ``sessionId``'s shell tab connections, timed by the waits its tab store keeps. */
export function useTerminalTabConnections(
  sessionId: string,
  shellWaits: ShellWaits,
  {
    liveness,
    listFailed,
    shells,
  }: {
    liveness: SessionLiveness["kind"];
    listFailed: boolean;
    shells: readonly TerminalTabShell[];
  },
): TerminalTabConnections {
  const { session } = useSession(sessionId);
  const commandSession = useCommandSession(sessionId);
  const sandbox = useSandboxResume(sessionId);
  const queryClient = useQueryClient();
  const facts = {
    listFailed,
    liveness,
    hostResumable: !!session?.hostResumable,
    sandbox: { status: sandbox.status, ownStatus: sandbox.resumeStatus, failure: sandbox.failure },
  };
  const waiting = shells.flatMap((shell) => {
    const wait = terminalTabWait({ ...facts, inventory: shell.inventory });
    return wait ? [[shell.key, wait] as const] : [];
  });
  const waitingSignature = JSON.stringify(waiting);
  const waitOf = new Map<string, string>(waiting);
  const [now, setNow] = useState(() => Date.now());
  // Bumped when the waits start over, which the clock alone may not show.
  const [, setRevision] = useState(0);
  const restart = () => {
    shellWaits.restart();
    setNow(Date.now());
    setRevision((revision) => revision + 1);
  };
  const resumingSession = useSyncExternalStore(
    subscribeResumes,
    useCallback(() => resumeAttempts.has(sessionId), [sessionId]),
  );
  const resumeSession = async () => {
    if (resumeAttempts.has(sessionId)) return;
    const attempt = nextResumeAttempt++;
    resumeAttempts.set(sessionId, attempt);
    notifyResumes();
    restart();
    // Joins a wake already on its way, such as Reconnect host's.
    const wake = wakeSandboxSession(sessionId);
    const answered = wake.then(
      () => ({ ok: true as const }),
      (error: unknown) => ({ ok: false as const, error }),
    );
    let deadline: number | undefined;
    const timedOut = new Promise<"timed_out">((resolve) => {
      deadline = window.setTimeout(() => resolve("timed_out"), SESSION_RESUME_REQUEST_LIMIT_MS);
    });
    const outcome = await Promise.race([answered, timedOut]);
    window.clearTimeout(deadline);
    if (outcome === "timed_out") {
      abandonSandboxWake(sessionId, wake);
      if (endResume(sessionId, attempt)) toast.error(SESSION_RESUME_TIMED_OUT);
      return;
    }
    if (!endResume(sessionId, attempt)) return;
    if (outcome.ok) {
      // The runner may take a while to start: its wait counts from when it was asked, and its
      // health is read again now rather than at the next poll.
      restart();
      refreshRunnerHealth();
    } else {
      const { error } = outcome;
      toast.error(error instanceof Error ? error.message : "Couldn't resume the session.");
    }
  };

  // A wait starts when it's first seen, in the store, so a panel that replaces this one keeps it.
  useLayoutEffect(() => {
    shellWaits.track(new Map(JSON.parse(waitingSignature) as [string, string][]));
    setNow(Date.now());
  }, [shellWaits, waitingSignature]);

  // A tab changes only when a wait reaches its limit, so the clock wakes then and not before.
  let nextDeadline: number | null = null;
  for (const [key, wait] of waiting) {
    // A wait the store hasn't taken in yet, or that waits for something new, starts now.
    const deadline = (shellWaits.since(key, wait) ?? now) + terminalTabWaitLimit(wait);
    if (deadline > now && (nextDeadline === null || deadline < nextDeadline))
      nextDeadline = deadline;
  }
  useEffect(() => {
    if (nextDeadline === null) return;
    const deadline = nextDeadline;
    let timer = 0;
    const wake = () => {
      // A timer that fires early, or a system clock set back during the wait, leaves time to go.
      const remaining = deadline - Date.now();
      if (remaining > 0) {
        timer = window.setTimeout(wake, remaining);
        return;
      }
      setNow(Date.now());
    };
    timer = window.setTimeout(wake, deadline - Date.now());
    return () => window.clearTimeout(timer);
  }, [nextDeadline]);

  const { resume } = sandbox;
  return {
    connectionOf: ({ key, inventory }) => {
      const wait = waitOf.get(key);
      const since = wait === undefined ? undefined : shellWaits.since(key, wait);
      const waited = since === undefined ? 0 : now - since;
      return terminalTabConnection({ ...facts, inventory, waited });
    },
    sandboxNotice: (connection) =>
      connection.kind === "sandbox"
        ? {
            status: connection.status,
            failure: connection.failure,
            // The status may be this module's own (a timed-out wait), so who may resume is
            // read against it rather than copied from the sandbox's.
            availability: resumeSandboxAvailability(commandSession, connection.status),
            resume: () => {
              restart();
              resume();
            },
          }
        : null,
    tryAgain: () => {
      restart();
      // Joins a request already on its way: cancelling it could let its stale answer land later.
      void queryClient.refetchQueries(
        { queryKey: terminalsQueryKey(sessionId) },
        { cancelRefetch: false },
      );
    },
    resumeSession,
    resumingSession,
    localReconnectCommand: buildReconnectCommand({
      conversationId: sessionId,
      serverUrl: getCliServerUrl(),
      wrapper: session?.labels?.["omnigent.wrapper"],
      harness: session?.harness,
      state: "local_stranded",
    }),
  };
}
