import { useCallback, useLayoutEffect, useRef } from "react";

import {
  type PullRequest,
  type PullRequestAssociation,
  type PullRequestInfo,
  usePullRequestInfo,
  usePullRequestInfos,
} from "@/hooks/usePullRequests";
import {
  storedListToShow,
  type StoredPullRequests,
  useSessionPullRequests,
} from "@/hooks/useSessionPullRequests";
import { matchWorktree } from "@/hooks/useHostWorktrees";
import { useSessionWorktrees } from "@/hooks/useSessionWorktrees";
import { gitProviderCopy } from "@/lib/gitProviders";
import {
  getPullRequestStatus,
  hasNewerPullRequestLifecycle,
  sortPullRequestsByStatus,
  type PullRequestStatus,
  type PullRequestStatusSummary,
} from "@/lib/pullRequestStatus";

export type ComposerPullRequest = PullRequestAssociation & PullRequestStatusSummary;

/** How much we actually know about the session's checked-out branch. */
export type ComposerBranchState =
  | "loading" // querying the host; no trustworthy answer yet
  | "branch" // a named branch is checked out (see `branch`)
  | "detached" // detached HEAD — no branch to name
  | "not-git" // explicit evidence the workspace is not a git checkout
  | "unknown"; // no host / offline / ambiguous — not a claim about the repo

/** Live git + PR status for the composer workspace bar. */
export interface ComposerGitStatus {
  /** Named checkout branch when `branchState === "branch"`, else `null`. */
  branch: string | null;
  branchState: ComposerBranchState;
  /** `true` = linked worktree, `false` = the repo's main worktree, `null` = unknown. */
  isWorktree: boolean | null;
  /** Matched worktree root path, or `null` when unresolved. */
  worktreePath: string | null;
  /**
   * Static branch recorded at session creation (`session.gitBranch`). Historical
   * metadata only — never substituted for the live branch, surfaced separately.
   */
  creationBranch: string | null;
  /** `owner/repo` from the git remote, or `null`. */
  repoNameWithOwner: string | null;
  /** GitHub association query state for fail-closed worktree and PR UI. */
  githubState: "loading" | "ready" | "unknown";
  /** Linked PRs ordered by action needed, with live status where available. */
  prs: ComposerPullRequest[];
  prStatus: PullRequestStatus | null;
  prCount: number;
  prNumber: number | null;
  /** Precedes `prNumber` in the PR's provider style: "#" for GitHub, "!" for Azure DevOps. */
  prNumberPrefix: string;
  /** Re-read live worktree + PR state from the host. */
  refresh: () => void;
  refreshing: boolean;
}

/**
 * Resolve the session's live checkout branch and worktree status.
 *
 * The branch comes from the host's `git worktree list` (via
 * {@link useSessionWorktrees}), matched to the session's workspace — the real
 * checked-out branch, distinct from a PR head. {@link usePullRequestInfo} supplies
 * only PR/repo metadata: its `branch` field can be a PR head ref, so it is not
 * trusted for the live branch. `not-git` is set only on explicit evidence;
 * offline / ambiguous / an empty list stay `unknown` rather than claiming the
 * workspace is not a repo.
 *
 * @param sessionId - Server session id (`null` on the pre-session window).
 * @param hostId - Host the session's workspace lives on.
 * @param workspace - Absolute workspace path.
 * @param creationBranch - Static `session.gitBranch`, surfaced as history only.
 */
/** Ready once live info or the server's list can answer; the server's list doesn't wait on the host. */
function githubStateOf(
  github: { data?: unknown; isError: boolean; isLoading: boolean },
  stored: StoredPullRequests | undefined,
): ComposerGitStatus["githubState"] {
  if (github.data && !github.isError) return "ready";
  if (stored && (stored.prs.length > 0 || stored.synced)) return "ready";
  return github.isLoading ? "loading" : "unknown";
}

function linkedPullRequests(
  info: PullRequestInfo | undefined,
  stored: StoredPullRequests | undefined,
): PullRequestAssociation[] {
  const fromStored = storedListToShow(stored, !!info);
  if (fromStored && (!info || info.tracking_available === true)) return fromStored;
  if (info?.prs) return info.prs;
  if (!info?.pr) return [];
  // Hosts that predate PR tracking expose only the branch's single PR.
  return [
    {
      url: info.pr.url,
      host: "",
      repository: info.repo?.name_with_owner ?? "",
      number: info.pr.number,
      title: info.pr.title,
      relationship: "inferred",
      provider: info.provider ?? undefined,
      provider_display: info.provider_display ?? undefined,
      state: info.pr.state,
      is_draft: info.pr.is_draft,
    },
  ];
}

interface PullRequestStatusSource {
  pr: PullRequest;
  updatedAt: number;
  live: boolean;
}

