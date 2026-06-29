// localStorage-backed recent workspace directories, keyed per host
// (paths are host-specific). Feeds the combobox "Recent" group.

import { useCallback, useMemo, useState } from "react";

const STORAGE_KEY = "omnigent:recent-workspaces";
const MAX_PER_HOST = 8;

// Map of host_id -> most-recent-first list of absolute paths.
type RecentMap = Record<string, string[]>;

function readAll(): RecentMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object") return {};
    // Keep only string[] values; drop anything malformed so a
    // corrupted entry can't crash the picker.
    const out: RecentMap = {};
    for (const [host, list] of Object.entries(parsed as Record<string, unknown>)) {
      if (Array.isArray(list)) {
        out[host] = list.filter((x): x is string => typeof x === "string");
      }
    }
    return out;
  } catch {
    return {};
  }
}

function writeAll(map: RecentMap): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Quota exceeded or storage disabled — non-fatal; recents just
    // stop persisting until the next successful write.
  }
}

export interface RecentWorkspaces {
  /** Most-recent-first absolute paths used on this host. */
  recent: string[];
  /**
   * Record ``path`` as the newest recent for the current host.
   * De-duplicates (moves an existing entry to the front) and caps
   * the list. No-op when ``hostId`` is null or the path is blank.
   */
  addRecent: (path: string) => void;
}

/**
 * Track recently-used workspace directories for one host.
 *
 * @param hostId Host whose recents to read/write, e.g.
 *   ``"host_a1b2..."``. ``null`` yields an empty list and a no-op
 *   ``addRecent`` (nothing is host-scoped yet).
 * @returns The host's recent paths plus an ``addRecent`` recorder.
 */
export function useRecentWorkspaces(hostId: string | null): RecentWorkspaces {
  // Bumped by addRecent to recompute after a write; the host's list is read
  // synchronously below, not hydrated via an effect.
  const [revision, setRevision] = useState(0);

  // Read synchronously and keyed on hostId so ``recent`` is always consistent
  // with the current host on the same render. A prior effect-based hydration
  // lagged one render behind hostId, which let a consumer briefly observe the
  // previous host's paths right after a host switch (a cross-host leak).
  const recent = useMemo(
    () => (hostId === null ? [] : (readAll()[hostId] ?? [])),
    [hostId, revision],
  );

  const addRecent = useCallback(
    (path: string) => {
      if (hostId === null) return;
      const trimmed = path.trim();
      if (!trimmed) return;
      const all = readAll();
      const existing = all[hostId] ?? [];
      const next = [trimmed, ...existing.filter((p) => p !== trimmed)].slice(0, MAX_PER_HOST);
      all[hostId] = next;
      writeAll(all);
      setRevision((r) => r + 1);
    },
    [hostId],
  );

  return { recent, addRecent };
}
