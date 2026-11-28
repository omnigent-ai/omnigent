// A session's linked PRs as the server stores them, which answer while the host is offline.

import {
  type QueryClient,
  type Mutation,
  useIsMutating,
  useMutation,
  useMutationState,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import type { ServerInfo } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { GitProviderDisplay } from "@/lib/gitProviders";
import { authenticatedFetch } from "@/lib/identity";
import { canonicalPullRequestUrl, pullRequestKey } from "@/lib/pullRequestReference";
import { sessionPullRequestMembership } from "@/lib/sessionPullRequestMembership";
import {
  beginPullRequestIntent,
  failPullRequestIntent,
  finishPullRequestIntentRequest,
  hasPullRequestIntents,
  pullRequestChangeIsUnclear,
  type PullRequestIntentEnd,
  pullRequestIntentEnd,
  settlePullRequestIntents,
} from "@/store/sessionPullRequestIntents";
import { relativeTime } from "@/lib/relativeTime";
import { isTempConvId } from "@/lib/tempConversationId";
import { useIsSessionFreePullRequestScope } from "@/hooks/sessionFreePullRequestScope";
import { announceChange } from "@/components/pendingChange/announceChange";
import {
  type BulkRunSnapshot,
  bulkRunSnapshot,
  startBulkRun,
  subscribeBulkRuns,
} from "@/pullRequestList/bulkRuns";
import type { RemovalProgress } from "@/pullRequestList/removalProgress";
import {
  type RemovalOutcome,
  removalOutcome,
  type RowRemoval,
} from "@/pullRequestList/subAgentRemoval";
import { type BulkOutcome, runBulk } from "@/lib/pullRequestBulkRunner";
import {
  errorFromResponse,
  fetchPullRequestInfo,
  updateSessionPr,
  type PullRequestAssociation,
  type PullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";

/** A session's first linked PR, as `SessionListItem.pull_request` carries it. */
export interface SessionPullRequestSummary {
  url: string;
  provider: string;
  provider_display?: GitProviderDisplay | null;
  repository: string;
  number: number;
  title?: string | null;
  /** "OPEN" | "MERGED" | "CLOSED" as last fetched; null before the host fetched it. */
  state?: string | null;
  is_draft?: boolean;
  /** Epoch seconds of the fetch that read `state`; null before one did. */
  state_checked_at?: number | null;
}

/** The session's linked PRs on the server, in display order. */
export interface StoredPullRequests {
  prs: PullRequestAssociation[];
  /**
   * Whether the server has imported the session's PRs from its host. Until it
   * has, an empty list means "not known yet" rather than "none".
   */
  synced: boolean;
}

interface SessionPullRequestList {
  object: "list";
  data: PullRequestAssociation[];
  last_id: string | null;
  has_more: boolean;
  synced?: boolean;
}

/** How long a stored PR state counts as current; the server refreshes older open PRs. */
export const PULL_REQUEST_FRESHNESS_MS = 5 * 60_000;

/**
 * How old a stored PR state is, e.g. `"2h ago"`, once it's past the freshness
 * window; null while it's current or before the host fetched it.
 *
 * @param checkedAt - The state's `state_checked_at`, in epoch seconds.
 */
export function stateAsOf(
  checkedAt: number | null | undefined,
  nowMs: number = Date.now(),
): string | null {
  if (!checkedAt) return null;
  const checkedMs = checkedAt * 1000;
  return nowMs - checkedMs >= PULL_REQUEST_FRESHNESS_MS
    ? `${relativeTime(checkedMs, nowMs)} ago`
    : null;
}

/** Whether the server stores session PRs; older servers have no such routes. */
export function storesSessionPullRequests(info: ServerInfo | "loading"): boolean {
  return info !== "loading" && info.session_pull_requests === true;
}

const SESSION_PULL_REQUESTS = "session-pull-requests";

export function sessionPullRequestsKey(conversationId: string | undefined) {
  return [SESSION_PULL_REQUESTS, conversationId] as const;
}

type LoadedSessionPullRequests = ReadonlyMap<string, readonly PullRequestAssociation[]>;

function readLoadedSessionPullRequests(
  queryClient: QueryClient,
  previous: LoadedSessionPullRequests,
): LoadedSessionPullRequests {
  const loaded = new Map<string, readonly PullRequestAssociation[]>();
  for (const [key, data] of queryClient.getQueriesData<StoredPullRequests>({
    queryKey: [SESSION_PULL_REQUESTS],
  })) {
    const conversationId = key[1];
    if (typeof conversationId === "string" && data) loaded.set(conversationId, data.prs);
  }
  const unchanged =
    loaded.size === previous.size &&
    [...loaded].every(([conversationId, prs]) => previous.get(conversationId) === prs);
  return unchanged ? previous : loaded;
}

/**
 * Every session's linked PRs that the app has already loaded, by session ID,
 * kept current as they load. Reads the cache only; it never fetches.
 */
export function useLoadedSessionPullRequests(enabled: boolean): LoadedSessionPullRequests {
  const queryClient = useQueryClient();
  const [loaded, setLoaded] = useState<LoadedSessionPullRequests>(() => new Map());
  useEffect(() => {
    if (!enabled) return undefined;
    const refresh = () =>
      setLoaded((previous) => readLoadedSessionPullRequests(queryClient, previous));
    refresh();
    return queryClient.getQueryCache().subscribe((event) => {
      if (event.query.queryKey[0] === SESSION_PULL_REQUESTS) refresh();
    });
  }, [enabled, queryClient]);
  return loaded;
}

function pullRequestsPath(conversationId: string, query: URLSearchParams): string {
  return `/v1/sessions/${encodeURIComponent(conversationId)}/pull-requests?${query}`;
}

/** Fetch all of the session's linked PRs from the server, in display order; `signal` stops it. */
export async function fetchSessionPullRequests(
  conversationId: string,
  signal?: AbortSignal,
): Promise<StoredPullRequests> {
  const prs: PullRequestAssociation[] = [];
  let synced: boolean | undefined;
  let after: string | null = null;
  // Each page needs the previous page's cursor; a session links at most 50 PRs.
  /* oxlint-disable no-await-in-loop */
  do {
    const query = new URLSearchParams({ limit: "100", ...(after ? { after } : {}) });
    const response = await authenticatedFetch(
      pullRequestsPath(conversationId, query),
      signal ? { signal } : undefined,
    );
    if (!response.ok) throw await errorFromResponse(response);
    const page = (await response.json()) as SessionPullRequestList;
    prs.push(...page.data);
    synced = page.synced === true;
    const next = page.has_more ? page.last_id : null;
    if (page.has_more && (!next || next === after)) {
      throw new Error("The server returned an invalid page of pull requests");
    }
    after = next;
  } while (after);
  /* oxlint-enable no-await-in-loop */
  return { prs, synced: synced === true };
}

/**
 * The session's linked PRs from the server, in display order. Never waits on the host.
 *
 * @param live - The caller's live PR info, if any. Its list is the server's as of
 *   that read, so a live read that finds a new PR refetches this one.
 * @param options.enabled - False skips the request, such as for a row the server lists no PR on.
 */
export function useSessionPullRequests(
  rawConversationId: string | undefined,
  live?: Pick<PullRequestInfo, "tracking_available" | "prs"> | null,
  { enabled = true }: { enabled?: boolean } = {},
) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  const stored = storesSessionPullRequests(useServerInfo());
  // A session-free pull request view has no session to list links for.
  const sessionFree = useIsSessionFreePullRequestScope(conversationId);
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: sessionPullRequestsKey(conversationId),
    queryFn: () => fetchSessionPullRequests(conversationId!),
    enabled: enabled && !!conversationId && stored && !sessionFree,
    staleTime: 30_000,
  });
  const liveUrls = live?.tracking_available ? live.prs?.map((pr) => pr.url).join("\n") : undefined;
  const storedUrls = query.data?.prs.map((pr) => pr.url).join("\n");
  useEffect(() => {
    if (query.isSuccess && liveUrls !== undefined && liveUrls !== storedUrls) {
      void queryClient.invalidateQueries({ queryKey: sessionPullRequestsKey(conversationId) });
    }
  }, [conversationId, liveUrls, storedUrls, query.isSuccess, queryClient]);
  return query;
}

