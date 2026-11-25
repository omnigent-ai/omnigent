// A pull request's details read on github.com as this Mac's own account, or as a login the Pull
// Requests page picked, kept on this device: its overview and its review conversations, so a PR
// opened before shows at once after a restart and is read again behind it. One cache for both,
// so they share one budget and one least-recently-used order. A view's session isn't part of a
// kept key, so the page, the sidebar and a linked session restore the same entry. Conversations
// are kept only under the revision they were read at, each part as old as its own read.

import type { QueryKey, QueryPersister } from "@tanstack/react-query";
import { z } from "zod";

import { subscribeDesktopFeatureFlag } from "@/lib/desktopFeatureFlags";
import { subscribeDesktopReadPath } from "@/lib/desktopReadPath";
import { getCurrentUserId } from "@/lib/identity";
import {
  createMacDurableReadCache,
  durableServerScope,
  type MacDurableKeep,
} from "@/lib/macDurableReadCache";
import { namedReadsAvailable, namedReadsSettled, subscribeNamedReads } from "@/lib/macNamedReads";
import { macNamedReadScopeKey, macReadScopeKey } from "@/lib/macReadScope";
import { pullRequestReadRestAt, pullRequestRecordScope } from "@/lib/pullRequestRecord";
import { type CachedThreads, historyOf } from "@/lib/reviewThreadsHistory";
import {
  oldestReadAt,
  readsComplete,
  restoredReads,
  savedReadTimes,
  showsLive,
  type TimedThreadsPage,
} from "@/lib/reviewThreadsReadAt";

/** The flag that keeps a pull request's details on this device; off keeps none and drops them. */
export const PULL_REQUEST_SAVED_DETAILS_FLAG = "pull-request-saved-details";

const GITHUB_HOST = "github.com";
const STORAGE_PREFIX = "omnigent:pull-request-details";
/** Moves when the saved shape changes, so older entries are dropped. */
const SCHEMA = "3";
/** Kept this long after GitHub answered; showing it again never renews it. */
const MAX_AGE_MS = 14 * 24 * 60 * 60_000;
/** One PR's overview, at most; a larger one is read again instead. */
export const MAX_SAVED_OVERVIEW_CHARS = 64 * 1024;
/** One PR's conversations at one revision, at most. */
export const MAX_SAVED_THREADS_CHARS = 128 * 1024;
/** Every kept overview and conversation together, its index included, in UTF-16 code units. */
export const MAX_SAVED_DETAILS_TOTAL_CHARS = 2 * 1024 * 1024;
/** The most entries kept; the least recently used go first. */
export const MAX_SAVED_DETAILS_ENTRIES = 400;

const OVERVIEW = "pull_request_overview";
const THREADS = "review_threads";

/** A GitHub login as named scopes key it, lowercased. */
const LOGIN_PATTERN = /^[a-z0-9][a-z0-9_-]{0,99}$/;
const PULL_REQUEST_URL = /^https:\/\/github\.com\/[^/\s]+\/[^/\s]+\/pull\/\d+$/;
const SHA = /^[0-9a-f]{7,64}$/i;

let savedDetailsOn = true;

/** The read scope a view's in-memory key ends with: this Mac's account's or a named login's. */
function scopeAt(queryKey: QueryKey, start: number): readonly unknown[] | null {
  const head = queryKey[start];
  const length = head === "mac-read" ? 4 : head === "mac-read-as" ? 5 : 0;
  if (length === 0 || queryKey.length !== start + length) return null;
  return githubScope(queryKey.slice(start));
}

/** `scope` when it reads github.com, the only host whose details are kept. */
const githubScope = (scope: readonly unknown[]) =>
  scope[2] === "github" && scope[3] === GITHUB_HOST ? scope : null;

/** The key one PR's overview is kept under, read as `scope`. */
export function pullRequestOverviewKey(
  scope: readonly unknown[],
  prUrl: string,
  server: string | null = durableServerScope(),
) {
  return ["code-host-mac", OVERVIEW, "server", server, ...scope, prUrl] as const;
}

/** The key one PR's conversations at `revision` are kept under, read as `scope` for `account`. */
export function pullRequestThreadsKey(
  scope: readonly unknown[],
  prUrl: string,
  account: string,
  revision: string,
  server: string | null = durableServerScope(),
) {
  return ["code-host-mac", THREADS, "server", server, ...scope, prUrl, account, revision] as const;
}

