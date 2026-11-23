// The viewer's mute preferences. Once this tab saves one, its entry here outranks
// row labels, which rollbacks and lagging reads can leave stale.

import { useSyncExternalStore } from "react";
import { sessionBelongsToProject } from "@/shell/sidebarNav";

/**
 * The reserved `conversation_labels` key recording that the viewer muted a
 * session. Per-viewer on the server like the pin; absent when unmuted.
 * Mirrors the server's `MUTED_LABEL_KEY`.
 */
export const MUTED_LABEL_KEY = "omnigent.muted";

/**
 * How long after a save its value outranks server data. Lists, and the stream's
 * re-reads, can come from a search index that catches up after the PATCH; this
 * is the reindex window the delete tombstones and created keep-alive use too.
 */
export const MUTE_REINDEX_GRACE_MS = 60_000;

interface MuteEntry {
  muted: boolean;
  /** A save is in flight; a newer save for this session must wait. */
  saving: boolean;
  /** `performance.now()` when the latest save settled (infinite while saving). */
  settledAt: number;
}

interface LabeledRow {
  id: string;
  labels?: Record<string, string> | null;
  project_id?: string | null;
  owner?: string | null;
}

interface MutedProject {
  id: string;
  name: string;
}

const entries = new Map<string, MuteEntry>();
const recheckListeners = new Set<(id: string) => void>();
const recheckTimers = new Map<string, ReturnType<typeof setTimeout>>();
let mutedProjects: readonly MutedProject[] = [];
// Only the viewer's own sessions are filed in their projects.
let mutedProjectsViewerId: string | null = null;
const mutedProjectListeners = new Set<() => void>();

/**
 * Replace the viewer's muted projects from their project list. A folder without
 * a first-class id can't store a mute, so it never counts.
 */
export function setMutedProjects(
  projects: readonly { id: string | null; name: string; muted?: boolean }[],
  viewerId: string | null,
): void {
  const next = projects.flatMap((project) =>
    project.muted === true && project.id !== null ? [{ id: project.id, name: project.name }] : [],
  );
  const unchanged =
    viewerId === mutedProjectsViewerId &&
    next.length === mutedProjects.length &&
    next.every(
      (project, index) =>
        project.id === mutedProjects[index].id && project.name === mutedProjects[index].name,
    );
  if (unchanged) return;
  mutedProjects = next;
  mutedProjectsViewerId = viewerId;
  for (const listener of mutedProjectListeners) listener();
}

function subscribeMutedProjects(listener: () => void): () => void {
  mutedProjectListeners.add(listener);
  return () => {
    mutedProjectListeners.delete(listener);
  };
}

function mutedProjectsSnapshot(): readonly MutedProject[] {
  return mutedProjects;
}

/**
 * Re-render when the viewer mutes or unmutes a project; rows whose own data
 * didn't change otherwise keep a stale mute.
 *
 * @returns The current muted projects; a new array after each change.
 */
export function useMutedProjects(): readonly MutedProject[] {
  return useSyncExternalStore(subscribeMutedProjects, mutedProjectsSnapshot);
}

/** Whether the viewer muted this session itself, regardless of its project. */
export function isSessionMuted(row: LabeledRow): boolean {
  return entries.get(row.id)?.muted ?? !!row.labels?.[MUTED_LABEL_KEY];
}

/** Whether the session sits in the folder of a project the viewer muted. */
export function isInMutedProject(row: LabeledRow): boolean {
  return mutedProjects.some((project) =>
    sessionBelongsToProject(row, project, mutedProjectsViewerId),
  );
}

/** Capture before a server read starts; pass the result to {@link reconcileMuteLabel}. */
export function muteReadMarker(): number {
  return performance.now();
}

/**
 * Subscribe to sessions whose saved preference overrode disagreeing server data
 * inside the grace window. Each is reported once the window ends, so a re-read
 * can pick up a change made elsewhere meanwhile.
 *
 * @returns Unsubscribes the listener.
 */