/**
 * The PRs to show from the server's list: once it has imported the session's
 * PRs, or when there's no live payload to show instead.
 */
export function storedListToShow(
  stored: StoredPullRequests | undefined,
  hasLive: boolean,
): PullRequestAssociation[] | undefined {
  return stored && (stored.synced || !hasLive) ? stored.prs : undefined;
}

interface UnlinkResult {
  /** The PRs still linked, as the list's own read left them. */
  prs: PullRequestAssociation[];
}

/** What's left to say about an unlink once its intent ends, or null when it went. */
function unlinkFailureOf(end: PullRequestIntentEnd): string | null {
  if (end.status === "settled") return null;
  if (end.status === "unconfirmed") return end.reason;
  return UNLINK_OVERTAKEN_REASON;
}

export const UNLINK_OVERTAKEN_REASON = "It was linked again while it was being removed.";

/** The session's PRs as cached, without `removed`. */
function cachedPullRequestsWithout(
  queryClient: QueryClient,
  sessionId: string,
  removed: ReadonlySet<string>,
): PullRequestAssociation[] {
  return (
    queryClient.getQueryData<StoredPullRequests>(sessionPullRequestsKey(sessionId))?.prs ??
    queryClient.getQueryData<PullRequestInfo>(["pr-page-github-info", sessionId])?.prs ??
    []
  ).filter((pr) => !removed.has(pr.url));
}

