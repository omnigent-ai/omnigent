// Back and Forward through one session's workspace tabs, wherever the app shell binds
// them: where each step lands, and the step itself.

import { useCallback, useMemo, useSyncExternalStore } from "react";

import {
  type WorkspaceHistoryDirection,
  type WorkspaceHistoryFacts,
  workspaceHistoryFacts,
} from "../workspaceCommands/model";
import { placeTitle } from "./placeTitle";
import { workspaceTabsSession } from "./workspaceTabsSessions";

export interface WorkspaceHistoryBinding {
  /** Where each step lands; null without a bound session. */
  facts: WorkspaceHistoryFacts | null;
  go: (direction: WorkspaceHistoryDirection) => void;
}

const noSubscription = () => () => {};

/** The history of `conversationId`'s workspace tabs; null binds none. */
export function useWorkspaceHistory(conversationId: string | null): WorkspaceHistoryBinding {
  const store = useMemo(
    () => (conversationId === null ? null : workspaceTabsSession(conversationId).store),
    [conversationId],
  );
  const snapshot = useSyncExternalStore(
    store?.subscribe ?? noSubscription,
    () => store?.snapshot() ?? null,
  );
  const facts = useMemo(
    () =>
      store && snapshot
        ? workspaceHistoryFacts(store.history.state, (visit) => placeTitle(visit.place).label)
        : null,
    [store, snapshot],
  );
  const go = useCallback(
    (direction: WorkspaceHistoryDirection) => {
      void store?.go(direction);
    },
    [store],
  );
  return { facts, go };
}
