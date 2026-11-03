// Browser-local "viewed" marks for pull request files, scoped to one server,
// user, pull request and head revision. A new head starts with no marks.

import { useCallback, useMemo, useSyncExternalStore } from "react";
import { z } from "zod";
import { useViewerId } from "@/hooks/useViewerId";
import { getOmnigentServerIdentity } from "./host";

export interface PullRequestIdentity {
  provider: string;
  host: string;
  repository: string;
  number: number;
}

const KEY_PREFIX = "omnigent:pr-viewed:v1:";
const MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000;
const MAX_ENTRIES = 500;
// Older heads stay so a tab still showing one can't overwrite marks for a newer head.
const MAX_HEADS = 3;

// Path patterns capture the repository and number; any suffix (e.g. `/files`) is ignored.
const GITHUB_PATH = /^\/([^/]+\/[^/]+)\/pull\/(\d+)(?:\/|$)/;
const GITLAB_PATH = /^\/(.+?)\/-\/merge_requests\/(\d+)(?:\/|$)/;
const AZURE_PATH = /^\/(?:(.+?)\/)?_git\/([^/]+)\/pullrequest\/(\d+)(?:\/|$)/i;
const AZURE_HOST = "dev.azure.com";
const LEGACY_AZURE_HOST = /^([^.]+)\.visualstudio\.com$/;

function pathIdentity(provider: string, url: URL, pattern: RegExp): PullRequestIdentity | null {
  const match = pattern.exec(url.pathname);
  if (!match) return null;
  return {
    provider,
    host: url.host.toLowerCase(),
    repository: match[1].toLowerCase(),
    number: Number(match[2]),
  };
}

/** Azure DevOps Services only, normalized to `dev.azure.com/{org}/{project}/{repo}` like the server. */
function azureIdentity(url: URL): PullRequestIdentity | null {
  const match = AZURE_PATH.exec(url.pathname);
  if (!match || url.port) return null;
  const prefix = match[1]?.split("/") ?? [];
  const host = url.hostname.toLowerCase();
  let org: string | undefined;
  if (host === AZURE_HOST) {
    org = prefix.shift();
  } else {
    org = LEGACY_AZURE_HOST.exec(host)?.[1];
    if (prefix[0]?.toLowerCase() === "defaultcollection") prefix.shift();
  }
  if (!org || prefix.length > 1) return null;
  const [, , repo, number] = match;
  // A project's default repository URL omits the project; it shares the repository's name.
  const project = prefix[0] ?? repo;
  return {
    provider: "azure_devops",
    host: AZURE_HOST,
    repository: `${org}/${project}/${repo}`.toLowerCase(),
    number: Number(number),
  };
}

const PARSERS = new Map<string, (url: URL) => PullRequestIdentity | null>([
  ["github", (url) => pathIdentity("github", url, GITHUB_PATH)],
  ["gitlab", (url) => pathIdentity("gitlab", url, GITLAB_PATH)],
  ["azure_devops", azureIdentity],
]);

/** Parse a GitHub, GitLab or Azure DevOps PR URL; null for anything else. */
export function pullRequestIdentityFromUrl(
  url: string,
  provider: string | null,
): PullRequestIdentity | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
  const parsers = provider ? [PARSERS.get(provider)] : [...PARSERS.values()];
  for (const parse of parsers) {
    const identity = parse?.(parsed);
    if (identity) return identity;
  }
  return null;
}

export function viewedStorageKey(server: string, user: string, pr: PullRequestIdentity): string {
  return `${KEY_PREFIX}${JSON.stringify([
    server,
    user,
    pr.provider,
    pr.host.toLowerCase(),
    pr.repository.toLowerCase(),
    pr.number,
  ])}`;
}

const storedSchema = z.object({
  v: z.literal(1),
  heads: z.record(z.string(), z.object({ paths: z.array(z.string()), updatedAt: z.number() })),
});
type StoredHeads = z.infer<typeof storedSchema>["heads"];

// Values whose localStorage write failed. They win over storage until another tab changes it.
const memoryFallback = new Map<string, string>();
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

if (typeof window !== "undefined") {
  // Other tabs' writes arrive only as `storage` events; a null key means storage.clear().
  window.addEventListener("storage", (event) => {
    if (event.key === null) memoryFallback.clear();
    else if (event.key.startsWith(KEY_PREFIX)) memoryFallback.delete(event.key);
    else return;
    notify();
  });
}

