// Client-side tracking of which file comments the user has seen.
//
// Stores { commentId: wallClockSeconds } in localStorage. A comment is
// recorded as seen when the user opens the comments panel on its file
// in the file browser (FileViewer marks every comment on the viewed
// file while the panel is open — opening the file alone is not
// enough); the inbox lists draft comments NOT in this registry.
// Unlike `useUnseenConversations` (a per-conversation timestamp
// watermark), tracking is per-comment-id: a new comment on an
// already-viewed file must still surface, and a seen comment must
// never resurface.
//
// Reads are exposed through `useSeenCommentIds` (useSyncExternalStore)
// so marking a comment seen in the FileViewer immediately clears it
// from the inbox page and the sidebar badge in the same tab.

import { useSyncExternalStore } from "react";

const STORAGE_KEY = "omnigent:seen-comment-ids";

// Seen ids accumulate forever (comments have no client-visible
// lifecycle end), so cap the registry and drop the oldest entries
// past it. A dropped id only resurfaces if its comment is still an
// unaddressed draft after 1000 newer comments were seen — accepted.
const MAX_ENTRIES = 1_000;

type SeenMap = Record<string, number>;

function readSeenMap(): SeenMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return {};
    }
    return parsed as SeenMap;
  } catch {
    return {};
  }
}

function writeSeenMap(map: SeenMap): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}

// Snapshot cache keyed by the raw localStorage string, so render-time
// getSnapshot returns a referentially-equal Set while the stored value
// is unchanged (useSyncExternalStore requires a stable snapshot), yet
// self-heals when storage changes underneath (another tab, test reset).
let _cachedRaw: string | null = null;
let _cachedSet: ReadonlySet<string> = new Set();
const _listeners = new Set<() => void>();

/** Current set of seen comment ids, as persisted in localStorage. */
export function getSeenCommentIds(): ReadonlySet<string> {
  if (typeof window === "undefined") return _cachedSet;
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    // Access errors (e.g. disabled storage) fall through to the cache.
  }
  if (raw !== _cachedRaw) {
    _cachedRaw = raw;
    _cachedSet = new Set(Object.keys(readSeenMap()));
  }
  return _cachedSet;
}

function subscribe(listener: () => void): () => void {
  _listeners.add(listener);
  // Same-tab writes notify through _listeners (markCommentsSeen).
  // Writes from OTHER tabs only surface as the window `storage` event
  // (the browser fires it in every tab except the writer) — without
  // this, an inbox tab keeps listing a comment that a second tab's
  // FileViewer already marked seen, since the app disables
  // refetchOnWindowFocus and nothing else re-renders the page.
  // key === null means storage.clear(); treat it as a change too.
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === STORAGE_KEY) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    _listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

/**
 * Record the given comment ids as seen (idempotent). Notifies
 * `useSeenCommentIds` subscribers so inbox surfaces update in place.
 */
export function markCommentsSeen(commentIds: string[]): void {
  if (commentIds.length === 0) return;
  const map = readSeenMap();
  const now = Math.floor(Date.now() / 1000);
  let changed = false;
  for (const id of commentIds) {
    if (map[id] === undefined) {
      map[id] = now;
      changed = true;
    }
  }
  if (!changed) return;
  const entries = Object.entries(map);
  if (entries.length > MAX_ENTRIES) {
    entries.sort((a, b) => b[1] - a[1]);
    entries.length = MAX_ENTRIES;
    writeSeenMap(Object.fromEntries(entries));
  } else {
    writeSeenMap(map);
  }
  _listeners.forEach((listener) => listener());
}

/** Reactive set of seen comment ids (current tab + persisted). */
export function useSeenCommentIds(): ReadonlySet<string> {
  return useSyncExternalStore(subscribe, getSeenCommentIds, getSeenCommentIds);
}
