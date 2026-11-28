import {
  type QueryClient,
  queryOptions,
  skipToken,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";

import { parseSessionPullRequestUrl } from "@/lib/sessionPullRequestUrl";
import { stackLandingTool } from "@/lib/pullRequestStacks/stackLanding";
import {
  fetchStackMembersOnHost,
  stackMemberLookups,
  type StackMembersAnswer,
  type StackMembersRead,
} from "@/lib/pullRequestStacks/stackMembers";
import type { StackParse } from "@/lib/pullRequestStacks/stackModel";
import { NATIVE_STACK_QUERY } from "@/lib/pullRequestStacks/nativeStackHost";
import {
  describedStackIdentity,
  type MemberRevision,
  memberInAnswer,
  sameMemberRevision,
} from "@/lib/pullRequestStacks/stackSnapshot";

import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import { usePullRequestRefreshOwner } from "@/pullRequestPage/pullRequestRefreshOwner";
import { onPullRequestRefreshRequested } from "@/pullRequestPage/refreshPullRequest";

import { dropStackSnapshots } from "./pullRequestStackSnapshots";

const STACK_MEMBERS_QUERY = "pull-request-stack-members";

/** How often an open stack's members are read again while the page is in view. */
export const STACK_MEMBERS_POLL_MS = 60_000;

/**
 * The members' cache key: the session whose host reads them, and the stack itself, so every
 * member of one description's stack shares one read.
 */
export function pullRequestStackMembersQueryKey(
  conversationId: string,
  identity: string,
  numbers: readonly number[],
): unknown[] {
  return [
    STACK_MEMBERS_QUERY,
    "host",
    conversationId,
    identity,
    ...[...numbers].sort((a, b) => a - b),
  ];
}

/** Read again while any member is open, since its review or merge can change any time. */
export function stackMembersPollInterval(
  answer: StackMembersAnswer | null | undefined,
): number | false {
  return answer?.members.some((member) => member.state === "OPEN") ? STACK_MEMBERS_POLL_MS : false;
}

/** Read every stack's members again, GitHub's own stacks too, as Refresh does. */
export async function refreshPullRequestStackMembers(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: [STACK_MEMBERS_QUERY] }),
    queryClient.invalidateQueries({ queryKey: [NATIVE_STACK_QUERY] }),
  ]);
  dropStackSnapshots(queryClient);
}

/** Every stack members answer cached now, a native stack's too, to tell a fresh one apart. */
export function cachedStackMembersAnswers(queryClient: QueryClient): Set<unknown> {
  const native = queryClient
    .getQueriesData<{ members: StackMembersAnswer | null }>({ queryKey: [NATIVE_STACK_QUERY] })
    .flatMap(([, data]) => (data?.members ? [data.members] : []));
  return new Set([
    ...queryClient.getQueriesData({ queryKey: [STACK_MEMBERS_QUERY] }).map(([, answer]) => answer),
    ...native,
  ]);
}

/**
 * The shared read of `parse`'s members around `prUrl`, through the session's host; `wanted` is
 * false where the host doesn't serve the stack or there's nothing to read.
 */
export function pullRequestStackMembersQueryOptions(
  conversationId: string,
  prUrl: string | null | undefined,
  parse: StackParse | null,
  hostServes: boolean,
) {
  const target = prUrl ? parseSessionPullRequestUrl(prUrl) : null;
  const numbers = parse ? stackMemberLookups(parse) : [];
  const readable = hostServes && !!conversationId && target?.provider === "github";
  // A stack of one has nothing to read beyond the PR itself, unless its tool's landing advice
  // needs the PR's own base: one lookup.
  const wanted =
    readable &&
    (numbers.length > 1 || (numbers.length === 1 && !!parse && stackLandingTool(parse) !== null));
  const identity = parse ? describedStackIdentity(parse) : "";
  return {
    wanted,
    options: queryOptions({
      queryKey: pullRequestStackMembersQueryKey(conversationId, identity, numbers),
      queryFn:
        wanted && target && prUrl
          ? () => fetchStackMembersOnHost(conversationId, prUrl, target.repository, numbers)
          : skipToken,
      staleTime: 30_000,
      retry: false,
    }),
  };
}

