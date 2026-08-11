// localStorage-backed set of harnesses the user has actually launched. Lets the
// picker promote a harness someone uses regularly (Pi, Cursor) into the primary
// list alongside the fully supported ones, instead of leaving it behind "More"
// forever.

import { useCallback, useMemo, useState } from "react";

const STORAGE_KEY = "omnigent:recent-harnesses";
const MAX_ENTRIES = 4;

/** Most-recent-first canonical harness ids, e.g. ``["pi-native", …]``. */
function readAll(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Drop anything malformed so a corrupted entry can't crash the picker.
    return parsed.filter((x): x is string => typeof x === "string");
  } catch {
    return [];
  }
}

function writeAll(list: string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
  } catch {
    // Quota exceeded or storage disabled — non-fatal; recents just stop
    // persisting until the next successful write.
  }
}

export interface RecentHarnesses {
  /** Most-recent-first harness ids the user has launched. */
  recentHarnesses: string[];
  /**
   * Record ``harness`` as the newest launched harness. De-duplicates (moves an
   * existing entry to the front) and caps the list. No-op for a blank id.
   */
  addRecentHarness: (harness: string) => void;
}

/**
 * Track which harnesses this user actually launches.
 *
 * Not host-scoped, unlike recent workspaces: a preference for Pi follows the
 * person across machines, whereas a workspace path is meaningful on one host.
 *
 * @returns The recent harness ids plus an ``addRecentHarness`` recorder.
 */
export function useRecentHarnesses(): RecentHarnesses {
  // Bumped by addRecentHarness to recompute after a write; the list is read
  // synchronously below rather than hydrated via an effect, so the picker never
  // renders one frame behind the stored value.
  const [revision, setRevision] = useState(0);

  const recentHarnesses = useMemo(() => {
    void revision;
    return readAll();
  }, [revision]);

  const addRecentHarness = useCallback((harness: string) => {
    const trimmed = harness.trim();
    if (!trimmed) return;
    const existing = readAll();
    // Already the newest entry → nothing to reorder, so skip the write and the
    // re-render it would trigger (the common case: relaunching the same harness).
    if (existing[0] === trimmed) return;
    writeAll([trimmed, ...existing.filter((h) => h !== trimmed)].slice(0, MAX_ENTRIES));
    setRevision((r) => r + 1);
  }, []);

  return { recentHarnesses, addRecentHarness };
}
