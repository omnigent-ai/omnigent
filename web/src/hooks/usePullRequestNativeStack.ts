import { queryOptions, skipToken, useQuery } from "@tanstack/react-query";

import { readAtObservedPriority, readPriorityMeta } from "@/hooks/pullRequestReadPriority";
import { useSecondaryReadPriority } from "@/hooks/secondaryReadPriority";
import { STACK_MEMBERS_POLL_MS } from "@/hooks/usePullRequestStackMembers";
import { useDesktopPullRequestReads } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  fetchNativeStack,
  NATIVE_STACK_QUERY,
  nativeStackMembers,
  nativeStackOffered,
  type NativeStackReadAnswer,
} from "@/lib/pullRequestStacks/nativeStackRead";
import type { StackMembersAnswer } from "@/lib/pullRequestStacks/stackMembers";
import { stackPullRequestRef } from "@/lib/pullRequestStacks/stackModel";

/**
 * The native stack's cache key: the PR's read scope whole, so another reader never sees this
 * answer, and the revision the page read the PR at, so a push or retarget reads it again and
 * an answer still in flight for the one before is never shown for it.
 */
export function pullRequestNativeStackQueryKey(
  prUrl: string | null | undefined,
  scope: readonly unknown[],
  revision: string | null,
): unknown[] {
  return [NATIVE_STACK_QUERY, prUrl, ...scope, revision];
}

/** GitHub's stack and its members, as one read gave both. */
export interface NativeStackData {
  /** Undefined when GitHub or the shell couldn't say. */
  read: NativeStackReadAnswer | undefined;
  /** The members of the stack's kept entries, as the account it was read as. */
  members: StackMembersAnswer | null;
}

/** Where GitHub's own stack for a PR stands, as this page's reader sees it. */
export type NativeStackState =
  /** Not read here: flag off, no PR, a PR read elsewhere, or a shell without the read. */
  | { kind: "off" }
  | { kind: "loading" }
  /**
   * Read, or failed, which can't say either: the description decides then. `readAt` is when
   * that answer came, so what it says can be ordered against other members' reads.
   */
  | ({ kind: "settled"; readAt: number } & NativeStackData);

/** Read again while any member is open, as the description stack's members are. */
function nativeStackPollInterval(data: NativeStackData | undefined): number | false {
  return data?.read?.answer.stack?.entries.nodes.some(
    ({ pullRequest }) => pullRequest?.state === "OPEN",
  )
    ? STACK_MEMBERS_POLL_MS
    : false;
}

type PullRequestReads = ReturnType<typeof useDesktopPullRequestReads>;

/**
 * The read of GitHub's own stack for `prUrl` at `revision`, as this Mac reads that PR, for its
 * hook and for reading ahead alike; null where this page doesn't read it.
 */
export function pullRequestNativeStackQueryOptions(
  prUrl: string | null | undefined,
  revision: string | null,
  { known, readsOnThisMac, target, scope }: PullRequestReads,
) {
  const macTarget = prUrl ? target(prUrl) : null;
  const self = prUrl ? stackPullRequestRef(prUrl) : null;
  const readable =
    !!macTarget && !!self && known(prUrl) && readsOnThisMac(prUrl) && nativeStackOffered(macTarget);
  return {
    readable,
    options: queryOptions({
      queryKey: pullRequestNativeStackQueryKey(prUrl, scope(prUrl), revision),
      queryFn:
        readable && macTarget && self
          ? async ({ client, queryKey, signal }): Promise<NativeStackData> => {
              // Where the read waits comes from who wants it, and it's let go of with its query.
              const read = await readAtObservedPriority(client, queryKey, (priority) =>
                fetchNativeStack(macTarget, { priority, signal }),
              );
              return { read, members: read ? nativeStackMembers(read, self) : null };
            }
          : skipToken,
      staleTime: 30_000,
      retry: false,
    }),
  };
}

/** GitHub's own stack for `prUrl` at `revision`, read on this Mac as that PR reads. */
export function usePullRequestNativeStack(
  prUrl: string | null | undefined,
  revision: string | null,
  enabled: boolean,
): NativeStackState {
  const reads = useDesktopPullRequestReads();
  const { readable, options } = pullRequestNativeStackQueryOptions(prUrl, revision, reads);
  const wanted = enabled && readable;
  const priority = useSecondaryReadPriority();
  const query = useQuery({
    ...options,
    queryFn: wanted ? options.queryFn : skipToken,
    meta: readPriorityMeta(priority),
    refetchInterval: (current) => nativeStackPollInterval(current.state.data),
    refetchIntervalInBackground: false,
  });
  if (!wanted) return { kind: "off" };
  if (query.isPending) return { kind: "loading" };
  if (query.isError) {
    return { kind: "settled", read: undefined, members: null, readAt: query.errorUpdatedAt };
  }
  return { kind: "settled", ...query.data, readAt: query.dataUpdatedAt };
}