/**
 * The kept key for a view's overview or conversations read on this Mac: a PR's shared record
 * (any reader's and epoch's: a restore proves its viewer against the scope's reader), or a
 * session's own `["github-info", session, prUrl, ...scope]` or
 * `["github-review-threads", session, prUrl, account, revision, ...scope]`. Null for anything
 * else, a host read included.
 */
export function pullRequestDetailsDurableKey(queryKey: QueryKey): QueryKey | null {
  const [root, , prUrl] = queryKey;
  if (typeof prUrl !== "string" || !PULL_REQUEST_URL.test(prUrl)) return null;
  if (pullRequestRecordScope(queryKey)) return recordDurableKey(queryKey, prUrl);
  if (root === "github-info") {
    const scope = scopeAt(queryKey, 3);
    return scope && pullRequestOverviewKey(scope, prUrl);
  }
  if (root === "github-review-threads") {
    const [, , , account, revision] = queryKey;
    const scope = scopeAt(queryKey, 5);
    if (!scope || typeof account !== "string" || typeof revision !== "string") return null;
    return pullRequestThreadsKey(scope, prUrl, account, revision);
  }
  return null;
}

/**
 * The kept key for a PR's shared record of its overview or conversations, by the parts after
 * its reader and epoch; null for any other key.
 */
function recordDurableKey(queryKey: QueryKey, prUrl: string): QueryKey | null {
  const recordScope = pullRequestRecordScope(queryKey);
  const scope = recordScope && githubScope(recordScope);
  if (!scope) return null;
  const rest = queryKey.slice(pullRequestReadRestAt(queryKey));
  if (queryKey[0] === "github-info") {
    // An overview's record has no parts of its own after its reader and epoch.
    return rest.length === 0 ? pullRequestOverviewKey(scope, prUrl) : null;
  }
  if (queryKey[0] !== "github-review-threads" || rest.length !== 2) return null;
  const [account, revision] = rest;
  if (typeof account !== "string" || typeof revision !== "string") return null;
  return pullRequestThreadsKey(scope, prUrl, account, revision);
}

/** The revision a kept key names, `base:head`, or null where it doesn't name a whole one. */
function revisionOf(revision: unknown): { base: string; head: string } | null {
  if (typeof revision !== "string") return null;
  const [base, head, extra] = revision.split(":");
  return extra === undefined && SHA.test(base ?? "") && SHA.test(head ?? "")
    ? { base: base!, head: head! }
    : null;
}

/**
 * Where a kept key's entry may be kept: the current server's and user's, read on github.com as
 * this Mac's own account or as a login the page named. Null otherwise, while the flag is off,
 * and for named logins once named reads are known to be off here.
 */
function keptScope(queryKey: QueryKey): MacDurableKeep | null {
  if (!savedDetailsOn) return null;
  const server = durableServerScope();
  const userId = getCurrentUserId();
  if (server === null || !userId || queryKey[0] !== "code-host-mac") return null;
  if (queryKey[2] !== "server" || queryKey[3] !== server) return null;
  const kind = queryKey[1];
  const tail = kind === OVERVIEW ? 1 : kind === THREADS ? 3 : 0;
  if (tail === 0) return null;
  const scope = queryKey.slice(4, queryKey.length - tail);
  const [prUrl, account, revision] = queryKey.slice(queryKey.length - tail);
  if (typeof prUrl !== "string" || !PULL_REQUEST_URL.test(prUrl)) return null;
  if (kind === THREADS && (typeof account !== "string" || !revisionOf(revision))) return null;
  const own = macReadScopeKey("github", GITHUB_HOST, userId);
  const same = (expected: readonly unknown[]) =>
    scope.length === expected.length && expected.every((part, index) => scope[index] === part);
  if (same(own)) return { scope: own, restorable: true };
  const login = scope[4];
  if (typeof login !== "string" || !LOGIN_PATTERN.test(login)) return null;
  if (namedReadsSettled() && !namedReadsAvailable()) return null;
  const named = macNamedReadScopeKey(GITHUB_HOST, login, userId);
  if (!same(named)) return null;
  return { scope: named, restorable: true, reader: { host: GITHUB_HOST, login } };
}

const readTime = z.number().finite().positive();
const commentSchema = z.object({ id: z.string().min(1), body: z.string() }).passthrough();
const threadSchema = z
  .object({
    id: z.string().min(1),
    comments: z
      .object({
        data: z.array(commentSchema),
        has_more: z.boolean(),
        next_cursor: z.string().nullable(),
      })
      .passthrough(),
  })
  .passthrough();