const unlinkMutationKey = (conversationId: string) => ["unlink-session-pr", conversationId];
// Unlinks queue, a batch as one: each answers with the remaining list, so a stale overlapping
// response could bring a PR back. A run across a session tree changes several sessions' lists,
// so every session's unlinks share the one queue.
const UNLINK_SCOPE = { id: "unlink-session-pr" };
/** The sessions an unlink run changes, besides the one its key names. */
interface UnlinkMeta extends Record<string, unknown> {
  unlinkSessionIds: readonly string[];
}
/** At most this many of a batch's server unlinks are out at once. */
const UNLINK_CONCURRENCY = 3;

// Each running batch's bulk run, by its unlink's mutation, so the session's progress can count it.
const removalRunOfMutation = new Map<number, string>();
const removalRunListeners = new Set<() => void>();
function notifyRemovalRuns() {
  for (const listener of removalRunListeners) listener();
}

function subscribeRemovalRuns(listener: () => void): () => void {
  removalRunListeners.add(listener);
  const unsubscribe = subscribeBulkRuns(listener);
  return () => {
    removalRunListeners.delete(listener);
    unsubscribe();
  };
}

/** A running batch's run as it is now, the same object until it changes; null before or after. */
function removalRunSnapshot(mutationId: number | null): BulkRunSnapshot | null {
  const runId = mutationId === null ? undefined : removalRunOfMutation.get(mutationId);
  return runId === undefined ? null : bulkRunSnapshot(runId);
}

/**
 * Read the session's PR list again, as a read sent now: after a change answered, or when it
 * disagreed with one. The list's own reads are the only thing that writes it.
 *
 * @param stored - Whether the server stores the list; an older one's live info has the links.
 */
export async function readSessionPullRequestsAgain(
  queryClient: QueryClient,
  conversationId: string,
  stored: boolean,
): Promise<void> {
  const key = sessionPullRequestsKey(conversationId);
  if (!stored) {
    const infoKey = ["pr-page-github-info", conversationId];
    await queryClient.cancelQueries({ queryKey: infoKey, exact: true });
    await queryClient
      .fetchQuery({
        queryKey: infoKey,
        queryFn: () => fetchPullRequestInfo(conversationId),
        staleTime: 0,
      })
      .catch(() => {});
    return;
  }
  // A read already out may have started before the change; this one replaces it.
  await queryClient.cancelQueries({ queryKey: key, exact: true });
  await queryClient
    .fetchQuery({
      queryKey: key,
      queryFn: ({ signal }) => fetchSessionPullRequests(conversationId, signal),
      staleTime: 0,
    })
    .catch(() => {});
}

const followedClients = new WeakSet<QueryClient>();

