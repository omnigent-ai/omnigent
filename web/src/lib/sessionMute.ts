// The viewer's per-session mute preference.
//
// The server stores the preference as the `omnigent.muted` label, and row
// labels decide it until this tab mutes or unmutes a session. From then on,
// the tab's entry here decides: other features restore cached rows wholesale
// (a failed move's or archive's rollback, for example), so a cached label alone
// can go stale. An entry changes only through this tab's own saves, or through
// server data requested after the latest save finished.

/**
 * The reserved `conversation_labels` key recording that the viewer muted a
 * session. Per-viewer on the server like the pin; absent when unmuted.
 * Mirrors the server's `MUTED_LABEL_KEY`.
 */
export const MUTED_LABEL_KEY = "omnigent.muted";

interface MuteEntry {
  muted: boolean;
  /** A save is in flight, so server data can't replace the value yet. */
  saving: boolean;
  /** Server reads started before this marker predate the latest save. */
  settledAt: number;
}

interface LabeledRow {
  id: string;
  labels?: Record<string, string> | null;
}

let marker = 0;
const entries = new Map<string, MuteEntry>();

/** Capture before a server read starts; pass the result to {@link reconcileMuteLabel}. */
export function muteReadMarker(): number {
  return marker;
}

/**
 * Read marker for data with no known read time, such as a pushed frame. Such
 * data never replaces a recorded preference.
 */
export const UNORDERED_READ = Number.NEGATIVE_INFINITY;

/** Whether the viewer muted this session. */
export function isConversationMuted(row: LabeledRow): boolean {
  return entries.get(row.id)?.muted ?? !!row.labels?.[MUTED_LABEL_KEY];
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
  entries.set(id, { muted, saving: true, settledAt: entry?.settledAt ?? marker });
  return true;
}

/** Record the saved (or, after a failure, restored) preference. */
export function settleMuteSave(id: string, muted: boolean): void {
  marker += 1;
  entries.set(id, { muted, saving: false, settledAt: marker });
}

/** Whether `row` disagrees with a saved preference, so only a fresh read can settle it. */
export function contradictsSavedMute(row: LabeledRow): boolean {
  const entry = entries.get(row.id);
  return (
    entry !== undefined &&
    !entry.saving &&
    !!row.labels &&
    !!row.labels[MUTED_LABEL_KEY] !== entry.muted
  );
}

/** Whether any session has a recorded preference, so fetched rows need reconciling. */
export function hasMuteEntries(): boolean {
  return entries.size > 0;
}

/**
 * Reconcile a server row's mute label with the recorded preference. Data
 * requested after the latest save replaces the entry; older data, or data
 * arriving while a save is in flight, takes the recorded value instead.
 *
 * @param readMarker - {@link muteReadMarker} captured when the read started.
 * @returns `row` itself when it already agrees, else a copy with the label fixed.
 */
export function reconcileMuteLabel<T extends LabeledRow>(row: T, readMarker: number): T {
  const entry = entries.get(row.id);
  // A partial row without labels has nothing to reconcile.
  if (entry === undefined || !row.labels) return row;
  if (!entry.saving && readMarker >= entry.settledAt) {
    entry.muted = !!row.labels[MUTED_LABEL_KEY];
    return row;
  }
  if (!!row.labels[MUTED_LABEL_KEY] === entry.muted) return row;
  const labels = Object.fromEntries(
    Object.entries(row.labels).filter(([key]) => key !== MUTED_LABEL_KEY),
  );
  if (entry.muted) labels[MUTED_LABEL_KEY] = "1";
  return { ...row, labels };
}

/** Forget recorded preferences, e.g. when the session caches reset. */
export function clearSessionMuteState(): void {
  entries.clear();
}