const savedDetailsSchema = z
  .discriminatedUnion("kind", [
    z.object({
      kind: z.literal("overview"),
      viewer: z.string().min(1),
      info: z
        .object({
          selected_pr_url: z.string(),
          dispatched_at: z.number().optional(),
          pr: z
            .object({
              url: z.string(),
              title: z.string(),
              number: z.number(),
              head_sha: z.string(),
              base_sha: z.string(),
            })
            .passthrough(),
        })
        .passthrough(),
    }),
    z.object({
      kind: z.literal("threads"),
      viewer: z.string().min(1),
      page: z
        .object({
          data: z.array(threadSchema),
          head_sha: z.string(),
          base_sha: z.string(),
          has_more: z.boolean(),
          next_cursor: z.string().nullable(),
          // What the next first page merges by, as in the run that saved it.
          first_page_ids: z.array(z.string()).optional(),
          paged_comment_ids: z.array(z.string()).optional(),
          history_stale: z.boolean().optional(),
        })
        .passthrough(),
      // When each part was read: a common time, and the parts read at another.
      read_times: z.object({ at: readTime, parts: z.record(z.string(), readTime).optional() }),
    }),
  ])
  .refine(
    // Kept conversations name a time only for parts they show, so a restore dates every one.
    (data) =>
      data.kind !== "threads" ||
      (restoredReads(data.page as unknown as TimedThreadsPage, data.read_times) !== undefined &&
        namesOnlyShownThreads(data.page)),
  );
export type SavedPullRequestDetails = z.infer<typeof savedDetailsSchema>;

/** Fields of an overview that belong to the session that read it, never kept. */
const SESSION_FIELDS = [
  "prs",
  "tracking_available",
  "host_lists_all",
  "provider_display",
  "followed_snapshot",
];
/** Fields of a conversations page that belong to this run, never kept; its history is kept pruned. */
const RUN_FIELDS = [
  "paged_comment_ids",
  "first_page_ids",
  "history_stale",
  "warning",
  "reads",
  "read_as",
];

/** Whether a kept page's merge history names only threads it shows. */
function namesOnlyShownThreads(page: {
  data: readonly { id: string }[];
  first_page_ids?: readonly string[];
  paged_comment_ids?: readonly string[];
}): boolean {
  const shown = new Set(page.data.map((thread) => thread.id));
  return [...(page.first_page_ids ?? []), ...(page.paged_comment_ids ?? [])].every((id) =>
    shown.has(id),
  );
}

/** A snapshot marked as restored from this device, until GitHub answers again. */
type Restorable = Record<string, unknown> & { restored?: true };

const without = (value: Record<string, unknown>, fields: readonly string[]) =>
  Object.fromEntries(Object.entries(value).filter(([field]) => !fields.includes(field)));

const isThreadsPage = (snapshot: Restorable): boolean =>
  Array.isArray(snapshot.data) && typeof snapshot.head_sha === "string";

/**
 * What may be kept of a conversations page: null where its reads don't date every part it shows,
 * or where it shows nothing read on this run, whose kept entry already says it all.
 */
export function savedThreadsOf(page: CachedThreads & { read_as?: string | null }) {
  const viewer = page.read_as;
  if (typeof viewer !== "string" || !viewer || !readsComplete(page) || !showsLive(page)) {
    return null;
  }
  const read_times = savedReadTimes(page);
  if (!read_times) return null;
  const saved = {
    kind: "threads" as const,
    viewer,
    page: {
      ...without(page as unknown as Record<string, unknown>, RUN_FIELDS),
      ...historyOf(page),
    },
    read_times,
  };
  const parsed = savedDetailsSchema.safeParse(saved);
  if (!parsed.success || JSON.stringify(parsed.data).length > MAX_SAVED_THREADS_CHARS) return null;
  return parsed.data;
}

/** A kept conversations page as its view's answer: every part kept, at its own saved time. */
export function restoredThreadsOf(
  data: Extract<SavedPullRequestDetails, { kind: "threads" }>,
): CachedThreads & { read_as: string } {
  const page = data.page as unknown as CachedThreads;
  const reads = restoredReads(page, data.read_times);
  return { ...page, ...(reads ? { reads } : {}), read_as: data.viewer };
}