/**
 * Settle each session's pending links and unlinks against every read of its list, from
 * whichever view made it. Installed once per client; a list nobody reads settles nothing.
 */
export function followPullRequestIntents(queryClient: QueryClient): void {
  if (followedClients.has(queryClient)) return;
  followedClients.add(queryClient);
  queryClient.getQueryCache().subscribe((event) => {
    if (event.type !== "updated") return;
    const { action } = event;
    const failed = action.type === "error";
    if (!failed && (action.type !== "success" || action.manual)) return;
    // A query the cache dropped since its read began decides nothing.
    if (queryClient.getQueryCache().get(event.query.queryHash) !== event.query) return;
    const [root, sessionId, ...rest] = event.query.queryKey;
    if (typeof sessionId !== "string" || rest.length > 0 || !hasPullRequestIntents(sessionId)) {
      return;
    }
    const stored = queryClient.getQueryData<StoredPullRequests>(sessionPullRequestsKey(sessionId));
    // The live info decides only on a server with no list of its own.
    if (root === "pr-page-github-info" ? stored !== undefined : root !== SESSION_PULL_REQUESTS)
      return;
    const live = queryClient.getQueryData<PullRequestInfo>(["pr-page-github-info", sessionId])?.prs;
    const linked = failed ? null : sessionPullRequestMembership({ stored, live }).keys;
    if (settlePullRequestIntents(sessionId, linked)) {
      void readSessionPullRequestsAgain(queryClient, sessionId, root === SESSION_PULL_REQUESTS);
    }
  });
}

/** One link of a batch: from this Mac's store, or on the server; its reply writes nothing. */
async function removeLink(
  serverInfo: ReturnType<typeof useServerInfo>,
  conversationId: string,
  url: string,
): Promise<void> {
  if (serverInfo === "loading") throw new Error("Still connecting to the server; try again.");
  await requestUnlink(serverInfo, conversationId, url);
}

/** How long a removal waits on the list's read before it ends anyway; the read still lands. */
const REMOVAL_READ_WAIT_MS = 10_000;

/** Read the session's list again, waiting at most {@link REMOVAL_READ_WAIT_MS} for it. */
async function readAfterRemoval(
  queryClient: QueryClient,
  conversationId: string,
  stored: boolean,
): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([
    readSessionPullRequestsAgain(queryClient, conversationId, stored),
    new Promise<void>((resolve) => {
      timer = setTimeout(resolve, REMOVAL_READ_WAIT_MS);
    }),
  ]);
  clearTimeout(timer);
}

/**
 * Send one server unlink, which works while the host is offline. It
 * shows unlinked at once and its reply writes nothing. A definite refusal reverts it and throws;
 * otherwise it resolves with its intent's end, which a read of the list the caller starts (or
 * the intent's lifetime) settles. A reply that never comes doesn't hold it past that end.
 */
async function sendUnlink(
  queryClient: QueryClient,
  serverInfo: ReturnType<typeof useServerInfo>,
  conversationId: string,
  url: string,
): Promise<{ end: Promise<PullRequestIntentEnd> }> {
  if (serverInfo === "loading") throw new Error("Still connecting to the server; try again.");
  followPullRequestIntents(queryClient);
  const intent = beginPullRequestIntent(conversationId, url, "unlink");
  const end = pullRequestIntentEnd(conversationId, intent);
  const answer = await Promise.race([
    requestUnlink(serverInfo, conversationId, url).then(
      () => ({ error: null as unknown }),
      (error: unknown) => ({ error: error ?? new Error("The unlink failed.") }),
    ),
    end.then(() => null),
  ]);
  // Replaced by a newer change, or out of time, while the request was out.
  if (answer === null) return { end };
  if (
    answer.error !== null &&
    (!storesSessionPullRequests(serverInfo) || !pullRequestChangeIsUnclear(answer.error))
  ) {
    failPullRequestIntent(conversationId, intent);
    throw answer.error;
  }
  // Answered, or unclear and maybe gone through: the server's list decides either way.
  finishPullRequestIntentRequest(conversationId, intent);
  // Wrapped, so awaiting the send doesn't also wait for the end.
  return { end };
}

