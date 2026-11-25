// The account's pending review on a pull request, read on this Mac as the account the page
// writes as, in that account's own key: another workspace user's, login's, sign-in's or
// scope's drafts never show, and a read that finished after any of them changed is dropped.

import { type QueryClient, replaceEqualDeep, useQuery } from "@tanstack/react-query";
import { useRef } from "react";

import { readAtObservedPriority, readPriorityMeta } from "@/hooks/pullRequestReadPriority";
import { useSecondaryReadPriority } from "@/hooks/secondaryReadPriority";
import type { CodeHostReadScheduling } from "@/lib/codeHosts";
import { readOnThisMac } from "@/lib/desktopPullRequests";
import {
  draftOwner,
  macPendingReviewOffers,
  macRouteCurrent,
  macWriteRoute,
  type MacWriteRoute,
  PENDING_REVIEW_READ,
  pullRequestIdentity,
  useMacWriteRouteVersion,
  useWorkspaceDraftOwner,
} from "@/lib/desktopPullRequestWrites";
import { macReadScopeKey } from "@/lib/macReadScope";
import { type MacNamedReader, namedReadScope } from "@/lib/macNamedReads";
import { readPullRequestRpc } from "@/lib/pullRequestRpc";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";

import type { ReviewThreadsPage } from "@/lib/pullRequestReviewThreads";

import {
  type PendingReviewRead,
  type PendingReviewState,
  parsePendingReviewAnswer,
  pendingReviewState,
  withPendingDrafts,
} from "./pendingReview";
import {
  HOST_PENDING_METHODS,
  type HostPendingRoute,
  hostPendingRoute,
  hostPendingScopeCurrent,
} from "./hostPendingRoute";

/**
 * `page` with the pending review's drafts in their threads, keeping the previous threads and
 * comments wherever their content is the same: the state is new on every render and its read on
 * every poll, but a file's diff draws its rows again only when its conversations really change.
 */
export function useThreadsWithPendingDrafts<Page extends ReviewThreadsPage>(
  page: Page | undefined,
  state: PendingReviewState,
): Page | undefined {
  const previous = useRef<Page | undefined>(undefined);
  const shown = replaceEqualDeep(previous.current, withPendingDrafts(page, state));
  previous.current = shown;
  return shown;
}

const PENDING_REVIEW_KEY = "github-pending-review";
/** How often a shown pending review is read again, so one started or finished elsewhere shows. */
export const PENDING_REVIEW_POLL_MS = 15_000;

const sameLogin = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();

/** Every pending review query of `prUrl`, whatever its scope. */
const pullRequestKey = (prUrl: string) => [PENDING_REVIEW_KEY, pullRequestIdentity(prUrl)];

/**
 * The query key of `route`'s pending review on `prUrl`: the PR, then the scope it's read in
 * (the host's account, or a named login's, each with its workspace user), then the login and
 * its sign-in, and the workspace user drafts belong to.
 */
export function pendingReviewKey(prUrl: string, route: MacWriteRoute, owner: string): unknown[] {
  const scope = route.reader
    ? namedReadScope(route.reader)
    : macReadScopeKey("github", route.target.host);
  return [
    ...pullRequestKey(prUrl),
    ...scope,
    route.actor.login.toLowerCase(),
    route.actor.generation,
    owner,
  ];
}

/** The query key of the pending review `route` reads through the session's host. */
export function hostPendingReviewKey(prUrl: string, route: HostPendingRoute, owner: string) {
  return [...pullRequestKey(prUrl), "host", route.conversationId, route.key, owner];
}

/** The key prefix of every pending-review read of `prUrl`, whatever its account or scope. */
export function pendingReviewQueryPrefix(prUrl: string): unknown[] {
  return pullRequestKey(prUrl);
}

/** Read every scope's pending review of `prUrl` again, e.g. after one of its drafts changed. */
export function invalidatePendingReview(queryClient: QueryClient, prUrl: string): void {
  void queryClient.invalidateQueries({ queryKey: pullRequestKey(prUrl) });
}

/** Thrown for a read answered after its account or scope moved on: it's never kept. */
class PendingReviewReadMoved extends Error {
  constructor() {
    super("The account changed while reading its pending review.");
    this.name = "PendingReviewReadMoved";
  }
}

/**
 * One read of `route`'s pending review, as that account in that scope; it throws for an answer
 * read as another login, one of another shape, or one that finished after the route moved.
 */
