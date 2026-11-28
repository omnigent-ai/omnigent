// After the open session is archived, open what ⌃[ would: the latest live visit, then sidebar order.
// It waits for a loading list and stops waiting once the user moves on; without the recent-session
// host, as with quick_pick_palette off, it goes home.

import type { QueryClient } from "@tanstack/react-query";

import { useRecentSessionSwitchStore } from "@/hooks/useRecentSessionSwitch";
import {
  currentSidebarSessionId,
  navigateToPendingSession,
  useSidebarSessionOrderStore,
} from "@/hooks/useSidebarSessionOrder";
import type { Conversation, ProjectSummary } from "@/hooks/useConversations";
import { archiveLanding, type ArchiveLanding, leavingAncestor } from "@/lib/archiveLanding";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import { getSessionParent } from "@/lib/sessionHost";
import type { Session } from "@/lib/types";

import { newSessionInProjectPath } from "../projectCommands/useProjectPaletteCommands";

/** Where an archive was asked from, for its diagnostics record; ⌘K's host also runs ⇧⌘A. */
export type ArchiveNavigationSource = "palette" | "header_menu" | "sidebar_menu" | "bulk";

export interface ArchiveNavigationRecord {
  event: "session_archive_navigation";
  source: ArchiveNavigationSource;
  landed: "recent" | "sidebar_order" | "new_session" | "pending" | "list_failed" | "user_left";
}

/** Log where an archive took the user and why: closed codes only, never ids or titles. */
export function recordArchiveNavigation(record: ArchiveNavigationRecord): void {
  try {
    console.info("[omnigent] session archive navigation", record);
  } catch {
    // A console that throws must not stop the navigation.
  }
}

type Navigate = (path: string, options: { replace: boolean }) => void;

export interface ArchiveNavigationRequest {
  /** The sessions being archived; any open session under them leaves too. */
  leavingIds: readonly string[];
  source: ArchiveNavigationSource;
  navigate: Navigate;
  /** New session's path, in the archived session's project when it had one. */
  newSessionPath: string;
  queryClient: QueryClient;
}

interface PendingNavigation {
  request: ArchiveNavigationRequest;
  stop: () => void;
}

let pending: PendingNavigation | null = null;

/**
 * New session in `conversation`'s project, as New session from its page opens it. A filed
 * session names its project by id, from the cached project list; an older one by its label.
 */
export function newSessionPathFor(
  queryClient: QueryClient,
  conversation: Pick<Conversation, "labels" | "project_id">,
): string {
  const projectId = conversation.project_id;
  const project =
    (projectId
      ? queryClient
          .getQueryData<ProjectSummary[]>(["projects"])
          ?.find((summary) => summary.id === projectId)?.name
      : undefined) ?? conversation.labels?.[PROJECT_LABEL_KEY];
  return project ? newSessionInProjectPath(project) : "/";
}

function parentOf(queryClient: QueryClient, entryId: string): string | null {
  return (
    getSessionParent(entryId) ??
    queryClient.getQueryData<Session>(["session", entryId])?.parentSessionId ??
    null
  );
}

/** Which of `leavingIds` takes the open session with it: itself or its nearest ancestor. */
export function leavingSessionOfOpenView(
  queryClient: QueryClient,
  openSessionId: string | null | undefined,
  leavingIds: readonly string[],
): string | null {
  if (!openSessionId) return null;
  return leavingAncestor(openSessionId, new Set(leavingIds), (entryId) =>
    parentOf(queryClient, entryId),
  );
}

function landingNow(
  { leavingIds, queryClient }: ArchiveNavigationRequest,
  readLiveSessionIds: () => ReadonlySet<string>,
): ArchiveLanding {
  const { history, sessionsKnown, sessionsFailed } = useRecentSessionSwitchStore.getState();
  const live = readLiveSessionIds();
  return archiveLanding(new Set(leavingIds), {
    history,
    sidebarEntryIds: useSidebarSessionOrderStore.getState().orderedSessionIds,
    isLive: (entryId) => live.has(entryId),
    parentOf: (entryId) => parentOf(queryClient, entryId),
    listState: listStateOf(sessionsKnown, sessionsFailed),
  });
}

function listStateOf(known: boolean, failed: boolean): "loading" | "failed" | "loaded" {
  if (!known) return "loading";
  return failed ? "failed" : "loaded";
}