async function requestUnlink(
  serverInfo: ServerInfo,
  conversationId: string,
  url: string,
): Promise<void> {
  // @deprecated server-pr-tracking compat, remove in 0.24.0 (~2026-11-24): servers
  // before 0.20.0 lack this route, so unlink through the runner or host instead.
  if (!storesSessionPullRequests(serverInfo)) {
    await updateSessionPr(conversationId, { url, action: "remove" });
    return;
  }
  const response = await authenticatedFetch(
    pullRequestsPath(conversationId, new URLSearchParams({ url })),
    { method: "DELETE" },
  );
  if (!response.ok) throw await errorFromResponse(response);
}

/** Refetch what the unlinks changed, once, keeping the removed PRs' own queries put. */
function refetchAfterUnlink(
  queryClient: QueryClient,
  conversationId: string,
  removed: ReadonlySet<string>,
) {
  void queryClient.invalidateQueries({
    queryKey: ["pr-page-github-info", conversationId],
    predicate: (query) => !removed.has(String(query.queryKey[2])),
  });
  // Sidebar and Canvas read the session's first PR from the session lists.
  void queryClient.invalidateQueries({ queryKey: ["conversations"] });
  void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
}

const plural = (count: number) => `${count} pull request${count === 1 ? "" : "s"}`;

/**
 * Unlink `urls` from the session as one run that outlives the view that started it; see
 * {@link startTreeUnlinkRun}. Resolves with the PRs still linked, as cached.
 */
export function startSessionUnlinkRun(
  queryClient: QueryClient,
  serverInfo: ReturnType<typeof useServerInfo>,
  conversationId: string,
  urls: readonly string[],
): Promise<PullRequestAssociation[]> {
  return startTreeUnlinkRun(
    queryClient,
    serverInfo,
    conversationId,
    urls.map((url) => ({
      url,
      targets: [{ sessionId: conversationId, url }],
      fromSubAgents: [],
      kept: [],
      ownershipPending: false,
    })),
  );
}

/**
 * The toast a run across a session tree ends with, counting PRs, not links: how many went, which
 * were only partly removed and who still links them, which none of whose links went and why,
 * and how many Stop left unsent. Anything short of every PR removed asks for a look.
 */
export function unlinkRunSummary(
  rows: readonly RowRemoval[],
  outcomes: ReadonlyMap<RowRemoval, RemovalOutcome>,
  stillLinked: ReadonlyMap<RowRemoval, readonly string[]>,
  errors: ReadonlyMap<RowRemoval, readonly string[]>,
): { title: string; detail: string | null; needsLook: boolean } {
  const withOutcome = (outcome: RemovalOutcome) =>
    rows.filter((row) => outcomes.get(row) === outcome);
  const removed = withOutcome("removed").length;
  const partial = withOutcome("partial");
  const notRemoved = withOutcome("not_removed");
  const notSent = rows.length - outcomes.size;
  const partialNames = [...new Set(partial.flatMap((row) => stillLinked.get(row) ?? []))];
  const reason = (failed: readonly RowRemoval[]) =>
    failed.flatMap((row) => errors.get(row) ?? [])[0];
  const notRemovedReason = reason(notRemoved);
  const partialReason = reason(partial);
  const detail = [
    partial.length === 0
      ? null
      : removed > 0 || notRemoved.length > 0
        ? `${partial.length} partly removed: still linked by ${partialNames.join(", ")}.`
        : `Still linked by ${partialNames.join(", ")}.`,
    partialReason && !notRemovedReason ? `A link couldn’t be removed: ${partialReason}` : null,
    notRemoved.length > 0
      ? `${plural(notRemoved.length)} couldn’t be removed${notRemovedReason ? `: ${notRemovedReason}` : "."}`
      : null,
    notSent > 0 ? `Stopped before ${notSent} more.` : null,
  ]
    .filter(Boolean)
    .join(" ");
  return {
    title:
      removed > 0
        ? `Removed ${plural(removed)}`
        : partial.length > 0
          ? `Partly removed ${plural(partial.length)}`
          : "No pull requests were removed",
    detail: detail || null,
    needsLook: removed < rows.length,
  };
}

