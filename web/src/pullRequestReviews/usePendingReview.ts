// The account's pending review on a pull request, read through the session's host in that
// route's own key: another workspace user's or route's drafts never show, and a read that
// finished after either changed is dropped.

import { type QueryClient, replaceEqualDeep, useQuery } from "@tanstack/react-query";
import { useRef } from "react";

import { readPriorityMeta } from "@/hooks/pullRequestReadPriority";
import { useSecondaryReadPriority } from "@/hooks/secondaryReadPriority";
import { pullRequestIdentity } from "@/lib/pullRequestStateHolds";
import { draftOwner, useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
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
  hostPendingScopeOf,
} from "./hostPendingRoute";
import { resolvePendingRecoveries } from "./pendingComposer";

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

/** Every pending review query of `prUrl`, whatever its scope. */
const pullRequestKey = (prUrl: string) => [PENDING_REVIEW_KEY, pullRequestIdentity(prUrl)];

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
  /** The session's host, where it serves the read. */
  hostRoute: HostPendingRoute | null;
  refetch: () => void;
}

/**
 * The pending review on `prUrl` through the session `conversationId`'s host: `unavailable`
 * where the host doesn't serve it. It's read on load, on focus, after a change and on Refresh,
 * never on a timer. A failed read never stands in for no review.
 */
export function usePendingReview(
  prUrl: string | null | undefined,
  enabled = true,
  conversationId = "",
): PendingReviewView {
  const owner = useWorkspaceDraftOwner();
  // Read for its re-render once the session's route is decided.
  const hostServes = usePullRequestMethodServed(conversationId, HOST_PENDING_METHODS.read);
  const hostRoute = hostServes && prUrl ? hostPendingRoute(conversationId, prUrl) : null;
  const available = !!hostRoute;
  const priority = useSecondaryReadPriority();
  const query = useQuery({
    queryKey:
      hostRoute && prUrl
        ? hostPendingReviewKey(prUrl, hostRoute, owner)
        : [PENDING_REVIEW_KEY, "off"],
    queryFn: async () => {
      const issuedAt = Date.now();
      const read = { ...(await readPendingReviewOnHost(hostRoute!)), issued_at: issuedAt };
      // A start that added nothing is let go once its review is no longer the one pending.
      resolvePendingRecoveries(
        owner,
        prUrl!,
        {
          login: hostRoute!.actor.login,
          host: hostPendingScopeOf(hostRoute!),
        },
        read,
      );
      return read;
    },
    meta: readPriorityMeta(priority),
    enabled: enabled && available,
    retry: false,
    refetchOnWindowFocus: true,
  });
  const state = pendingReviewState({
    offered: enabled && available,
    data: available ? query.data : undefined,
    dataUpdatedAt: query.dataUpdatedAt,
    failed: query.isError,
    fetching: query.isFetching,
  });
  return { state, hostRoute, refetch: () => void query.refetch() };
}