export function onMuteRecheck(listener: (id: string) => void): () => void {
  recheckListeners.add(listener);
  return () => {
    recheckListeners.delete(listener);
  };
}

function cancelRecheck(id: string): void {
  clearTimeout(recheckTimers.get(id));
  recheckTimers.delete(id);
}

function scheduleRecheck(id: string, at: number): void {
  if (recheckTimers.has(id)) return;
  const timer = setTimeout(
    () => {
      recheckTimers.delete(id);
      for (const listener of recheckListeners) listener(id);
    },
    // Rounded up: a truncated delay could fire just before the window ends.
    Math.max(0, Math.ceil(at - performance.now())),
  );
  recheckTimers.set(id, timer);
}

/** Whether the viewer muted this session, directly or through its project. */
export function isConversationMuted(row: LabeledRow): boolean {
  return isSessionMuted(row) || isInMutedProject(row);
}

/** Whether unseen output surfaces as unread for this viewer; a muted session shows none. */
export function showsUnreadFor(row: LabeledRow, unseen: boolean): boolean {
  return unseen && !isConversationMuted(row);
}

/** Whether the viewer may flag the session unread; a muted session can't be. */
export function canFlagUnread(row: LabeledRow): boolean {
  return !isConversationMuted(row);
}

/**
 * Record a mute save starting. Returns false, leaving state unchanged, when
 * this session already has a save in flight.
 */
export function beginMuteSave(id: string, muted: boolean): boolean {
  const entry = entries.get(id);
  if (entry?.saving) return false;
  // This save supersedes whatever another device changed before it.
  cancelRecheck(id);
  entries.set(id, { muted, saving: true, settledAt: Number.POSITIVE_INFINITY });
  return true;
}

/** Record the saved (or, after a failure, restored) preference. */
export function settleMuteSave(id: string, muted: boolean): void {
  entries.set(id, { muted, saving: false, settledAt: performance.now() });
}

/** Whether any session has a recorded preference, so fetched rows need reconciling. */
export function hasMuteEntries(): boolean {
  return entries.size > 0;
}

/**
 * Reconcile a server row's mute label with the recorded preference. Data
 * requested {@link MUTE_REINDEX_GRACE_MS} or more after the latest save replaces
 * the entry; earlier data, or data arriving while a save is in flight, takes the
 * recorded value instead. Disagreeing data read after the save settled is
 * reported to {@link onMuteRecheck} listeners when the window ends.
 *
 * @param readMarker - {@link muteReadMarker} captured when the read started.
 * @returns `row` itself when it already agrees, else a copy with the label fixed.
 */
export function reconcileMuteLabel<T extends LabeledRow>(row: T, readMarker: number): T {
  const entry = entries.get(row.id);
  // A partial row without labels has nothing to reconcile.
  if (entry === undefined || !row.labels) return row;
  const trustedFrom = entry.settledAt + MUTE_REINDEX_GRACE_MS;
  if (readMarker >= trustedFrom) {
    entry.muted = !!row.labels[MUTED_LABEL_KEY];
    return row;
  }
  if (!!row.labels[MUTED_LABEL_KEY] === entry.muted) return row;
  // Data read before the save settled predates it; only later reads may be news.
  if (readMarker >= entry.settledAt) scheduleRecheck(row.id, trustedFrom);
  const labels = Object.fromEntries(
    Object.entries(row.labels).filter(([key]) => key !== MUTED_LABEL_KEY),
  );
  if (entry.muted) labels[MUTED_LABEL_KEY] = "1";
  return { ...row, labels };
}

/** Forget recorded preferences between tests; switching viewer or server reloads the page. */
export function clearSessionMuteState(): void {
  for (const id of [...recheckTimers.keys()]) cancelRecheck(id);
  entries.clear();
  mutedProjects = [];
  mutedProjectsViewerId = null;
}