/** The URL `sessionId` stores for `url`'s PR, which an unlink must name exactly. */
function linkUrlIn(queryClient: QueryClient, sessionId: string, url: string): string {
  const key = canonicalPullRequestUrl(url);
  const listed =
    queryClient.getQueryData<StoredPullRequests>(sessionPullRequestsKey(sessionId))?.prs ??
    queryClient.getQueryData<PullRequestInfo>(["pr-page-github-info", sessionId])?.prs ??
    [];
  return listed.find((pr) => canonicalPullRequestUrl(pr.url) === key)?.url ?? url;
}

/**
 * Unlink each row's PR from the sessions its removal names, the viewed session's and its owned
 * sub-agents', as one run that outlives the view that started it: rows with a link on this Mac
 * one at a time, then the rest a few at once, each row's links in turn. Its toast counts PRs,
 * offers Stop, and says which were only partly removed. Resolves with the viewed session's PRs
 * still linked, as cached.
 */
export function startTreeUnlinkRun(
  queryClient: QueryClient,
  serverInfo: ReturnType<typeof useServerInfo>,
  viewedSessionId: string,
  removals: readonly RowRemoval[],
): Promise<PullRequestAssociation[]> {
  const mutation = queryClient.getMutationCache().build(queryClient, {
    mutationKey: unlinkMutationKey(viewedSessionId),
    scope: UNLINK_SCOPE,
    // Each target session reports the run as its own pending removal.
    meta: {
      unlinkSessionIds: [
        ...new Set(removals.flatMap((row) => row.targets.map((target) => target.sessionId))),
      ],
    } satisfies UnlinkMeta,
    mutationFn: async (rows: readonly RowRemoval[]) => {
      const bulkRun = startBulkRun({
        label: `Removing ${plural(rows.length)}…`,
        total: rows.length,
      });
      removalRunOfMutation.set(mutation.mutationId, bulkRun.id);
      notifyRemovalRuns();
      // Said from the run itself, so it's heard however the view that started it changes.
      announceChange(`Removing ${plural(rows.length)}…`);
      const sessionIds = [
        ...new Set(rows.flatMap((row) => row.targets.map((target) => target.sessionId))),
      ];
      let summary: ReturnType<typeof unlinkRunSummary> | null = null;
      try {
        // A read that started before the unlinks would bring their PRs back.
        await Promise.all(
          sessionIds.flatMap((id) => [
            queryClient.cancelQueries({ queryKey: sessionPullRequestsKey(id) }),
            queryClient.cancelQueries({ queryKey: ["pr-page-github-info", id] }),
          ]),
        );
        const removedBySession = new Map<string, Set<string>>();
        const outcomes = new Map<RowRemoval, RemovalOutcome>();
        const stillLinked = new Map<RowRemoval, string[]>();
        // Each refused link's reason, by the PR it belongs to.
        const errors = new Map<RowRemoval, string[]>();
        let ended = 0;
        const stillCurrent = () => !bulkRun.stopRequested();
        const nameOf = (row: RowRemoval, sessionId: string) =>
          row.fromSubAgents.find((session) => session.id === sessionId)?.name ?? "this session";
        const step = async (row: RowRemoval): Promise<BulkOutcome> => {
          const done = new Set<string>();
          try {
            for (const [index, target] of row.targets.entries()) {
              // Stop between a PR's links, whether the last one went or not, sends no more of them.
              if (index > 0 && !stillCurrent()) break;
              const linkUrl = linkUrlIn(queryClient, target.sessionId, target.url);
              try {
                // A batch runs as on main, with no intents: an answered unlink counts as
                // removed, and the list's read after the run shows what's left.
                // oxlint-disable-next-line no-await-in-loop -- a PR's links go one at a time
                await removeLink(serverInfo, target.sessionId, linkUrl);
                done.add(target.sessionId);
                const removed = removedBySession.get(target.sessionId) ?? new Set<string>();
                removed.add(linkUrl);
                removedBySession.set(target.sessionId, removed);
              } catch (error) {
                const reasons = errors.get(row) ?? [];
                reasons.push(error instanceof Error ? error.message : String(error));
                errors.set(row, reasons);
              }
            }
          } finally {
            const outcome = removalOutcome(row, done);
            outcomes.set(row, outcome);
            stillLinked.set(row, [
              ...row.targets
                .filter((target) => !done.has(target.sessionId))
                .map((target) => nameOf(row, target.sessionId)),
              ...row.kept.map((session) => session.name),
            ]);
            ended += 1;
            bulkRun.progress({
              done: ended,
              failed: [...outcomes.values()].filter((value) => value !== "removed").length,
            });
          }
          return outcomes.get(row) === "not_removed" ? "refused" : "applied";
        };
        // The runner steps through keys; each row's is its place in the run.
        const keys = rows.map((_, index) => String(index));
        const stepAt = (key: string) => step(rows[Number(key)]!);
        await runBulk(keys, stepAt, { limit: UNLINK_CONCURRENCY, stillCurrent });
        // Each answer's remaining list is left unread: one read per session settles them all,
        // and the removed rows stay marked until it lands, or until it's taken too long.
        await Promise.all(
          [...removedBySession].map(([sessionId, removed]) => {
            refetchAfterUnlink(queryClient, sessionId, removed);
            return readAfterRemoval(queryClient, sessionId, storesSessionPullRequests(serverInfo));
          }),
        );
        summary = unlinkRunSummary(rows, outcomes, stillLinked, errors);
        bulkRun.finish(summary);
        return cachedPullRequestsWithout(
          queryClient,
          viewedSessionId,
          removedBySession.get(viewedSessionId) ?? new Set(),
        );
      } finally {
        // A run that broke off still ends, so nothing waits on it and its toast says so.
        if (!summary) {
          summary = {
            title: "Couldn’t finish removing the pull requests",
            detail: "Some may still be linked.",
            needsLook: true,
          };
          bulkRun.finish(summary);
          for (const id of sessionIds) {
            void queryClient.invalidateQueries({ queryKey: sessionPullRequestsKey(id) });
          }
        }
        announceChange(
          summary.detail ? `${summary.title}. ${summary.detail}` : `${summary.title}.`,
        );
      }
    },
    onSettled: () => {
      removalRunOfMutation.delete(mutation.mutationId);
    },
  });
  return mutation.execute(removals);
}

