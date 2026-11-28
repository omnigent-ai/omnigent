// The session branch's pull request, listed by the session's host where it serves
// ListBranchPullRequests. Elsewhere the session info's own inference stands and
// this hook stays out of the way.

import { useQuery } from "@tanstack/react-query";
import { useMemo, useSyncExternalStore } from "react";

import type { PullRequestInfo } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  type BranchDiscovery,
  branchChoiceVersion,
  parseBranchPullRequests,
  subscribeBranchChoices,
  withBranchChoice,
  withBranchPullRequest,
} from "@/lib/branchPullRequest";
import { readPullRequestRpc } from "@/lib/pullRequestRpc";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import { useIsSessionFreePullRequestScope } from "./sessionFreePullRequestScope";

/** What the panel shows about the branch's PR beside the session's PRs. */
export type BranchDiscoveryNotice =
  | { status: "searching" }
  | { status: "ambiguous"; discovery: BranchDiscovery }
  | { status: "none-chosen"; discovery: BranchDiscovery }
  | { status: "failed"; error: unknown };

/** The host's lookup for the branch's PR: its answer so far, or why it failed. */
export interface BranchPullRequestState {
  discovery: BranchDiscovery | undefined;
  error: unknown;
  loading: boolean;
}

const BRANCH_POLL_MS = 10_000;

/** The query key of a session's branch lookups, for asking again after a choice or retry. */
export const branchPullRequestQueryKey = (sessionId: string | undefined) => [
  "branch-pull-request",
  sessionId,
];

/**
 * Look up the PRs for `branch` of the session's workspace through its host. Null where
 * the host doesn't serve the lookup, there's no branch, or the caller doesn't ask.
 */
export function useBranchPullRequest(
  sessionId: string | undefined,
  branch: string | undefined,
  { enabled: findBranch, poll }: { enabled: boolean; poll: boolean },
): BranchPullRequestState | null {
  const served = usePullRequestMethodServed(sessionId ?? "", "listBranchPullRequests");
  // A session-free pull request view has no branch of its own to find a PR for.
  const sessionFree = useIsSessionFreePullRequestScope(sessionId);
  const active = findBranch && served && !sessionFree && !!sessionId && !!branch;
  const query = useQuery({
    queryKey: [...branchPullRequestQueryKey(sessionId), branch],
    queryFn: async () =>
      parseBranchPullRequests(
        await readPullRequestRpc<unknown>(sessionId!, "listBranchPullRequests", (client) =>
          client.listBranchPullRequests({ branch: branch! }),
        ),
      ),
    enabled: active,
    retry: false,
    staleTime: 30_000,
    refetchInterval: poll ? BRANCH_POLL_MS : false,
    refetchIntervalInBackground: false,
  });
  const { data, error, isPending: loading } = query;
  const choices = useSyncExternalStore(subscribeBranchChoices, branchChoiceVersion);
  return useMemo(() => {
    if (!active) return null;
    const choiceKey = { sessionId: sessionId!, branch: branch! };
    const discovery = data ? { choiceKey, result: withBranchChoice(data, choiceKey) } : undefined;
    return { discovery, error, loading };
    // `choices` re-applies the user's choice to the same answer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, sessionId, branch, data, error, loading, choices]);
}

/** The panel's notice for `state`, or none once the branch's PR is found or there's none. */
export function branchDiscoveryNotice(
  state: BranchPullRequestState,
): BranchDiscoveryNotice | undefined {
  if (state.error) return { status: "failed", error: state.error };
  const discovery = state.discovery;
  if (!discovery) return state.loading ? { status: "searching" } : undefined;
  const { result } = discovery;
  if (result.status === "ambiguous") return { status: "ambiguous", discovery };
  if (result.status === "none" && result.chosen) return { status: "none-chosen", discovery };
  return undefined;
}

/**
 * `info` with what the host's lookup found for the branch, in place of the PR the session
 * info inferred, once the lookup answers. `keepSelection` keeps the PR the caller asked for selected.
 */
export function withBranchDiscovery(
  info: PullRequestInfo,
  state: BranchPullRequestState,
  { keepSelection }: { keepSelection: boolean },
): PullRequestInfo & { branch_discovery?: BranchDiscoveryNotice } {
  const notice = branchDiscoveryNotice(state);
  const merged = state.discovery
    ? { ...withBranchPullRequest(info, state.discovery), branch_looked_up: true }
    : info;
  return {
    ...merged,
    ...(keepSelection ? { selected_pr_url: info.selected_pr_url, pr: info.pr } : {}),
    ...(notice ? { branch_discovery: notice } : {}),
  };
}