/** What may be kept of a view's answer under `queryKey`, or null. */
function saveOf(snapshot: Restorable, queryKey: QueryKey): SavedPullRequestDetails | null {
  if (queryKey[1] === THREADS) {
    return isThreadsPage(snapshot) ? savedThreadsOf(snapshot as unknown as CachedThreads) : null;
  }
  // Nothing read since the restore: what's kept already says it all, and keeps its age.
  if (snapshot.restored) return null;
  const viewer = (snapshot.read_on_mac as { login?: unknown } | undefined)?.login;
  if (typeof viewer !== "string" || !viewer || !snapshot.pr) return null;
  const saved = { kind: "overview" as const, viewer, info: without(snapshot, SESSION_FIELDS) };
  const parsed = savedDetailsSchema.safeParse(saved);
  if (!parsed.success || JSON.stringify(parsed.data).length > MAX_SAVED_OVERVIEW_CHARS) return null;
  return parsed.data;
}

/** Whether a kept entry still belongs under its key: the kind, the PR and, for conversations, the revision. */
function entryHolds(queryKey: QueryKey, data: SavedPullRequestDetails): boolean {
  if (data.kind === "overview") {
    return queryKey[1] === OVERVIEW && data.info.pr.url === queryKey.at(-1);
  }
  const revision = revisionOf(queryKey.at(-1));
  return (
    queryKey[1] === THREADS &&
    revision !== null &&
    revision.base === data.page.base_sha &&
    revision.head === data.page.head_sha
  );
}

/** A kept entry as its view's answer, marked restored until GitHub answers. */
function restoreOf(data: SavedPullRequestDetails): Restorable {
  if (data.kind === "threads") return restoredThreadsOf(data) as unknown as Restorable;
  return { ...data.info, prs: [], tracking_available: false, restored: true };
}

/** When the oldest part of a conversations page was read; an overview is read whole. */
function readAtOf(snapshot: Restorable): number | undefined {
  return isThreadsPage(snapshot)
    ? oldestReadAt(snapshot as unknown as TimedThreadsPage)
    : undefined;
}

const detailsCache = createMacDurableReadCache<Restorable, SavedPullRequestDetails, QueryKey>({
  prefix: STORAGE_PREFIX,
  buster: SCHEMA,
  maxAgeMs: MAX_AGE_MS,
  maxEntryChars: MAX_SAVED_THREADS_CHARS + 1024,
  totalBudgetChars: MAX_SAVED_DETAILS_TOTAL_CHARS,
  maxEntries: MAX_SAVED_DETAILS_ENTRIES,
  dataSchema: savedDetailsSchema,
  keepOf: keptScope,
  readAtOf,
  entryHolds,
  durableKeyOf: pullRequestDetailsDurableKey,
  viewerOf: (data) => data.viewer,
  saveOf,
  restoreOf,
  // A kept PR paints at once; a stale one is read again behind it.
  refetchOnRestore: true,
  prunesOn: (prune) => {
    const stopNamed = subscribeNamedReads(prune);
    const stopSupport = subscribeDesktopReadPath(prune);
    const stopFlag = subscribeDesktopFeatureFlag(
      PULL_REQUEST_SAVED_DETAILS_FLAG,
      { fallback: true },
      ({ value }) => {
        savedDetailsOn = value !== false;
        prune();
      },
    );
    return () => {
      stopNamed();
      stopSupport();
      stopFlag();
    };
  },
});

/**
 * The persister for a view's overview or conversations read under `queryKey`, where kept; typed
 * as the view's own answer, which the cache keeps the shareable part of.
 */
export function pullRequestDetailsPersister<Answer, Key extends QueryKey = QueryKey>(
  queryKey: Key,
): QueryPersister<Answer, Key> | undefined {
  const durableKey = pullRequestDetailsDurableKey(queryKey);
  if (!durableKey || !keptScope(durableKey)) return undefined;
  return detailsCache.persister() as unknown as QueryPersister<Answer, Key> | undefined;
}

/**
 * Follow account and scope changes from now on, so a sign-out drops kept details though no PR
 * opened this run. Called once Mac reads start; repeat calls do nothing.
 */
export function watchPullRequestDetailsCache(): void {
  detailsCache.watch();
}

/** Forget the persister and stop its watchers, so a test starts clean. */
export function resetPullRequestDetailsCacheForTests(): void {
  detailsCache.resetForTests();
  savedDetailsOn = true;
}