/** Start a run removing rows across the viewed session's tree; see {@link startTreeUnlinkRun}. */
export function useUnlinkSessionTree(viewedSessionId: string) {
  const queryClient = useQueryClient();
  const serverInfo = useServerInfo();
  return (removals: readonly RowRemoval[]) =>
    startTreeUnlinkRun(queryClient, serverInfo, viewedSessionId, removals);
}

/** Start a run unlinking several of the session's PRs; see {@link startSessionUnlinkRun}. */
export function useUnlinkSessionPrs(conversationId: string) {
  const queryClient = useQueryClient();
  const serverInfo = useServerInfo();
  return (urls: readonly string[]) =>
    startSessionUnlinkRun(queryClient, serverInfo, conversationId, urls);
}

/** Whether `mutation` is an unlink for the session, including a tree run changing its links. */
function unlinksFor(mutation: Mutation<unknown, Error, unknown>, conversationId: string): boolean {
  const [kind, sessionId] = mutation.options.mutationKey ?? [];
  if (kind !== "unlink-session-pr") return false;
  const meta = mutation.options.meta as Partial<UnlinkMeta> | undefined;
  return sessionId === conversationId || !!meta?.unlinkSessionIds?.includes(conversationId);
}

/**
 * Whether an unlink, one or a batch, is out or queued for the session, including a run
 * started from an ancestor's list that removes the session's links.
 */
/** The same, read once outside React, as a link's completion does before it writes. */
export function sessionUnlinkRunning(queryClient: QueryClient, conversationId: string): boolean {
  return (
    queryClient.isMutating({ predicate: (mutation) => unlinksFor(mutation, conversationId) }) > 0
  );
}

export function useSessionUnlinkPending(conversationId: string): boolean {
  return useIsMutating({ predicate: (mutation) => unlinksFor(mutation, conversationId) }) > 0;
}

/** A pending unlink as the session's progress reads it. */
interface PendingUnlink {
  id: number;
  paused: boolean;
  /** The session its key names, which started it. */
  sessionId: unknown;
  /** One URL for a single unlink, a batch's rows otherwise. */
  variables: unknown;
}

