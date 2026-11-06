// Browser-local viewed marks for a pull request's files. A mark records the hunk hash a file was
// viewed at, so the file stays viewed on a new head while its hunks are unchanged. Marks are kept
// per server, user and pull request, whichever head the reader is on.

import type { FileDiffMetadata } from "@pierre/diffs";
import { useCallback, useMemo, useSyncExternalStore } from "react";
import { z } from "zod";
import type { PullRequestAssociation } from "@/hooks/usePullRequests";
import { useViewerId } from "@/hooks/useViewerId";
import { getOmnigentServerIdentity } from "./host";

/** A PR as the server identified it; associations carry these fields already normalized. */
export type PullRequestIdentity = Pick<
  PullRequestAssociation,
  "provider" | "host" | "repository" | "number"
>;

/** The hunk hashes each marked file was viewed at, oldest first. */
export type ViewedMarks = Record<string, string[]>;

const KEY_PREFIX = "omnigent:pr-viewed:v2:";
// Version 1 stored bare paths per head. Nothing reads it, and pruning removes it.
const LEGACY_KEY_PREFIX = "omnigent:pr-viewed:v1:";
const UNVERIFIED_PREFIX = "unverified:";
const MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000;
const MAX_ENTRIES = 500;
const MAX_HASHES_PER_PATH = 3;

export function viewedStorageKey(server: string, user: string, pr: PullRequestIdentity): string {
  return `${KEY_PREFIX}${JSON.stringify([
    server,
    user,
    // Hosts that predate providers serve GitHub.
    pr.provider ?? "github",
    pr.host.toLowerCase(),
    pr.repository.toLowerCase(),
    pr.number,
  ])}`;
}

/**
 * Stands in for a file's hash when its hunks can't be compared: the file is missing from the patch,
 * has no text hunks, or its hunks were cut short. It matches only on the head it was made for.
 */
export function unverifiedPatchHash(headSha: string): string {
  return `${UNVERIFIED_PREFIX}${headSha}`;
}

function isVerified(hash: string): boolean {
  return !hash.startsWith(UNVERIFIED_PREFIX);
}

/** A 53-bit string hash (cyrb53): synchronous, and the same in every browser. */
function hashText(text: string): string {
  let hashA = 0xdeadbeef;
  let hashB = 0x41c6ce57;
  for (let index = 0; index < text.length; index++) {
    const code = text.charCodeAt(index);
    hashA = Math.imul(hashA ^ code, 2654435761);
    hashB = Math.imul(hashB ^ code, 1597334677);
  }
  hashA =
    Math.imul(hashA ^ (hashA >>> 16), 2246822507) ^ Math.imul(hashB ^ (hashB >>> 13), 3266489909);
  hashB =
    Math.imul(hashB ^ (hashB >>> 16), 2246822507) ^ Math.imul(hashA ^ (hashA >>> 13), 3266489909);
  return (4294967296 * (2097151 & hashB) + (hashA >>> 0)).toString(36);
}

/**
 * The hunk headers and lines of one parsed file, each line with its diff marker. Null when the file
 * has no hunks, or a hunk's lines disagree with its header, which means the patch was cut short.
 */
function hunkText(file: FileDiffMetadata): string | null {
  // A file with no hunks (binary, pure rename) has nothing to compare.
  if (!file.hunks?.length) return null;
  const lines: string[] = [];
  for (const hunk of file.hunks) {
    lines.push(
      `@@ -${hunk.deletionStart},${hunk.deletionCount} +${hunk.additionStart},${hunk.additionCount} @@`,
    );
    // One push per line: spreading a large hunk into push() exceeds the call-argument limit.
    let deletionTotal = 0;
    let additionTotal = 0;
    for (const part of hunk.hunkContent) {
      if (part.type === "context") {
        for (let offset = 0; offset < part.lines; offset++) {
          lines.push(` ${file.additionLines[part.additionLineIndex + offset]}`);
        }
        deletionTotal += part.lines;
        additionTotal += part.lines;
        continue;
      }
      for (let offset = 0; offset < part.deletions; offset++) {
        lines.push(`-${file.deletionLines[part.deletionLineIndex + offset]}`);
      }
      for (let offset = 0; offset < part.additions; offset++) {
        lines.push(`+${file.additionLines[part.additionLineIndex + offset]}`);
      }
      deletionTotal += part.deletions;
      additionTotal += part.additions;
    }
    if (deletionTotal !== hunk.deletionCount || additionTotal !== hunk.additionCount) return null;
  }
  return lines.join("\n");
}

