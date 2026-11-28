// GitHub's own stack for a PR, read through the session's host where it serves the read; off
// elsewhere, so the description's stack decides.

import { queryOptions, skipToken, useQuery } from "@tanstack/react-query";

import {
  fetchNativeStackOnHost,
  NATIVE_STACK_HOST_METHOD,
  NATIVE_STACK_QUERY,
  nativeStackMembers,
  type NativeStackReadAnswer,
} from "@/lib/pullRequestStacks/nativeStackHost";
import type { StackMembersAnswer } from "@/lib/pullRequestStacks/stackMembers";
import { stackPullRequestRef } from "@/lib/pullRequestStacks/stackModel";
import { usePullRequestCapabilityServed } from "@/pullRequestPage/pullRequestPageRoute";

/**
 * The native stack's cache key: the session whose host read it, and the revision the page read
 * the PR at, so a push or retarget reads it again and an answer still in flight for the one
 * before is never shown for it.
 */
export function pullRequestNativeStackQueryKey(
  prUrl: string | null | undefined,
  sessionId: string,
  revision: string | null,
): unknown[] {
  return [NATIVE_STACK_QUERY, prUrl, "host", sessionId, revision];
}

/** GitHub's stack and its members, as one read gave both. */
export interface NativeStackData {
  /** Undefined when GitHub or the host couldn't say. */
  read: NativeStackReadAnswer | undefined;
  /** The members of the stack's kept entries, as the account it was read as. */
  members: StackMembersAnswer | null;
}

/** Where GitHub's own stack for a PR stands, as the session's host reads it. */
export type NativeStackState =
  /** Not read: flag off, no PR, or a host that doesn't serve the read. */
  | { kind: "off" }
  | { kind: "loading" }
  /**
   * Read, or failed, which can't say either: the description decides then. `readAt` is when
   * that answer came, so what it says can be ordered against other members' reads.
   */
  | ({ kind: "settled"; readAt: number } & NativeStackData);

/** The read of GitHub's own stack for `prUrl` at `revision` through `sessionId`'s host. */
export function pullRequestNativeStackQueryOptions(
  sessionId: string,
  prUrl: string | null | undefined,
  revision: string | null,
  served: boolean,
) {
  const self = prUrl ? stackPullRequestRef(prUrl) : null;
  const readable = served && !!sessionId && !!prUrl && !!self;
  return {
    readable,
    options: queryOptions({
      queryKey: pullRequestNativeStackQueryKey(prUrl, sessionId, revision),
      queryFn:
        readable && prUrl && self
          ? async ({ signal }): Promise<NativeStackData> => {
              const read = await fetchNativeStackOnHost(sessionId, prUrl, signal);
              return { read, members: read ? nativeStackMembers(read, self) : null };
            }
          : skipToken,
      staleTime: 30_000,
      retry: false,
      // Read on open, Refresh and Reload only: never on a timer, focus or reconnect.
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
    }),
  };
}

/** GitHub's own stack for `prUrl` at `revision`, where `sessionId`'s host serves the read. */
export function usePullRequestNativeStack(
  sessionId: string,
  prUrl: string | null | undefined,
  revision: string | null,
  enabled: boolean,
): NativeStackState {
  const served = usePullRequestCapabilityServed(sessionId, NATIVE_STACK_HOST_METHOD);
  const { readable, options } = pullRequestNativeStackQueryOptions(
    sessionId,
    prUrl,
    revision,
    served,
  );
  const wanted = enabled && readable;
  const query = useQuery({
    ...options,
    queryFn: wanted ? options.queryFn : skipToken,
  });
  if (!wanted) return { kind: "off" };
  if (query.isPending) return { kind: "loading" };
  if (query.isError) {
    return { kind: "settled", read: undefined, members: null, readAt: query.errorUpdatedAt };
  }
  return { kind: "settled", ...query.data, readAt: query.dataUpdatedAt };
}
