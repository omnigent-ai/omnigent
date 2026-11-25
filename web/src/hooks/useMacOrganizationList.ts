// One of the lists this Mac keeps for a PR's GitHub organization, such as its people or the
// people this account worked with there. The shell answers from memory and fills the list in
// the background, so this only re-reads the local answer, as often as `rereadMs` says.

import { skipToken, useQuery, type UseQueryResult } from "@tanstack/react-query";

import { useMacReadScopes } from "@/hooks/useMacReadScopes";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { readOnThisMac, type MacReadAnswer } from "@/lib/desktopPullRequests";
import {
  macReadTarget,
  readsHostOnThisMac,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import { macReadScopeKey } from "@/lib/macReadScope";

/** How a list says how far along it is; the shell's answers share these. */
export interface MacOrganizationListProgress {
  state: "off" | "filling" | "refreshing" | "ready" | "paused" | "unavailable";
  next_attempt_at: string | null;
}

const FILLING_REREAD_MS = 3_000;
const PAUSED_MIN_REREAD_MS = 30_000;
const READY_REREAD_MS = 5 * 60_000;

/** How soon to read the shell's answer again, from what it last said. */
export function organizationListRereadMs(
  answer: MacOrganizationListProgress | undefined,
  now = Date.now(),
) {
  if (!answer) return false;
  if (answer.state === "off") return false;
  if (answer.state === "filling" || answer.state === "refreshing") return FILLING_REREAD_MS;
  if (answer.state === "paused") {
    const at = Date.parse(answer.next_attempt_at ?? "");
    return Math.max(PAUSED_MIN_REREAD_MS, Number.isFinite(at) ? at - now : 0);
  }
  return READY_REREAD_MS;
}

export interface MacOrganizationList<Answer> {
  /** The shell's answer for this PR's organization, while it's offered here. */
  answer: Answer | undefined;
  /** The login the answer was read as, or null before one arrived. */
  login: string | null;
  query: UseQueryResult<MacReadAnswer<Answer>>;
  queryKey: readonly unknown[];
  /** Where a read goes: the PR's GitHub host and organization, when this Mac offers it. */
  target: { github: CodeHostPullRequest; organization: string } | null;
}

/**
 * `operation`'s answer for `prUrl`'s organization, read while `enabled`. Only where this Mac
 * reads the PR's GitHub host and its shell offers the operation; elsewhere, nothing.
 */
export function useMacOrganizationList<Answer extends MacOrganizationListProgress>(
  operation: string,
  prUrl: string,
  enabled: boolean,
): MacOrganizationList<Answer> {
  useDesktopReadPathVersion();
  useMacReadScopes();
  const pullRequest = macReadTarget(prUrl);
  const github = pullRequest?.provider === "github" ? pullRequest : null;
  const offered = !!github && readsHostOnThisMac("github", github.host, operation);
  const organization = github?.repository.split("/")[0] ?? null;
  const queryKey = [
    "code-host-mac",
    operation,
    github?.host,
    organization?.toLowerCase(),
    // A change of account drops what was read as the previous one.
    ...(offered && github ? macReadScopeKey("github", github.host) : []),
  ];
  const query = useQuery({
    queryKey,
    queryFn:
      offered && enabled && github && organization
        ? async () => readOnThisMac<Answer>(github, operation, { organization })
        : skipToken,
    // The answer comes from the shell's memory, so each open reads it again.
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: (current) => organizationListRereadMs(current.state.data?.data),
    retry: false,
  });
  const target = offered && github && organization ? { github, organization } : null;
  return {
    answer: target ? query.data?.data : undefined,
    login: query.data?.viewer ?? null,
    query,
    queryKey,
    target,
  };
}