function storedRaw(key: string): string | null {
  const memory = memoryFallback.get(key);
  if (memory !== undefined) return memory;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function parseHeads(raw: string | null): StoredHeads {
  try {
    const result = storedSchema.safeParse(JSON.parse(raw ?? "null"));
    return result.success ? result.data.heads : {};
  } catch {
    return {};
  }
}

function viewedFor(
  heads: StoredHeads,
  headSha: string,
): { paths: Set<string>; resetFromHead: string | null } {
  const current = Object.hasOwn(heads, headSha) ? heads[headSha] : undefined;
  if (current) return { paths: new Set(current.paths), resetFromHead: null };
  const [latestOther] = Object.entries(heads).sort((a, b) => b[1].updatedAt - a[1].updatedAt);
  return { paths: new Set(), resetFromHead: latestOther?.[0] ?? null };
}

/** Viewed paths for `headSha`; when only other heads have marks, `resetFromHead` names the latest. */
export function readViewed(
  key: string,
  headSha: string,
): { paths: Set<string>; resetFromHead: string | null } {
  return viewedFor(parseHeads(storedRaw(key)), headSha);
}

function lastUpdated(key: string): number {
  const times = Object.values(parseHeads(window.localStorage.getItem(key))).map(
    (head) => head.updatedAt,
  );
  return times.length > 0 ? Math.max(...times) : -Infinity;
}

/** Evict other entries past the age and count limits, scanning storage so no shared index can drift. */
function pruneEntries(currentKey: string, now: number): void {
  // ponytail: parses every stored entry (≤ MAX_ENTRIES) per write; keep a cached index if that shows up in profiles.
  try {
    const entries: [string, number][] = [];
    for (let position = 0; position < window.localStorage.length; position++) {
      const entryKey = window.localStorage.key(position);
      if (entryKey?.startsWith(KEY_PREFIX) && entryKey !== currentKey) {
        entries.push([entryKey, lastUpdated(entryKey)]);
      }
    }
    entries.sort((a, b) => b[1] - a[1]);
    entries.forEach(([entryKey, updatedAt], position) => {
      if (position >= MAX_ENTRIES - 1 || now - updatedAt > MAX_AGE_MS) {
        window.localStorage.removeItem(entryKey);
      }
    });
  } catch {
    // Pruning is best effort; the write reports whether storage works.
  }
}

/** Store the viewed paths for `headSha`; keeps them in memory if storage fails. */
export function writeViewed(
  key: string,
  headSha: string,
  paths: Iterable<string>,
): { persisted: boolean } {
  const updatedAt = Date.now();
  const otherHeads = Object.entries(parseHeads(storedRaw(key)))
    .filter(([sha]) => sha !== headSha)
    .sort((a, b) => b[1].updatedAt - a[1].updatedAt)
    .slice(0, MAX_HEADS - 1);
  const heads = Object.fromEntries([
    [headSha, { paths: [...new Set(paths)], updatedAt }],
    ...otherHeads,
  ]);
  const value = JSON.stringify({ v: 1, heads });
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
  /** True while this head has no marks but an earlier head did. */
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

/** A `useSyncExternalStore` reader that returns the same snapshot until the stored value changes. */
function snapshotReader(key: string | null, headSha: string | null): () => ViewedSnapshot {
  if (key === null || headSha === null) return () => UNAVAILABLE;
  let lastRaw: string | null | undefined;
  let lastInMemory = false;
  let snapshot = UNAVAILABLE;
  return () => {
    const raw = storedRaw(key);
    const inMemory = memoryFallback.has(key);
    if (raw !== lastRaw || inMemory !== lastInMemory) {
      lastRaw = raw;
      lastInMemory = inMemory;
      const { paths, resetFromHead } = viewedFor(parseHeads(raw), headSha);
      snapshot = {
        available: true,
        viewed: paths,
        resetNotice: resetFromHead !== null,
        persisted: !inMemory,
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
  const key = server && user && pr ? viewedStorageKey(server, user, pr) : null;
  const head = headSha || null;
  const getSnapshot = useMemo(() => snapshotReader(key, head), [key, head]);
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const update = useCallback(
    (change: (paths: Set<string>) => void) => {
      if (key === null || head === null) return;
      // Start from storage so back-to-back calls and other tabs' writes are not lost.
      const { paths } = readViewed(key, head);
      change(paths);
      writeViewed(key, head, paths);
    },
    [key, head],
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
