import type { ScheduledTaskRun } from "@/lib/scheduledTasksApi";

/** One entry in an automation's run list: a run on its own, or the runs sharing a session. */
export type RunListEntry =
  | { kind: "run"; run: ScheduledTaskRun }
  | { kind: "session"; sessionId: string; runs: ScheduledTaskRun[] };

/**
 * Group the runs that share a session, so the list shows its running total once.
 *
 * A run joins its session's group when another listed run used the same
 * session, or when the session is the one the automation continues, which can
 * hold spend from before the automation. A group takes the place of its newest
 * run and keeps its runs in list order.
 *
 * @param runs - The automation's runs, newest first.
 * @param continuedSessionId - The session the automation continues, or `null`.
 */
export function groupRunsBySession(
  runs: readonly ScheduledTaskRun[],
  continuedSessionId: string | null,
): RunListEntry[] {
  const runsPerSession = new Map<string, number>();
  for (const run of runs) {
    if (run.conversationId !== null) {
      runsPerSession.set(run.conversationId, (runsPerSession.get(run.conversationId) ?? 0) + 1);
    }
  }
  const groups = new Map<string, ScheduledTaskRun[]>();
  const entries: RunListEntry[] = [];
  for (const run of runs) {
    const sessionId = run.conversationId;
    const shared =
      sessionId !== null &&
      (sessionId === continuedSessionId || (runsPerSession.get(sessionId) ?? 0) > 1);
    if (!shared) {
      entries.push({ kind: "run", run });
      continue;
    }
    const groupRuns = groups.get(sessionId);
    if (groupRuns) {
      groupRuns.push(run);
      continue;
    }
    const newGroup = [run];
    groups.set(sessionId, newGroup);
    entries.push({ kind: "session", sessionId, runs: newGroup });
  }
  return entries;
}
