// Where the app shell sends links, pages and shells while the workspace sidebar shows as
// tabs (`useTabsSidebar`). The tabs code loads on first use, so a deployment with the
// flag off never downloads it.

import { type ComponentType, createElement, lazy, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import type { AgentBrowserTabRequest } from "@/components/BrowserPane/agentTools/useRevealBrowserOnAgentTabs";
import { readBrowserTabsState } from "@/hooks/useBrowserTabs";
import { onBrowserActionSettled } from "@/lib/browserActionBus";

import {
  claimFileAction,
  nextFileActionRequest,
  useFileActionStore,
} from "./fileCommands/fileActionStore";
import type { ShowAllPullRequestsCommandProps } from "./workspaceCommands/useShowAllPullRequestsCommand";
import type * as WorkspaceTabsSessions from "./workspaceTabs/workspaceTabsSessions";

type WorkspaceTabsModule = typeof WorkspaceTabsSessions;
/** What an open reports; a refusal carries the reason to show. */
type WorkspaceTabsResult = { ok: true } | { ok: false; reason?: string };

/** The session tabs' module, loaded once, on the first link, page or shell routed to tabs. */
let workspaceTabsModule: Promise<WorkspaceTabsModule> | null = null;

/** The tabs sidebar's host, loaded when it first shows. */
export const LazyWorkspaceTabsHost = lazy(() => import("./workspaceTabs/WorkspaceTabsHost"));

interface FileActionHostModule {
  FileActionHost: ComponentType;
}

/** Said when a file action can't run because its code still won't load. */
export const FILE_ACTION_LOAD_FAILED =
  "Couldn't run this file action. Reload the window and try again.";

/**
 * The file action host's module, or, when it can't load, such as after an update replaced it, a
 * stand-in that loads it again on the next request: only that action fails, not the shell around it.
 */
export function loadFileActionHost(
  load: () => Promise<FileActionHostModule> = () => import("./fileCommands/FileActionHost"),
): Promise<{ default: ComponentType }> {
  return load().then(
    (module) => ({ default: module.FileActionHost }),
    (error: unknown) => {
      console.error("[file-actions] couldn't load the file action host:", error);
      return { default: () => createElement(FileActionHostRetry, { load }) };
    },
  );
}

/**
 * Loads the host again for the next request, which waits in the queue with any made meanwhile,
 * so the host runs them all once loaded; a request the load fails for says so and is dropped.
 */
function FileActionHostRetry({ load }: { load: () => Promise<FileActionHostModule> }) {
  const request = useFileActionStore(nextFileActionRequest);
  const [host, setHost] = useState<ComponentType | null>(null);
  const retriedFor = useRef<number | null>(null);
  useEffect(() => {
    if (host !== null || request === null || retriedFor.current === request.key) return;
    retriedFor.current = request.key;
    load().then(
      (module) => setHost(() => module.FileActionHost),
      (error: unknown) => {
        console.error("[file-actions] couldn't load the file action host:", error);
        if (claimFileAction(request)) toast.error(FILE_ACTION_LOAD_FAILED);
      },
    );
  }, [host, load, request]);
  return host === null ? null : createElement(host);
}

/** Carries out the file actions the tabs sidebar's menus and ⌘K ask for; loaded with the sidebar. */
export const LazyFileActionHost = lazy(() => loadFileActionHost());

interface ShowAllPullRequestsCommandModule {
  ShowAllPullRequestsCommand: ComponentType<ShowAllPullRequestsCommandProps>;
}

/** ⌘K's Show all pull requests, or nothing when its code can't load: only that row is missing. */
export function loadShowAllPullRequestsCommand(
  load: () => Promise<ShowAllPullRequestsCommandModule> = () =>
    import("./workspaceCommands/useShowAllPullRequestsCommand"),
): Promise<{ default: ComponentType<ShowAllPullRequestsCommandProps> }> {
  return load().then(
    (module) => ({ default: module.ShowAllPullRequestsCommand }),
    (error: unknown) => {
      console.error("[pull-requests] couldn't load Show all pull requests:", error);
      return { default: () => null };
    },
  );
}

/** Registers ⌘K's Show all pull requests for a tabs sidebar session; loaded with the sidebar. */
export const LazyShowAllPullRequestsCommand = lazy(() => loadShowAllPullRequestsCommand());

export { useTabsSidebar } from "./useTabsSidebar";

/** Said when the tabs code can't load, such as after an update replaced it, or an open fails. */
export const WORKSPACE_TABS_OPEN_FAILED = "Couldn't open this in the workspace sidebar. Try again.";

/** The session tabs' module; a failed load is forgotten, so the next request tries again. */
function loadWorkspaceTabs(): Promise<WorkspaceTabsModule> {
  workspaceTabsModule ??= import("./workspaceTabs/workspaceTabsSessions").catch(
    (error: unknown) => {
      workspaceTabsModule = null;
      throw error;
    },
  );
  return workspaceTabsModule;
}

/**
 * Run `open` against the session's tabs, saying why when it can't. A request a newer one
 * replaced, or one the person cancelled at an unsaved-changes question, says nothing.
 */
export function routeToWorkspaceTabs(
  open: (tabs: WorkspaceTabsModule) => Promise<WorkspaceTabsResult>,
): Promise<void> {
  return loadWorkspaceTabs()
    .then(open)
    .then((result) => {
      if (!result.ok && result.reason) toast.error(result.reason);
    })
    .catch((error: unknown) => {
      console.error("[workspace-tabs] open failed:", error);
      toast.error(WORKSPACE_TABS_OPEN_FAILED);
    });
}

/**
 * Show the session's shell or side chat tab, the selected one first, as the session menu's
 * Shells and Side chats do; with none open, a New tab to start or choose one in.
 */
export function showWorkspaceTabOrChooser(
  sessionId: string,
  kind: "terminal" | "chat",
): Promise<void> {
  return routeToWorkspaceTabs(async (tabs) => {
    const { store } = tabs.workspaceTabsSession(sessionId);
    const isKind = (tab: { binding?: { kind: string } } | object) =>
      "binding" in tab && tab.binding?.kind === kind;
    const selected = store.selectedTab();
    const tab = selected && isKind(selected) ? selected : store.tabs().findLast(isKind);
    if (tab && store.select(tab.id)) return { ok: true };
    return tabs.openWorkspaceNewTab(sessionId);
  });
}

/** Per session, the agent's latest browser_tabs request this window is showing. */
const latestAgentTabRequests = new Map<string, string>();
let listeningForAgentTabs = false;

/** Show a session's browser view in its tab, or say the strip is full, as other opens do. */
export function showWorkspaceBrowserView(
  sessionId: string,
  viewKey: string,
  { background }: { background: boolean },
): Promise<void> {
  return routeToWorkspaceTabs(async (tabs) => {
    if (tabs.openWorkspaceBrowserView(sessionId, viewKey, { background }) !== null) {
      return { ok: true };
    }
    // Loaded with the tabs code, not eagerly.
    const { TAB_CAP_REASON } = await import("./workspaceTabs/planOpen");
    return { ok: false, reason: TAB_CAP_REASON };
  });
}

function showBrowserView(sessionId: string, viewKey: string): void {
  void showWorkspaceBrowserView(sessionId, viewKey, { background: false });
}

/** A new tab request's own outcome names its tab; it shows unless a later request replaced it. */
function listenForAgentTabOutcomes(): void {
  if (listeningForAgentTabs) return;
  listeningForAgentTabs = true;
  onBrowserActionSettled((event, sessionId, outcome) => {
    if (latestAgentTabRequests.get(sessionId) !== event.actionId) return;
    latestAgentTabRequests.delete(sessionId);
    const tabId = outcome.data?.tab_id;
    if (outcome.ok && typeof tabId === "string") showBrowserView(sessionId, tabId);
  });
}

/**
 * Show the browser tab an agent opened or selected in the session's tabs, as the classic
 * sidebar shows its Browser pane. A new tab has no id until the relay opens it, so it
 * shows once this window's relay reports that request succeeded; a window that didn't
 * run the request doesn't move to it. Each request replaces the session's earlier one.
 */
export function showAgentBrowserTab(sessionId: string, request: AgentBrowserTabRequest): void {
  listenForAgentTabOutcomes();
  if (request.action === "new") {
    latestAgentTabRequests.set(sessionId, request.actionId);
    return;
  }
  latestAgentTabRequests.delete(sessionId);
  if (request.tabId !== null && readBrowserTabsState(sessionId).tabs.includes(request.tabId)) {
    showBrowserView(sessionId, request.tabId);
  }
}