function enrichPullRequest(
  association: PullRequestAssociation,
  source: PullRequestStatusSource | undefined,
): ComposerPullRequest {
  if (!source) return association;
  if (hasNewerPullRequestLifecycle(association, source.pr, source.updatedAt)) {
    return association;
  }
  const { pr } = source;
  const terminal = ["MERGED", "CLOSED"].includes(pr.state?.toUpperCase());
  // An offline or failed refresh retains terminal lifecycle, not stale merge readiness.
  if (!source.live && !terminal) return association;
  const lifecycle = {
    ...association,
    title: pr.title,
    state: pr.state,
    is_draft: pr.is_draft,
  };
  if (!source.live) return lifecycle;
  return {
    ...lifecycle,
    checks: pr.checks,
    checks_supported: pr.checks_supported,
    merge_state_status: pr.merge_state_status,
  };
}

export function useComposerGitStatus({
  sessionId,
  hostId,
  workspace,
  creationBranch = null,
}: {
  sessionId: string | null | undefined;
  hostId: string | null | undefined;
  workspace: string | null | undefined;
  creationBranch?: string | null;
}): ComposerGitStatus {
  const worktrees = useSessionWorktrees(sessionId, hostId, workspace);
  const github = usePullRequestInfo(sessionId ?? undefined, { poll: "linked" });
  const stored = useSessionPullRequests(sessionId ?? undefined, github.data);
  const info = github.data;
  const result = worktrees.data;
  const associations = linkedPullRequests(info, stored.data);
  // The default query already reads its selected PR; merged PRs cannot become actionable again.
  const detailUrls = associations
    .filter((pr) => pr.url && pr.url !== info?.pr?.url && pr.state?.toUpperCase() !== "MERGED")
    .map((pr) => pr.url);
  const details = usePullRequestInfos(sessionId ?? undefined, detailUrls);
  const statusSources = new Map<string, PullRequestStatusSource>();
  const sources = [
    { query: github, url: info?.pr?.url },
    ...details.map((query, index) => ({ query, url: detailUrls[index] })),
  ];
  for (const { query, url } of sources) {
    const pr = query.data?.pr;
    if (pr && pr.url === url) {
      statusSources.set(pr.url, {
        pr,
        updatedAt: query.dataUpdatedAt,
        live: !query.isError && query.isEnabled !== false,
      });
    }
  }
  const prs = sortPullRequestsByStatus(
    associations.map((association) =>
      enrichPullRequest(association, statusSources.get(association.url)),
    ),
  );

  let branchState: ComposerBranchState;
  let branch: string | null = null;
  let isWorktree: boolean | null = null;
  let worktreePath: string | null = null;

  if (!hostId || !workspace) {
    branchState = "unknown";
  } else if (worktrees.isPlaceholderData || result === undefined) {
    // Placeholder = a stale prior-key result on a host/workspace switch; treat as
    // still loading rather than advertise the old workspace's branch as live.
    branchState = worktrees.isLoading || worktrees.isPlaceholderData ? "loading" : "unknown";
  } else if (result.status === "not_git") {
    branchState = "not-git";
  } else if (result.status === "unknown" || result.worktrees.length === 0) {
    // Ambiguous git failure, or an OK-but-empty list — neither proves non-git.
    branchState = "unknown";
  } else {
    const matched = matchWorktree(result.worktrees, workspace);
    if (matched === null) {
      branchState = "unknown";
    } else {
      isWorktree = !matched.is_main;
      worktreePath = matched.path;
      if (matched.detached || !matched.branch) {
        branchState = "detached";
      } else {
        branchState = "branch";
        branch = matched.branch;
      }
    }
  }

  const primaryPr = prs[0];
  const prNumber = primaryPr?.number ?? null;
  const prCount = prs.length;
  // The primary PR's own provider first, then the session's.
  const prNumberPrefix = gitProviderCopy(
    primaryPr?.provider ?? info?.provider,
    undefined,
    primaryPr?.provider_display ?? info?.provider_display,
  ).prNumberPrefix;

  const worktreeRefetch = worktrees.refetch;
  const githubRefetch = github.refetch;
  const detailQueriesRef = useRef(details);
  useLayoutEffect(() => {
    detailQueriesRef.current = details;
  }, [details]);
  const refresh = useCallback(() => {
    void worktreeRefetch();
    void githubRefetch();
    for (const detail of detailQueriesRef.current) void detail.refetch();
  }, [worktreeRefetch, githubRefetch]);

  return {
    branch,
    branchState,
    isWorktree,
    worktreePath,
    creationBranch: creationBranch?.trim() || null,
    repoNameWithOwner: info?.repo?.name_with_owner ?? null,
    githubState: githubStateOf(github, stored.data),
    prs,
    prStatus: primaryPr ? getPullRequestStatus(primaryPr) : null,
    prCount,
    prNumber,
    prNumberPrefix,
    refresh,
    refreshing:
      worktrees.isFetching || github.isFetching || details.some((detail) => detail.isFetching),
  };
}
