// Browser-local "viewed" marks for pull request files, scoped to one server,
// user, pull request and head revision. A new head resets the marks.

import { useCallback, useEffect, useMemo, useState } from "react";
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
const INDEX_KEY = `${KEY_PREFIX}index`;
const MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000;
const MAX_ENTRIES = 500;

// Path patterns capture the repository and number; any suffix (e.g. `/files`) is ignored.
const PULL_REQUEST_PATHS: readonly [provider: string, pattern: RegExp][] = [
  ["github", /^\/([^/]+\/[^/]+)\/pull\/(\d+)(?:\/|$)/],
  ["gitlab", /^\/(.+?)\/-\/merge_requests\/(\d+)(?:\/|$)/],
  ["azure_devops", /^\/(.+?\/_git\/[^/]+)\/pullrequest\/(\d+)(?:\/|$)/i],
];

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
  for (const [id, pattern] of PULL_REQUEST_PATHS) {
    if (provider && provider !== id) continue;
    const match = pattern.exec(parsed.pathname);
    if (match) {
      return {
        provider: id,
        host: parsed.host.toLowerCase(),
        repository: match[1].toLowerCase(),
        number: Number(match[2]),
      };
    }
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
  headSha: z.string(),
  paths: z.array(z.string()),
  updatedAt: z.number(),
});
const indexSchema = z.record(z.string(), z.number());

// Values whose localStorage write failed; they shadow storage for this page's lifetime.
const memoryFallback = new Map<string, string>();

function readStored(key: string): z.infer<typeof storedSchema> | null {
  let raw = memoryFallback.get(key) ?? null;
  try {
    raw ??= window.localStorage.getItem(key);
    const result = storedSchema.safeParse(JSON.parse(raw ?? "null"));
    return result.success ? result.data : null;
  } catch {
    return null;
  }
}

/** Viewed paths for `headSha`; a different stored head reads as empty and reports it. */
export function readViewed(
  key: string,
  headSha: string,
): { paths: Set<string>; resetFromHead: string | null } {
  const stored = readStored(key);
  if (stored === null) return { paths: new Set(), resetFromHead: null };
  if (stored.headSha !== headSha) return { paths: new Set(), resetFromHead: stored.headSha };
  return { paths: new Set(stored.paths), resetFromHead: null };
}

function readIndex(): Record<string, number> {
  try {
    const result = indexSchema.safeParse(
      JSON.parse(window.localStorage.getItem(INDEX_KEY) ?? "{}"),
    );
    return result.success ? result.data : {};
  } catch {
    return {};
  }
}

/** Record `key` as just used and drop entries past the age and count limits. */
function touchIndex(key: string, now: number): void {
  const entries = Object.entries({ ...readIndex(), [key]: now }).filter(([entryKey]) =>
    entryKey.startsWith(KEY_PREFIX),
  );
  entries.sort((a, b) => b[1] - a[1]);
  const kept = entries.filter(
    ([, at], position) => position < MAX_ENTRIES && now - at <= MAX_AGE_MS,
  );
  const keptKeys = new Set(kept.map(([entryKey]) => entryKey));
  for (const [entryKey] of entries) {
    if (!keptKeys.has(entryKey)) {
      window.localStorage.removeItem(entryKey);
      memoryFallback.delete(entryKey);
    }
  }
  window.localStorage.setItem(INDEX_KEY, JSON.stringify(Object.fromEntries(kept)));
}

/** Store the viewed paths for `headSha`, replacing any other head; keeps them in memory if storage fails. */
export function writeViewed(
  key: string,
  headSha: string,
  paths: Iterable<string>,
): { persisted: boolean } {
  const updatedAt = Date.now();
  const value = JSON.stringify({ v: 1, headSha, paths: [...new Set(paths)], updatedAt });
  try {
    touchIndex(key, updatedAt);
    window.localStorage.setItem(key, value);
    memoryFallback.delete(key);
    return { persisted: true };
  } catch {
    memoryFallback.set(key, value);
    return { persisted: false };
  }
}

const EMPTY: ReadonlySet<string> = new Set();

interface LoadedViewed {
  key: string | null;
  headSha: string | null;
  viewed: ReadonlySet<string>;
  resetNotice: boolean;
}

function loadViewed(key: string | null, headSha: string | null): LoadedViewed {
  if (key === null || headSha === null) return { key, headSha, viewed: EMPTY, resetNotice: false };
  const { paths, resetFromHead } = readViewed(key, headSha);
  return { key, headSha, viewed: paths, resetNotice: resetFromHead !== null };
}

export function useViewedFiles(
  pr: PullRequestIdentity | null,
  headSha: string | null,
): {
  available: boolean;
  viewed: ReadonlySet<string>;
  toggle: (path: string) => void;
  setMany: (paths: string[], viewed: boolean) => void;
  resetNotice: boolean;
  persisted: boolean;
} {
  const server = getOmnigentServerIdentity();
  const user = useViewerId();
  const key = server && user && pr ? viewedStorageKey(server, user, pr) : null;
  const head = headSha || null;
  const [loaded, setLoaded] = useState(() => loadViewed(key, head));
  const [persisted, setPersisted] = useState(true);
  let current = loaded;
  if (loaded.key !== key || loaded.headSha !== head) {
    current = loadViewed(key, head);
    setLoaded(current);
  }

  const showPaths = useCallback(
    (paths: ReadonlySet<string>) =>
      setLoaded((previous) =>
        previous.key === key && previous.headSha === head
          ? { ...previous, viewed: paths }
          : previous,
      ),
    [key, head],
  );

  useEffect(() => {
    if (key === null || head === null) return;
    // Other tabs' writes arrive only as `storage` events; a null key means storage.clear().
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === key) showPaths(readViewed(key, head).paths);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [key, head, showPaths]);

  const update = useCallback(
    (change: (paths: Set<string>) => void) => {
      if (key === null || head === null) return;
      // Start from storage so back-to-back calls and other tabs' writes are not lost.
      const { paths } = readViewed(key, head);
      change(paths);
      setPersisted(writeViewed(key, head, paths).persisted);
      showPaths(paths);
    },
    [key, head, showPaths],
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

  return useMemo(
    () => ({
      available: current.key !== null && current.headSha !== null,
      viewed: current.viewed,
      toggle,
      setMany,
      resetNotice: current.resetNotice,
      persisted,
    }),
    [current, toggle, setMany, persisted],
  );
}
