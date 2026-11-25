// Back and Forward through one session's workspace tabs, wherever the app shell binds
// them: where each step lands, the step itself, and the browser tabs' navigation owners,
// which load the page a step lands on.

import { useCallback, useEffect, useMemo, useSyncExternalStore } from "react";

import {
  type WorkspaceHistoryDirection,
  type WorkspaceHistoryFacts,
  workspaceHistoryFacts,
} from "../workspaceCommands/model";
import {
  attachBrowserTabNavigation,
  type BrowserNavigationShell,
} from "./browserTabNavigationRegistry";
import { desktopBrowserNavigationShell } from "./desktopBrowserNavigation";
import { placeTitle } from "./placeTitle";
import { workspaceTabsSession } from "./workspaceTabsSessions";

export interface WorkspaceHistoryBinding {
  /** Where each step lands; null without a bound session. */
  facts: WorkspaceHistoryFacts | null;
  go: (direction: WorkspaceHistoryDirection) => void;
}

const noSubscription = () => () => {};

/**
 * The history of `conversationId`'s workspace tabs; null binds none. While bound, its
 * browser tabs' pages follow each step through `browserShell`, where the app has one.
 */
export function useWorkspaceHistory(
  conversationId: string | null,
  browserShell: () => BrowserNavigationShell | null = desktopBrowserNavigationShell,
): WorkspaceHistoryBinding {
  const store = useMemo(
    () => (conversationId === null ? null : workspaceTabsSession(conversationId).store),
    [conversationId],
  );
  const snapshot = useSyncExternalStore(
    store?.subscribe ?? noSubscription,
    () => store?.snapshot() ?? null,
  );
  const shell = useMemo(() => (store ? browserShell() : null), [store, browserShell]);

  useEffect(() => {
    if (!store || conversationId === null || !shell) return;
    return attachBrowserTabNavigation(store, conversationId, shell);
  }, [store, conversationId, shell]);

  const facts = useMemo(
    () =>
      store && snapshot
        ? workspaceHistoryFacts(
            store.history.state,
            (visit) => placeTitle(visit.place).label,
            store.isVisitBlocked,
          )
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