/**
 * Where the members of `prUrl`'s stack stand, read through the session's host in one read the
 * stack's members share. `revision` is the PR's own head and base as its details last said: a
 * read that recorded it otherwise, from before a push or retarget, is read again.
 */
export function usePullRequestStackMembers(
  conversationId: string,
  prUrl: string | null | undefined,
  parse: StackParse | null,
  revision: MemberRevision | null,
): StackMembersRead {
  const hostServes = usePullRequestMethodServed(conversationId, "getPullRequestStack");
  const { wanted, options } = useMemo(
    () => pullRequestStackMembersQueryOptions(conversationId, prUrl, parse, hostServes),
    [conversationId, prUrl, parse, hostServes],
  );
  const query = useQuery({
    ...options,
    refetchInterval: (current) => stackMembersPollInterval(current.state.data),
    refetchIntervalInBackground: false,
  });
  useReadAgainAfterChange(wanted ? options.queryKey : null, prUrl, revision, query.data);
  useRefreshMembersWithPullRequest(wanted ? options.queryKey : null, prUrl);
  if (!wanted) return { kind: "unavailable" };
  if (query.isPending) return { kind: "loading" };
  if (query.isError) return { kind: "failed" };
  return { kind: "loaded", answer: query.data };
}

/**
 * A refresh of the PR reads its stack's members again: this stack's shared read only, which
 * necessarily reads every member of it, and no other stack's.
 */
function useRefreshMembersWithPullRequest(
  queryKey: unknown[] | null,
  prUrl: string | null | undefined,
) {
  const queryClient = useQueryClient();
  const owner = usePullRequestRefreshOwner();
  const keyText = queryKey ? JSON.stringify(queryKey) : null;
  useEffect(() => {
    if (!keyText || !prUrl) return undefined;
    const key = JSON.parse(keyText) as unknown[];
    const where = { prUrl, kind: "members" as const, client: queryClient, owner };
    return onPullRequestRefreshRequested(where, async () => {
      await queryClient.invalidateQueries({ queryKey: key, exact: true }).catch(() => {});
      const state = queryClient.getQueryState(key);
      return state?.status === "error"
        ? { kind: "failed", error: (state.error as Error) ?? new Error("Couldn’t read the stack.") }
        : { kind: "ok" };
    });
  }, [keyText, prUrl, queryClient, owner]);
}

/**
 * Whether a shared members answer records `prUrl` at another head or base than its own details
 * give: read before that PR was pushed or retargeted, so it's read again before it's relied on.
 * A read that didn't cover the PR has nothing to disagree with.
 */
export function stackMembersBehind(
  answer: StackMembersAnswer | null | undefined,
  prUrl: string | null | undefined,
  revision: MemberRevision | null,
): boolean {
  if (!answer || !prUrl || !revision) return false;
  const recorded = memberInAnswer(answer, prUrl);
  if (!recorded) return false;
  // A read that doesn't name a base commit has none to disagree with.
  const compared = recorded.base_sha === null ? { ...revision, base_sha: null } : revision;
  return !sameMemberRevision(recorded, compared);
}

function useReadAgainAfterChange(
  queryKey: unknown[] | null,
  prUrl: string | null | undefined,
  revision: MemberRevision | null,
  answer: StackMembersAnswer | null | undefined,
) {
  const queryClient = useQueryClient();
  const asked = useRef<string | null>(null);
  const keyText = queryKey ? JSON.stringify(queryKey) : null;
  useEffect(() => {
    if (!keyText || !stackMembersBehind(answer, prUrl, revision)) return;
    // Only once per revision: GitHub may answer with the old head for a moment.
    const attempt = `${keyText}|${JSON.stringify(revision)}`;
    if (asked.current === attempt) return;
    asked.current = attempt;
    void queryClient.invalidateQueries({ queryKey: JSON.parse(keyText), exact: true });
  }, [queryClient, keyText, prUrl, revision, answer]);
}