/**
 * The hash of each parsed file's hunks, keyed by the parser's file name. Files without comparable
 * hunks are left out. Pass the map the Changes tab renders from, before any diff is expanded.
 */
export function patchFileHashes(
  filesByPath: ReadonlyMap<string, FileDiffMetadata>,
): ReadonlyMap<string, string> {
  const hashes = new Map<string, string>();
  for (const [path, file] of filesByPath) {
    const text = hunkText(file);
    if (text !== null) hashes.set(path, hashText(text));
  }
  return hashes;
}

const storedSchema = z.object({
  v: z.literal(2),
  marks: z.record(z.string(), z.array(z.string())),
  updatedAt: z.number(),
});

// This tab's changes that storage has not accepted, per key: each changed path's new hashes, or
// null when the path was dropped. Reads apply them over what storage holds, so other tabs' writes show.
const unsavedChanges = new Map<string, Map<string, string[] | null>>();
const listeners = new Set<() => void>();
// Bumped on every change this module can observe, so snapshots are recomputed only then.
let version = 0;

function notify(): void {
  version += 1;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function isStoredKey(key: string): boolean {
  return key.startsWith(KEY_PREFIX) || key.startsWith(LEGACY_KEY_PREFIX);
}

if (typeof window !== "undefined") {
  // Other tabs' writes arrive only as `storage` events; a null key means storage.clear().
  window.addEventListener("storage", (event) => {
    try {
      if (event.storageArea !== null && event.storageArea !== window.localStorage) return;
    } catch {
      // Unreadable storage: treat the event as ours and re-read.
    }
    // A cleared store discards unsaved changes. Another tab's write is applied under them on read.
    if (event.key === null) unsavedChanges.clear();
    else if (!isStoredKey(event.key)) return;
    notify();
  });
}

/** The entry stored under `key`, or null when it is absent, unreadable or not a version 2 entry. */
function readStored(key: string): z.infer<typeof storedSchema> | null {
  try {
    const raw = window.localStorage.getItem(key);
    const result = storedSchema.safeParse(JSON.parse(raw ?? "null"));
    return result.success ? result.data : null;
  } catch {
    return null;
  }
}

/** The marks storage holds for `key`, with this tab's unsaved changes applied over them. */
function readMarks(key: string): ViewedMarks {
  const marks = new Map(Object.entries(readStored(key)?.marks ?? {}));
  for (const [path, hashes] of unsavedChanges.get(key) ?? []) {
    if (hashes === null) marks.delete(path);
    else marks.set(path, hashes);
  }
  return Object.fromEntries(marks);
}

function storedKeys(): string[] {
  const keys = new Set(unsavedChanges.keys());
  try {
    for (let position = 0; position < window.localStorage.length; position++) {
      const key = window.localStorage.key(position);
      if (key !== null && isStoredKey(key)) keys.add(key);
    }
  } catch {
    // Unreadable storage: only in-memory values are known.
  }
  return [...keys];
}

/**
 * Removes entries past the age or count limit, and every version 1 entry, which is never read.
 * Unreadable entries count as oldest. `keepKey` and entries with unsaved changes are never removed.
 */
function pruneEntries(keepKey: string, now: number): void {
  // Parses every stored entry per write; cache their ages if this shows up in profiles.
  const entries = storedKeys()
    .filter((key) => key !== keepKey && !unsavedChanges.has(key))
    .map((key) => [key, readStored(key)?.updatedAt ?? -Infinity] as const)
    .sort((a, b) => b[1] - a[1]);
  try {
    entries.forEach(([key, updatedAt], position) => {
      if (position >= MAX_ENTRIES - 1 || now - updatedAt > MAX_AGE_MS) {
        window.localStorage.removeItem(key);
      }
    });
  } catch {
    // Pruning is best effort; the write reports whether storage works.
  }
}

/** The paths where `marks` differs from `stored`, as the changes `readMarks` applies over `stored`. */
function changesOver(stored: ViewedMarks, marks: ViewedMarks): Map<string, string[] | null> {
  const changes = new Map<string, string[] | null>();
  for (const path of new Set([...Object.keys(stored), ...Object.keys(marks)])) {
    if (JSON.stringify(stored[path] ?? null) !== JSON.stringify(marks[path] ?? null)) {
      changes.set(path, marks[path] ?? null);
    }
  }
  return changes;
}

/**
 * Store a pull request's marks under `key`. If storage refuses, the changes stay in this tab over
 * what storage holds until a write succeeds.
 */
export function writeViewed(key: string, marks: ViewedMarks): { persisted: boolean } {
  const updatedAt = Date.now();
  const value = JSON.stringify({ v: 2, marks, updatedAt });
  pruneEntries(key, updatedAt);
  let persisted = true;
  try {
    window.localStorage.setItem(key, value);
    unsavedChanges.delete(key);
  } catch {
    persisted = false;
    const changes = changesOver(readStored(key)?.marks ?? {}, marks);
    if (changes.size > 0) unsavedChanges.set(key, changes);
    else unsavedChanges.delete(key);
  }
  notify();
  return { persisted };
}

/**
 * Viewed when the current hash was marked. Changed when the current hunks can be compared and the
 * file was marked at a comparable hash that differs from them. Unverified history alone is silent.
 */
function classify(
  marks: ViewedMarks,
  fingerprintOf: (path: string) => string,
): { viewed: Set<string>; changed: Set<string> } {
  const viewed = new Set<string>();
  const changed = new Set<string>();
  for (const [path, hashes] of Object.entries(marks)) {
    const current = fingerprintOf(path);
    if (hashes.includes(current)) viewed.add(path);
    else if (isVerified(current) && hashes.some(isVerified)) changed.add(path);
  }
  return { viewed, changed };
}

interface ViewedSnapshot {
  /** False until server, user, PR and head are known; mutations do nothing meanwhile. */
  available: boolean;
  /** Marked files whose current hunks match a hash they were viewed at. */
  viewed: ReadonlySet<string>;
  /**
   * Marked files whose current hunks match none of their marked hashes. Covers files outside the
   * PR's list too, so callers show only the ones they render.
   */
  changed: ReadonlySet<string>;
  /** False while this tab's changes to the pull request are not yet stored. */
  persisted: boolean;
}

const UNAVAILABLE: ViewedSnapshot = {
  available: false,
  viewed: new Set(),
  changed: new Set(),
  persisted: true,
};

/** A `useSyncExternalStore` reader that returns the same snapshot until the store changes. */
function snapshotReader(
  storageKey: string | null,
  head: string | null,
  fingerprintOf: (path: string) => string,
): () => ViewedSnapshot {
  if (storageKey === null || head === null) return () => UNAVAILABLE;
  let seenVersion = -1;
  let snapshot = UNAVAILABLE;
  return () => {
    if (seenVersion !== version) {
      seenVersion = version;
      const { viewed, changed } = classify(readMarks(storageKey), fingerprintOf);
      snapshot = { available: true, viewed, changed, persisted: !unsavedChanges.has(storageKey) };
    }
    return snapshot;
  };
}

/**
 * Viewed state for the pull request. `patchHashes` holds each file's hash in the patch fetched for
 * `headSha` (see `patchFileHashes`); a file missing from it is unverified.
 */
export function useViewedFiles(
  pr: PullRequestIdentity | null,
  headSha: string | null,
  patchHashes: ReadonlyMap<string, string>,
): ViewedSnapshot & { setMany: (paths: string[], viewed: boolean) => void } {
  const server = getOmnigentServerIdentity();
  const user = useViewerId();
  const storageKey = server && user && pr ? viewedStorageKey(server, user, pr) : null;
  const head = headSha || null;
  const fingerprintOf = useMemo(
    () => (path: string) => patchHashes.get(path) ?? unverifiedPatchHash(head ?? ""),
    [patchHashes, head],
  );
  const getSnapshot = useMemo(
    () => snapshotReader(storageKey, head, fingerprintOf),
    [storageKey, head, fingerprintOf],
  );
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const setMany = useCallback(
    (paths: string[], viewed: boolean) => {
      if (storageKey === null || head === null) return;
      // Start from what storage holds now plus this tab's unsaved changes, so other tabs' writes survive.
      const after = new Map(Object.entries(readMarks(storageKey)));
      let modified = false;
      for (const path of paths) {
        if (!viewed) {
          // Unmarking forgets every hash, including one a stale view of this file still shows.
          if (after.delete(path)) modified = true;
          continue;
        }
        const hash = fingerprintOf(path);
        const hashes = after.get(path) ?? [];
        if (hashes.includes(hash)) continue;
        modified = true;
        // Stand-ins from other heads can never match here, so they don't push out a comparable hash.
        const comparable = hashes.filter(isVerified);
        after.set(path, [...comparable, hash].slice(-MAX_HASHES_PER_PATH));
      }
      if (modified) writeViewed(storageKey, Object.fromEntries(after));
    },
    [storageKey, head, fingerprintOf],
  );

  return useMemo(() => ({ ...snapshot, setMany }), [snapshot, setMany]);
}
