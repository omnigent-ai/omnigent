// Mirrors a session's open browser tabs to the server, so the same tabs reopen
// on the user's other devices and logins. The local store stays the working
// copy; the server copy wins when it is newer. Each save names the revision it
// was based on, and a save the server rejects as stale adopts the server's tabs.

import { resolveServerInfo } from "./capabilities";
import { authenticatedFetch } from "./identity";
import { supportsBrowser } from "./nativeBridge";
import { apiErrorFromResponse } from "./sessionsApi";
import {
  type BrowserTabPage,
  readSessionWorkspaceState,
  writeSessionWorkspaceState,
} from "./sessionWorkspaceState";
import { isTempConvId } from "./tempConversationId";

/** A tab as the server stores it. */
export interface ServerBrowserTab {
  id: string;
  url: string | null;
  title: string | null;
}

/** A user's tabs in one session as the server stores them. */
export interface ServerBrowserTabs {
  revision: number;
  tabs: ServerBrowserTab[];
}

const PUSH_DELAY_MS = 1000;

const pushTimers = new Map<string, ReturnType<typeof setTimeout>>();
// Serializes each session's fetches and saves so one never races another.
const queues = new Map<string, Promise<void>>();
const adoptedListeners = new Set<(conversationId: string) => void>();

function browserTabsPath(conversationId: string): string {
  return `/v1/sessions/${encodeURIComponent(conversationId)}/browser-tabs`;
}

/** Whether this client and server both keep tabs and the session exists on the server. */
async function syncEnabled(conversationId: string): Promise<boolean> {
  if (!supportsBrowser() || isTempConvId(conversationId)) return false;
  return (await resolveServerInfo()).session_browser_tabs === true;
}

/** The session's local tabs in the server's shape. */
export function localBrowserTabs(conversationId: string): ServerBrowserTab[] {
  const state = readSessionWorkspaceState(conversationId);
  return (state.openBrowsers ?? []).map((id) => {
    const page = state.browserTabPages?.[id];
    return { id, url: page?.url ?? null, title: page?.title ?? null };
  });
}

function sameTabs(left: ServerBrowserTab[], right: ServerBrowserTab[]): boolean {
  return (
    left.length === right.length &&
    left.every(
      (tab, index) =>
        tab.id === right[index].id &&
        tab.url === right[index].url &&
        tab.title === right[index].title,
    )
  );
}

/** Replace the session's local tabs with the server's, keeping the selection if it survives. */
function adopt(conversationId: string, server: ServerBrowserTabs): void {
  const ids = server.tabs.map((tab) => tab.id);
  const pages: Record<string, BrowserTabPage> = {};
  for (const tab of server.tabs) {
    if (tab.url) pages[tab.id] = tab.title ? { url: tab.url, title: tab.title } : { url: tab.url };
  }
  const selected = readSessionWorkspaceState(conversationId).selectedBrowserId;
  writeSessionWorkspaceState(conversationId, {
    openBrowsers: ids,
    browserTabPages: pages,
    selectedBrowserId: selected && ids.includes(selected) ? selected : null,
    browserTabsRevision: server.revision,
  });
  for (const listener of adoptedListeners) listener(conversationId);
}

async function fetchServerTabs(conversationId: string): Promise<ServerBrowserTabs> {
  const response = await authenticatedFetch(browserTabsPath(conversationId));
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as ServerBrowserTabs;
}

/** Save the local tabs on top of ``baseRevision``; a stale save adopts the server's tabs. */
async function push(conversationId: string, baseRevision: number): Promise<void> {
  const response = await authenticatedFetch(browserTabsPath(conversationId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_revision: baseRevision, tabs: localBrowserTabs(conversationId) }),
  });
  if (response.status === 409) {
    adopt(conversationId, await fetchServerTabs(conversationId));
    return;
  }
  if (!response.ok) throw await apiErrorFromResponse(response);
  const saved = (await response.json()) as ServerBrowserTabs;
  writeSessionWorkspaceState(conversationId, { browserTabsRevision: saved.revision });
}

async function reconcile(conversationId: string): Promise<void> {
  const server = await fetchServerTabs(conversationId);
  const revision = readSessionWorkspaceState(conversationId).browserTabsRevision ?? 0;
  if (server.revision > revision) {
    adopt(conversationId, server);
  } else if (!sameTabs(localBrowserTabs(conversationId), server.tabs)) {
    // Local edits the server lacks, including tabs opened before syncing existed.
    await push(conversationId, server.revision);
  } else if (server.revision !== revision) {
    writeSessionWorkspaceState(conversationId, { browserTabsRevision: server.revision });
  }
}

function enqueue(conversationId: string, task: () => Promise<void>): Promise<void> {
  const previous = queues.get(conversationId) ?? Promise.resolve();
  // A failed task rejects its own caller; the next task still runs.
  const next = previous.catch(() => undefined).then(task);
  queues.set(conversationId, next);
  return next;
}

/**
 * Bring a session's local tabs and the server's in line: adopt newer server
 * tabs, or save local edits. Resolves without a request when either side does
 * not keep tabs.
 *
 * @throws ApiError when the server rejects a fetch or save.
 */
export async function syncBrowserTabs(conversationId: string): Promise<void> {
  if (!(await syncEnabled(conversationId))) return;
  await enqueue(conversationId, () => reconcile(conversationId));
}

/** Report a background sync failure; the local tabs stay as they are. */
function reportSyncFailure(conversationId: string, error: unknown): void {
  console.error(`[browser tabs] sync failed for session ${conversationId}`, error);
}

/** Start a sync without awaiting it, reporting a failure to the console. */
export function syncBrowserTabsInBackground(conversationId: string): void {
  syncBrowserTabs(conversationId).catch((error: unknown) =>
    reportSyncFailure(conversationId, error),
  );
}

/** Save the session's tabs after local edits settle. */
export function scheduleBrowserTabsPush(conversationId: string): void {
  clearTimeout(pushTimers.get(conversationId));
  pushTimers.set(
    conversationId,
    setTimeout(() => {
      pushTimers.delete(conversationId);
      const save = async () => {
        if (!(await syncEnabled(conversationId))) return;
        await enqueue(conversationId, () =>
          push(conversationId, readSessionWorkspaceState(conversationId).browserTabsRevision ?? 0),
        );
      };
      save().catch((error: unknown) => reportSyncFailure(conversationId, error));
    }, PUSH_DELAY_MS),
  );
}

/** Subscribe to sessions whose local tabs were just replaced by the server's. */
export function onBrowserTabsAdopted(listener: (conversationId: string) => void): () => void {
  adoptedListeners.add(listener);
  return () => adoptedListeners.delete(listener);
}
