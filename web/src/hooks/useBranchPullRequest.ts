// The session branch's pull request, found on this Mac when the session's
// repository context is known and the Mac reads its host. Otherwise the
// session's machine finds it, as before, and this hook stays out of the way.

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import type { PullRequestInfo } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  type BranchDiscovery,
  branchDiscoveryHost,
  discoverBranchPullRequest,
  withBranchPullRequest,
} from "@/lib/branchPullRequest";
import { type CodeHostContext, useCodeHostContext } from "@/lib/codeHostContext";
import {
  branchDiscoveryProvider,
  branchDiscoverySettled,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import { useMacReadScopes } from "@/hooks/useMacReadScopes";
import { macReadScopeKey } from "@/lib/macReadScope";
import { useIsMacPullRequestScope } from "./macPullRequestScope";

/** What the panel shows about the branch's PR beside the session's PRs. */
export type BranchDiscoveryNotice =
  | { status: "searching" }
  | { status: "ambiguous"; discovery: BranchDiscovery }
  | { status: "none-chosen"; discovery: BranchDiscovery }
  | { status: "incomplete"; discovery: BranchDiscovery }
  | { status: "failed"; error: unknown; provider: BranchDiscovery["provider"]; host: string };

/** This Mac finds the branch's PR: its answer so far, or why it can't. */
export interface BranchPullRequestState {
  provider: BranchDiscovery["provider"];
  host: string;
  discovery: BranchDiscovery | undefined;
  error: unknown;
  loading: boolean;
  refetch: () => void;
}

const CONTEXT_POLL_MS = 10_000;

/**
 * Find the session branch's PR on this Mac. Null while the session's machine
 * finds it: no context, a host the Mac doesn't read, or the user's choice.
 */
export function useBranchPullRequest(
  sessionId: string | undefined,
  { enabled: findBranch, poll }: { enabled: boolean; poll: boolean },
): BranchPullRequestState | null {
  useDesktopReadPathVersion();
  useMacReadScopes();
  // A session-free pull request view has no branch of its own to find a PR for.
  const macScope = useIsMacPullRequestScope(sessionId);
  const enabled = findBranch && !macScope;
  const context = useCodeHostContext(sessionId, {
    enabled,
    refetchInterval: poll ? CONTEXT_POLL_MS : false,
  }).data;
  const ok: Extract<CodeHostContext, { status: "ok" }> | null =
    context && context.status === "ok" ? context : null;
  const host = ok ? branchDiscoveryHost(ok) : null;
  const settled = branchDiscoverySettled(host);
  const provider = sessionId && settled ? branchDiscoveryProvider(sessionId, host) : null;
  const query = useQuery({
    queryKey: [
      "branch-pull-request",
      sessionId,
      ok?.context_key,
      ...(provider && provider !== "narnia" && host ? macReadScopeKey(provider, host) : []),
    ],
    queryFn: () => discoverBranchPullRequest(sessionId!, ok!, provider!),
    enabled: enabled && !!sessionId && !!ok && !!provider,
    retry: false,
    staleTime: 30_000,
    refetchInterval: poll ? CONTEXT_POLL_MS : false,
    refetchIntervalInBackground: false,
  });
  const active = enabled && !!ok && !!provider && !!host;
  // An answer for an older context, e.g. before a commit or a branch switch, is dropped.
  const discovery =
    query.data && ok && query.data.contextKey === ok.context_key ? query.data : undefined;
  const { error, isPending: loading, refetch } = query;
  return useMemo(
    () =>
      active
        ? {
            provider: provider!,
            host: host!,
            discovery,
            error,
            loading,
            refetch: () => void refetch(),
          }
        : null,
    [active, provider, host, discovery, error, loading, refetch],
  );
}

/** The panel's notice for `state`, or none once the branch's PR is found or there's none. */
export function branchDiscoveryNotice(
  state: BranchPullRequestState,
): BranchDiscoveryNotice | undefined {
  if (state.error) {
    return { status: "failed", error: state.error, provider: state.provider, host: state.host };
  }
  const discovery = state.discovery;
  if (!discovery) return state.loading ? { status: "searching" } : undefined;
  const { result } = discovery;
  if (result.status === "ambiguous") return { status: "ambiguous", discovery };
  if (result.status === "incomplete") return { status: "incomplete", discovery };
  if (result.status === "none" && result.chosen) return { status: "none-chosen", discovery };
  return undefined;
}

/**
 * `info` with what this Mac found for the branch. The PR the session's machine
 * inferred is never shown in its place. `keepSelection` keeps the PR the caller
 * asked for selected.
 */
export function withBranchDiscovery(
  info: PullRequestInfo,
  state: BranchPullRequestState,
  { keepSelection }: { keepSelection: boolean },
): PullRequestInfo & { branch_discovery?: BranchDiscoveryNotice } {
  const notice = branchDiscoveryNotice(state);
  const empty: BranchDiscovery = {
    contextKey: "",
    provider: state.provider,
    host: state.host,
    result: { status: "none", chosen: false },
    choiceKey: null,
    remembered: false,
  };
  const merged = withBranchPullRequest(info, state.discovery ?? empty);
  return {
    ...merged,
    branch_on_mac: true,
    ...(keepSelection ? { selected_pr_url: info.selected_pr_url, pr: info.pr } : {}),
    ...(notice ? { branch_discovery: notice } : {}),
  };
}