/** Go where `landing` says; false while there is nowhere to go yet. */
function follow(request: ArchiveNavigationRequest, landing: ArchiveLanding): boolean {
  if (landing.kind === "pending" || landing.kind === "list_failed") {
    recordArchiveNavigation({
      event: "session_archive_navigation",
      source: request.source,
      landed: landing.kind,
    });
    return false;
  }
  if (landing.kind === "new_session") {
    request.navigate(request.newSessionPath, { replace: true });
    recordArchiveNavigation({
      event: "session_archive_navigation",
      source: request.source,
      landed: "new_session",
    });
    return true;
  }
  navigateToPendingSession(landing.entryId, `/c/${landing.entryId}`, (path) =>
    request.navigate(path, { replace: true }),
  );
  recordArchiveNavigation({
    event: "session_archive_navigation",
    source: request.source,
    landed: landing.landed,
  });
  return true;
}

function cancelPendingNavigation(): void {
  pending?.stop();
  pending = null;
}

/**
 * Leave the archived open session for the entry ⌃[ would open. Call it when the archive is
 * committed, after any confirmation. A later archive replaces a choice still waiting.
 */
export function navigateAfterArchiving(request: ArchiveNavigationRequest): void {
  cancelPendingNavigation();
  const readLiveSessionIds = useRecentSessionSwitchStore.getState().readLiveSessionIds;
  // There is no ⌃[ order to follow, so go home, as without quick_pick_palette.
  if (readLiveSessionIds === null) {
    request.navigate("/", { replace: true });
    return;
  }
  const landing = landingNow(request, readLiveSessionIds);
  if (follow(request, landing)) return;
  const openAtCommit = currentSidebarSessionId();
  let waitingFor = landing.kind;
  const record = (landed: ArchiveNavigationRecord["landed"]) =>
    recordArchiveNavigation({
      event: "session_archive_navigation",
      source: request.source,
      landed,
    });
  // The first time the user opens another entry, the choice is theirs, even if they come back.
  // A sidebar that stops publishing, as when it unmounts, says nothing about the route.
  const stopWatchingRoute = useSidebarSessionOrderStore.subscribe((state) => {
    if (pending === null || !state.published) return;
    if (currentSidebarSessionId() === openAtCommit) return;
    record("user_left");
    cancelPendingNavigation();
  });
  // Choose again whenever the list's state changes, until it can.
  const stopWatchingList = useRecentSessionSwitchStore.subscribe((state, previous) => {
    if (
      pending === null ||
      (state.sessionsKnown === previous.sessionsKnown &&
        state.sessionsFailed === previous.sessionsFailed &&
        state.readLiveSessionIds === previous.readLiveSessionIds)
    ) {
      return;
    }
    // The host that registered the list has gone: no ⌃[ order is left to follow.
    if (state.readLiveSessionIds === null) {
      cancelPendingNavigation();
      return;
    }
    const next = landingNow(request, state.readLiveSessionIds);
    if (next.kind === "pending" || next.kind === "list_failed") {
      // Record each move into or out of failure once, not every refetch.
      if (next.kind !== waitingFor) record(next.kind);
      waitingFor = next.kind;
      return;
    }
    cancelPendingNavigation();
    follow(request, next);
  });
  pending = {
    request,
    stop: () => {
      stopWatchingRoute();
      stopWatchingList();
    },
  };
}

/** Why a session row's action leaves the view that shows it. */
export type SessionLeaveCause = "archive" | "delete" | "leave";

/**
 * Leave the open `conversation`'s view: an archive opens the session ⌃[ would, and a delete or
 * a leave goes home.
 */
export function leaveSessionView(
  cause: SessionLeaveCause,
  {
    conversation,
    source,
    navigate,
    queryClient,
  }: {
    conversation: Pick<Conversation, "id" | "labels" | "project_id">;
    source: ArchiveNavigationSource;
    navigate: Navigate;
    queryClient: QueryClient;
  },
): void {
  if (cause !== "archive") {
    navigate("/", { replace: true });
    return;
  }
  navigateAfterArchiving({
    leavingIds: [conversation.id],
    source,
    navigate,
    newSessionPath: newSessionPathFor(queryClient, conversation),
    queryClient,
  });
}

/** Whether an archive is still waiting for the session list to choose where to go. */
export function hasPendingArchiveNavigation(): boolean {
  return pending !== null;
}

/** Drop any waiting choice, as between tests. */
export function resetArchiveNavigationForTests(): void {
  cancelPendingNavigation();
}