export async function readPendingReview(
  prUrl: string,
  route: MacWriteRoute,
  scheduling: CodeHostReadScheduling = {},
): Promise<PendingReviewRead> {
  const owner = draftOwner();
  const { data, viewer: login } = await readOnThisMac<unknown>(
    route.target,
    PENDING_REVIEW_READ,
    { repository: route.target.repository, number: route.target.number },
    scheduling,
  );
  if (!login || !sameLogin(login, route.actor.login)) throw new PendingReviewReadMoved();
  if (draftOwner() !== owner || !macRouteCurrent(prUrl, route)) throw new PendingReviewReadMoved();
  const read = parsePendingReviewAnswer(data);
  if (!read) throw new Error("This Mac answered with a pending review it couldn’t read.");
  return read;
}

/**
 * One read of the pending review through the session's host, as its account; it throws for an
 * answer of another shape, or one that finished after the route or workspace user moved.
 */
export async function readPendingReviewOnHost(route: HostPendingRoute): Promise<PendingReviewRead> {
  const owner = draftOwner();
  const data = await readPullRequestRpc<unknown>(
    route.conversationId,
    HOST_PENDING_METHODS.read,
    (client) => client.getPendingReview({ prUrl: route.prUrl }),
  );
  if (draftOwner() !== owner || !hostPendingScopeCurrent(route, route.prUrl)) {
    throw new PendingReviewReadMoved();
  }
  const read = parsePendingReviewAnswer(data);
  if (!read) throw new Error("The session’s host answered with a pending review it couldn’t read.");
  return read;
}

/** The pending review as the page would write now, and the route it reads along. */
export interface PendingReviewView {
  state: PendingReviewState;
  /** This Mac's route, where it offers pending reviews. */
  route: MacWriteRoute | null;
  /** The session's host, where this shell has no route and the host serves the read. */
  hostRoute: HostPendingRoute | null;
  refetch: () => void;
}

/**
 * The pending review on `prUrl` as the page would write now, for `readAs` (the page's own
 * login, or null for the host's account): `unavailable` wherever pending reviews aren't offered.
 * Where this shell has no route, the session `conversationId`'s host reads it instead, never on
 * a timer: on load, on focus, after a change and on Refresh. A failed read never stands in for
 * no review.
 */
export function usePendingReview(
  prUrl: string | null | undefined,
  readAs: MacNamedReader | null,
  enabled = true,
  conversationId = "",
): PendingReviewView {
  useMacWriteRouteVersion();
  const owner = useWorkspaceDraftOwner();
  const route = prUrl ? macWriteRoute(prUrl, readAs) : null;
  const offered = !!prUrl && !!route && macPendingReviewOffers(route);
  // Read for its re-render once the session's route is decided.
  const hostServes = usePullRequestMethodServed(conversationId, HOST_PENDING_METHODS.read);
  const hostRoute = !route && hostServes && prUrl ? hostPendingRoute(conversationId, prUrl) : null;
  const available = offered || !!hostRoute;
  const priority = useSecondaryReadPriority();
  const query = useQuery({
    queryKey:
      offered && route
        ? pendingReviewKey(prUrl, route, owner)
        : hostRoute && prUrl
          ? hostPendingReviewKey(prUrl, hostRoute, owner)
          : [PENDING_REVIEW_KEY, "off"],
    queryFn: async ({ client, queryKey }) => {
      const issuedAt = Date.now();
      const read = hostRoute
        ? await readPendingReviewOnHost(hostRoute)
        : await readAtObservedPriority(client, queryKey, (observed) =>
            readPendingReview(prUrl!, route!, { priority: observed }),
          );
      return { ...read, issued_at: issuedAt };
    },
    meta: readPriorityMeta(priority),
    enabled: enabled && available,
    retry: false,
    // The host's read is never polled; this Mac's is, so one started elsewhere shows.
    refetchInterval: hostRoute ? false : PENDING_REVIEW_POLL_MS,
    refetchIntervalInBackground: false,
    // In its place, it's read again when the window comes back.
    refetchOnWindowFocus: !!hostRoute,
  });
  const state = pendingReviewState({
    offered: enabled && available,
    data: available ? query.data : undefined,
    dataUpdatedAt: query.dataUpdatedAt,
    failed: query.isError,
    fetching: query.isFetching,
  });
  return {
    state,
    route: offered ? route : null,
    hostRoute,
    refetch: () => void query.refetch(),
  };
}