/** The PRs `unlink` removes from `conversationId`, by key, and how many PRs it removes in all. */
function unlinkScope(unlink: PendingUnlink, conversationId: string) {
  if (typeof unlink.variables === "string") {
    return { urls: [pullRequestKey(unlink.variables)], total: 1, batch: false };
  }
  const rows = (unlink.variables ?? []) as readonly RowRemoval[];
  const own = unlink.sessionId === conversationId;
  return {
    urls: rows
      .filter((row) => own || row.targets.some((target) => target.sessionId === conversationId))
      .map((row) => pullRequestKey(row.url)),
    total: rows.length,
    batch: true,
  };
}

/**
 * The removal the session waits on, or null once none is pending: the oldest one running, or
 * else the oldest one queued behind another. Only a pending unlink keeps it, whatever its run's
 * toast still shows; a run that ended or stopped clears it as its unlink settles.
 */
export function useSessionRemovalProgress(conversationId: string): RemovalProgress | null {
  const pending = useMutationState({
    filters: {
      status: "pending",
      predicate: (mutation) => unlinksFor(mutation, conversationId),
    },
    select: (mutation): PendingUnlink => ({
      id: mutation.mutationId,
      paused: mutation.state.isPaused,
      sessionId: mutation.options.mutationKey?.[1],
      variables: mutation.state.variables,
    }),
  });
  const current = pending.find((unlink) => !unlink.paused) ?? pending[0] ?? null;
  const run = useSyncExternalStore(
    subscribeRemovalRuns,
    () => removalRunSnapshot(current?.id ?? null),
    () => null,
  );
  const started = current !== null && removalRunOfMutation.has(current.id);
  return useMemo(() => {
    if (!current) return null;
    const { urls, total, batch } = unlinkScope(current, conversationId);
    if (current.paused) {
      return {
        phase: "waiting",
        urls: new Set(urls),
        done: null,
        total,
        stop: "none",
        runId: null,
      };
    }
    // A batch whose run ended is only settling; it counts as done until it has.
    const done = run ? run.done : started ? total : 0;
    const stop = !batch || !run ? "none" : run.stopRequested ? "requested" : "available";
    const runId = stop === "available" && run ? run.id : null;
    return { phase: "removing", urls: new Set(urls), done, total, stop, runId };
  }, [conversationId, current, run, started]);
}

/** Unlink one PR on the server, which works while the host is offline. Returns the remaining PRs. */
export function useUnlinkSessionPr(conversationId: string) {
  const queryClient = useQueryClient();
  const serverInfo = useServerInfo();
  return useMutation({
    // The panel outlives a session switch; a per-session key leaves a running
    // unlink its own session's callbacks and clears its state from the next one.
    mutationKey: unlinkMutationKey(conversationId),
    scope: UNLINK_SCOPE,
    mutationFn: async (url: string): Promise<UnlinkResult> => {
      // Said from the unlink itself, so it's heard however the view that started it changes.
      announceChange("Removing the pull request…");
      try {
        if (serverInfo === "loading") throw new Error("Still connecting to the server; try again.");
        const { end } = await sendUnlink(queryClient, serverInfo, conversationId, url);
        // The row stays marked until the list's own read drops it, and only how the intent
        // ends (the read, or its lifetime) says whether it went; a read that hangs can't hold it.
        refetchAfterUnlink(queryClient, conversationId, new Set([url]));
        void readSessionPullRequestsAgain(
          queryClient,
          conversationId,
          storesSessionPullRequests(serverInfo),
        );
        // Not removed, and why: the list still links it, couldn't be read, or a newer link of
        // the PR took over (so the panel keeps it selected).
        const failure = unlinkFailureOf(await end);
        if (failure) throw new Error(failure);
        announceChange("Removed the pull request.");
        return { prs: cachedPullRequestsWithout(queryClient, conversationId, new Set()) };
      } catch (error) {
        announceChange(
          `Couldn’t remove the pull request: ${error instanceof Error ? error.message : String(error)}`,
        );
        throw error;
      }
    },
  });
}
