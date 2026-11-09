// A session's tasks for the Tasks tab and the chat's task preview: its plan's
// steps when it has a plan, otherwise the agent's own task list.

import { useChildSessions } from "@/hooks/useChildSessions";
import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import { useSessionLedger, type SessionLedgerView } from "@/hooks/useSessionLedger";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { parentIdFromLabels } from "@/lib/sessionHost";
import {
  pausedLabel,
  sessionTaskList,
  type LedgerEntry,
  type PausedLabel,
  type SessionTaskList,
} from "@/lib/sessionLedger";

const NO_ENTRIES: LedgerEntry[] = [];

export interface SessionTasksView extends SessionTaskList {
  /** The session's ledger; empty on servers that keep none. */
  ledger: SessionLedgerView;
  /** Whether the server keeps session ledgers. */
  ledgerSupported: boolean;
  /** True until it's known whether the session has a plan. */
  loading: boolean;
  /**
   * Whether the session's work is in flight, so a started task is running: its
   * turn, a background task (such as a shell the agent left running between
   * turns) or a sub-agent it started.
   */
  sessionWorking: boolean;
  /**
   * What the list says while the session is idle with tasks the agent could
   * run and none waiting for the user: "Stopped", or "Run ended" after an
   * automation run. Null otherwise.
   */
  paused: PausedLabel | null;
}

/**
 * @param conversationId - The session; undefined while it has none.
 * @param enabled - Whether to load the ledger now, e.g. while the tab shows.
 */
export function useSessionTasks(
  conversationId: string | undefined,
  enabled: boolean,
): SessionTasksView {
  const serverInfo = useServerInfo();
  const ledgerSupported = serverInfo !== "loading" && serverInfo.session_ledger === true;
  const ledger = useSessionLedger(conversationId, enabled && ledgerSupported);
  const nativeTasks = useConversationEntryValue(conversationId ?? null, (state) => state.todos);
  const sessionStatus = useConversationEntryValue(
    conversationId ?? null,
    (state) => state.sessionStatus,
  );
  const backgroundTasks = useConversationEntryValue(
    conversationId ?? null,
    (state) => state.backgroundTaskCount,
  );
  // The same list the Agents rail reads. A side chat isn't delegated work.
  const { children } = useChildSessions(conversationId ?? null);
  const workersRunning = children.some(
    (child) => child.busy && parentIdFromLabels(child.labels) === null,
  );
  const sessionWorking =
    sessionStatus === "running" ||
    sessionStatus === "waiting" ||
    (backgroundTasks ?? 0) > 0 ||
    workersRunning;
  // A question or permission prompt in the chat waiting for the user's answer.
  const awaitingUser = useConversationEntryValue(conversationId ?? null, (state) =>
    state.blocks.some((block) => block.type === "elicitation" && block.status === "pending"),
  );
  const entries = ledgerSupported ? ledger.entries : NO_ENTRIES;
  const tasks = sessionTaskList(entries, nativeTasks, sessionWorking, awaitingUser);
  return {
    ...tasks,
    ledger,
    ledgerSupported,
    loading: serverInfo === "loading" || ledger.isLoading,
    sessionWorking,
    paused: pausedLabel(tasks.progress, entries, sessionWorking),
  };
}
