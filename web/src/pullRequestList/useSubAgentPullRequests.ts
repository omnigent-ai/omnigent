// A session's pull requests joined with its sub-agents' linked PRs, read while the list is on
// screen. Sub-agent PR lists share the cache entries their own sessions' panels use.
//
// Freshness: a link a sub-agent makes shows within one poll (30 s) while the list is open, with no
// event for it. An archive or a delete is event-driven: its frame removes the sub-agent's rows at once.

import { type QueryClient, useQueries, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  type SessionDescendants,
  sessionDescendantsQueryKey,
  useSessionDescendants,
} from "@/hooks/useSessionDescendants";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  fetchSessionPullRequests,
  type StoredPullRequests,
  sessionPullRequestsKey,
} from "@/newPullRequestPage/hooks/useSessionPullRequests";
import {
  classifySubAgentReadError,
  recordSubAgentReadFailure,
  type SubAgentReadTrigger,
} from "@/lib/subAgentPullRequestDiagnostics";
import { childPrimaryLabel } from "@/shell/subagentLabel";
import {
  createConcurrencyLimit,
  mergeSubAgentPullRequests,
  type SessionPullRequestMerge,
  type SubAgentPullRequestLinks,
} from "./subAgentPullRequests";

/** How often a visible list re-reads the sub-agent tree and each sub-agent's PRs, so a link appears within this. */
export const SUB_AGENT_PULL_REQUESTS_POLL_MS = 30_000;
/** How many sub-agents' PR lists are read at once. */
const SUB_AGENT_READ_CONCURRENCY = 4;

/**
 * A sub-agent's stored PR list. A failed read is recorded with its trigger and reason, then left
 * out of the list; an aborted read is not a failure.
 */
async function readSubAgentPullRequests(
  queryClient: QueryClient,
  parentSessionId: string,
  childSessionId: string,
  signal: AbortSignal,
  origin: SubAgentReadOrigin,
) {
  const trigger: SubAgentReadTrigger =
    origin !== "list"
      ? origin
      : queryClient.getQueryData(sessionPullRequestsKey(childSessionId)) === undefined
        ? "initial"
        : "refresh";
  try {
    return await fetchSessionPullRequests(childSessionId, signal);
  } catch (error) {
    if (!signal.aborted) {
      recordSubAgentReadFailure({
        stage: "member_pull_requests",
        trigger,
        parentSessionId,
        childSessionId,
        ...classifySubAgentReadError(error),
      });
    }
    throw error;
  }
}

/**
 * The session's own PRs joined with the sub-agents' PRs already in the cache, read without fetching.
 * A sub-agent whose cached read failed is left out. This is what a PR opened from the list, in its
 * own place where the list isn't on screen, may be linked to through its sub-agent.
 *
 * @param queryClient - The app's query client holding the cached tree and sub-agent lists.
 * @param conversationId - The viewed session, e.g. `"conv_root"`.
 * @param own - The session's own stored PRs.
 */
export function cachedSubAgentPullRequests(
  queryClient: QueryClient,
  conversationId: string,
  own: readonly PullRequestAssociation[],
): SessionPullRequestMerge {
  const descendants = queryClient.getQueryData<SessionDescendants>(
    sessionDescendantsQueryKey(conversationId),
  );
  const members = (descendants?.rows ?? []).flatMap((row): SubAgentPullRequestLinks[] => {
    const key = sessionPullRequestsKey(row.info.id);
    if (queryClient.getQueryState(key)?.status === "error") return [];
    const stored = queryClient.getQueryData<StoredPullRequests>(key);
    if (!stored) return [];
    return [
      {
        session: { id: row.info.id, name: childPrimaryLabel(row.info) },
        depth: row.depth,
        prs: stored.prs,
      },
    ];
  });
  return mergeSubAgentPullRequests(own, members);
}

/** Who asked: the visible list, a PR opened in its own place without the tree known, or the composer's chip. */
export type SubAgentReadOrigin = "list" | "open" | "composer";

export interface SubAgentPullRequestsView {
  /** The own list joined with the sub-agents' PRs read so far. */
  merge: SessionPullRequestMerge;
  /** The tree had more sub-agents than the list reads; the first ones are shown. */
  truncated: boolean;
  /** The tree and every sub-agent's read have answered, or failed; `merge` is then final. */
  settled: boolean;
  /** The tree read failed, so the sub-agents' PRs are unknown. */
  treeFailed: boolean;
}

/**
 * The session's own PRs joined with its sub-agents', or undefined while the list is off screen or
 * `own` isn't known yet. A sub-agent whose latest read failed is left out, even if an earlier read
 * succeeded. Reads stop when the list goes away.
 *
 * @param conversationId - The viewed session, e.g. `"conv_root"`.
 * @param own - The session's own stored PRs, as the list shows them.
 * @param listVisible - False when the list isn't on screen or sub-agents are switched off.
 * @param origin - `open` when the reads answer a PR opened outside the list, for diagnostics.
 */
export function useSubAgentPullRequests(
  conversationId: string | undefined,
  own: readonly PullRequestAssociation[] | undefined,
  listVisible: boolean,
  origin: SubAgentReadOrigin = "list",
): SubAgentPullRequestsView | undefined {
  const active = listVisible && own !== undefined && !!conversationId;
  const queryClient = useQueryClient();
  const descendants = useSessionDescendants(active ? (conversationId ?? null) : null, {
    enabled: active,
    pollMs: SUB_AGENT_PULL_REQUESTS_POLL_MS,
    origin,
  });
  // One limiter for the list's whole life, so a re-render never starts a second batch.
  const [limit] = useState(() => createConcurrencyLimit(SUB_AGENT_READ_CONCURRENCY));
  const rows = descendants.data?.rows;
  // An empty query list when inactive unobserves every member, so its queued reads stop.
  // A member whose latest read failed contributes nothing, though its cached data stays.
  const member = useQueries({
    queries: active
      ? (rows ?? []).map((row) => ({
          queryKey: sessionPullRequestsKey(row.info.id),
          queryFn: ({ signal }: { signal: AbortSignal }) =>
            limit(
              () =>
                readSubAgentPullRequests(queryClient, conversationId!, row.info.id, signal, origin),
              signal,
            ),
          staleTime: SUB_AGENT_PULL_REQUESTS_POLL_MS,
          retry: false,
          refetchInterval: SUB_AGENT_PULL_REQUESTS_POLL_MS,
        }))
      : [],
    combine: (results) => ({
      stored: results.map((result) => (result.isError ? undefined : result.data)),
      // A read still waiting for its turn or its first answer keeps the view unsettled.
      settled: results.every((result) => !result.isPending),
    }),
  });
  const memberStored = member.stored;
  const members = useMemo<SubAgentPullRequestLinks[]>(() => {
    if (!rows) return [];
    return rows.flatMap((row, index) => {
      const stored = memberStored[index];
      if (!stored) return [];
      return [
        {
          session: { id: row.info.id, name: childPrimaryLabel(row.info) },
          depth: row.depth,
          prs: stored.prs,
        },
      ];
    });
  }, [rows, memberStored]);
  const merge = useMemo(
    () => (active ? mergeSubAgentPullRequests(own!, members) : undefined),
    [active, own, members],
  );
  if (!active || !merge) return undefined;
  return {
    merge,
    truncated: descendants.data?.truncated ?? false,
    settled: (descendants.isSuccess || descendants.isError) && member.settled,
    treeFailed: descendants.isError,
  };
}
