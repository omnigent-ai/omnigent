// Browser-local "viewed" marks for pull request files, scoped to one server,
// user, pull request and head revision. Each head has its own storage key, so
// a new head starts with no marks and writes for different heads never collide.

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

const KEY_PREFIX = "omnigent:pr-viewed:v1:";
const MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000;
const MAX_ENTRIES = 500;

function pullRequestKey(server: string, user: string, pr: PullRequestIdentity): string {
  return `${KEY_PREFIX}${JSON.stringify([
    server,
    user,
    // Hosts that predate providers serve GitHub.
    pr.provider ?? "github",
    pr.host.toLowerCase(),
    pr.repository.toLowerCase(),
    pr.number,
  ])}|`;
}

export function viewedStorageKey(
  server: string,
  user: string,
  pr: PullRequestIdentity,
  headSha: string,
): string {
  return `${pullRequestKey(server, user, pr)}${headSha}`;
}

const storedSchema = z.object({
  v: z.literal(1),
  paths: z.array(z.string()),
  updatedAt: z.number(),
});

// Values whose localStorage write failed. They win over storage until another tab changes it.
const memoryFallback = new Map<string, string>();
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

if (typeof window !== "undefined") {
  // Other tabs' writes arrive only as `storage` events; a null key means storage.clear().
  window.addEventListener("storage", (event) => {
    try {
      if (event.storageArea !== null && event.storageArea !== window.localStorage) return;
    } catch {
      // Unreadable storage: treat the event as ours and re-read.
    }
    if (event.key === null) memoryFallback.clear();
    else if (event.key.startsWith(KEY_PREFIX)) memoryFallback.delete(event.key);
    else return;
    notify();
  });
}

function readStored(key: string): z.infer<typeof storedSchema> | null {
  try {
    const raw = memoryFallback.get(key) ?? window.localStorage.getItem(key);
    const result = storedSchema.safeParse(JSON.parse(raw ?? "null"));
    return result.success ? result.data : null;
  } catch {
    return null;
  }
}

export function readViewed(key: string): Set<string> {
  return new Set(readStored(key)?.paths);
}

function storedKeys(): string[] {
  const keys = new Set(memoryFallback.keys());
  try {
    for (let position = 0; position < window.localStorage.length; position++) {
      const key = window.localStorage.key(position);
      if (key?.startsWith(KEY_PREFIX)) keys.add(key);
    }
  } catch {
    // Unreadable storage: only in-memory values are known.
  }
  return [...keys];
}

/** Evict other entries past the age and count limits; corrupt entries count as oldest. */
function pruneEntries(currentKey: string, now: number): void {
  // Parses every stored entry per write; cache their ages if this shows up in profiles.
  const entries = storedKeys()
    .filter((key) => key !== currentKey && !memoryFallback.has(key))
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

/**
 * Store the viewed paths under `key`; keeps them in memory if storage fails. localStorage
 * is not atomic across tabs, so two tabs writing one head within milliseconds can lose a change.
 */
export function writeViewed(key: string, paths: Iterable<string>): { persisted: boolean } {
  const updatedAt = Date.now();
  const value = JSON.stringify({ v: 1, paths: [...new Set(paths)], updatedAt });
  pruneEntries(key, updatedAt);
  let persisted = true;
  try {
    window.localStorage.setItem(key, value);
    memoryFallback.delete(key);
  } catch {
    memoryFallback.set(key, value);
    persisted = false;
  }
  notify();
  return { persisted };
}

interface ViewedSnapshot {
  /** False until server, user, PR and head are known; mutations do nothing meanwhile. */
  available: boolean;
  viewed: ReadonlySet<string>;
  /** True while marks were changed on another head of this PR but never on this one. */
  resetNotice: boolean;
  /** False while the marks live only in memory because storage failed. */
  persisted: boolean;
}

const UNAVAILABLE: ViewedSnapshot = {
  available: false,
  viewed: new Set(),
  resetNotice: false,
  persisted: true,
};

/** A `useSyncExternalStore` reader that returns the same snapshot until the store changes. */
function snapshotReader(pullRequest: string | null, headSha: string | null): () => ViewedSnapshot {
  if (pullRequest === null || headSha === null) return () => UNAVAILABLE;
  const key = `${pullRequest}${headSha}`;
  let seenVersion = -1;
  let snapshot = UNAVAILABLE;
  return () => {
    if (seenVersion !== version) {
      seenVersion = version;
      const stored = readStored(key);
      snapshot = {
        available: true,
        viewed: new Set(stored?.paths),
        resetNotice:
          stored === null &&
          storedKeys().some(
            (other) => other.startsWith(pullRequest) && other !== key && readStored(other),
          ),
        persisted: !memoryFallback.has(key),
      };
    }
    return snapshot;
  };
}

export function useViewedFiles(
  pr: PullRequestIdentity | null,
  headSha: string | null,
): ViewedSnapshot & {
  toggle: (path: string) => void;
  setMany: (paths: string[], viewed: boolean) => void;
} {
  const server = getOmnigentServerIdentity();
  const user = useViewerId();
  const pullRequest = server && user && pr ? pullRequestKey(server, user, pr) : null;
  const head = headSha || null;
  const getSnapshot = useMemo(() => snapshotReader(pullRequest, head), [pullRequest, head]);
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const update = useCallback(
    (change: (paths: Set<string>) => void) => {
      if (pullRequest === null || head === null) return;
      const key = `${pullRequest}${head}`;
      // Start from storage so back-to-back calls and other tabs' writes are not lost.
      const before = readViewed(key);
      const after = new Set(before);
      change(after);
      // Skipping no-op writes keeps "this head has a record" meaning "marks were changed here".
      if (after.size === before.size && [...after].every((path) => before.has(path))) return;
      writeViewed(key, after);
    },
    [pullRequest, head],
  );
  const toggle = useCallback(
    (path: string) =>
      update((paths) => {
        if (!paths.delete(path)) paths.add(path);
      }),
    [update],
  );
  const setMany = useCallback(
    (paths: string[], viewed: boolean) =>
      update((viewedPaths) => {
        for (const path of paths) {
          if (viewed) viewedPaths.add(path);
          else viewedPaths.delete(path);
        }
      }),
    [update],
  );

  return useMemo(() => ({ ...snapshot, toggle, setMany }), [snapshot, toggle, setMany]);
}
