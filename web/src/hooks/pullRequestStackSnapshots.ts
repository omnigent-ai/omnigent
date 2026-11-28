// Where each stack's shared record lives: one entry per stack under its reader's scope, and an
// index from each member to the stack its newest read put it in, so a reader or account change
// keeps its records apart and Refresh drops them all. Every entry carries when the read that
// said it came back, and only a newer read replaces it.

import type { QueryClient } from "@tanstack/react-query";

import { stackPullRequestRef, stackRefKey } from "@/lib/pullRequestStacks/stackModel";
import type { StackSnapshot } from "@/lib/pullRequestStacks/stackSnapshot";

export const STACK_SNAPSHOT_QUERY = "pull-request-stack-snapshot";
export const STACK_MEMBERSHIP_QUERY = "pull-request-stack-membership";

/** The stack a member's newest read put it in, or null where that read found it in none. */
export interface StackMembership {
  identity: string | null;
  readAt: number;
}

export function stackSnapshotQueryKey(scope: readonly unknown[], identity: string): unknown[] {
  return [STACK_SNAPSHOT_QUERY, ...scope, identity];
}

export function stackMembershipQueryKey(scope: readonly unknown[], member: string): unknown[] {
  return [STACK_MEMBERSHIP_QUERY, ...scope, member];
}

const memberKey = (url: string) => {
  const ref = stackPullRequestRef(url);
  return ref ? stackRefKey(ref) : null;
};

/** Say where `member` stands as of `readAt`, unless a read at least as new already said. */
function setMembership(
  queryClient: QueryClient,
  scope: readonly unknown[],
  member: string,
  next: StackMembership,
): void {
  const key = stackMembershipQueryKey(scope, member);
  const known = queryClient.getQueryData<StackMembership>(key);
  if (known && known.readAt >= next.readAt) return;
  queryClient.setQueryData(key, next);
}

/**
 * Keep `snapshot` as its stack's record and point each member at it, when it's newer than
 * what's kept. A member the newer read no longer lists leaves the stack.
 */
export function recordStackSnapshot(
  queryClient: QueryClient,
  scope: readonly unknown[],
  snapshot: StackSnapshot,
): void {
  const key = stackSnapshotQueryKey(scope, snapshot.identity);
  const known = queryClient.getQueryData<StackSnapshot>(key);
  // The same answer again, or one older than what's kept, says nothing new.
  if (known && known.readAt >= snapshot.readAt) return;
  queryClient.setQueryData(key, snapshot);
  const { identity, readAt } = snapshot;
  for (const member of snapshot.members)
    setMembership(queryClient, scope, member, { identity, readAt });
  for (const member of known?.members ?? []) {
    if (snapshot.members.includes(member)) continue;
    const membership = queryClient.getQueryData<StackMembership>(
      stackMembershipQueryKey(scope, member),
    );
    // Only a member this stack still holds leaves it; one another stack took stays there.
    if (membership?.identity === identity) {
      setMembership(queryClient, scope, member, { identity: null, readAt });
    }
  }
}

/** A member's own read, at `readAt`, found it in no stack. */
export function forgetStackMembership(
  queryClient: QueryClient,
  scope: readonly unknown[],
  url: string,
  readAt: number,
): void {
  const member = memberKey(url);
  if (member) setMembership(queryClient, scope, member, { identity: null, readAt });
}

/** The record of the stack `url`'s newest read put it in, if any. */
export function stackSnapshotOf(
  queryClient: QueryClient,
  scope: readonly unknown[],
  url: string,
): StackSnapshot | null {
  const member = memberKey(url);
  if (!member) return null;
  const membership = queryClient.getQueryData<StackMembership>(
    stackMembershipQueryKey(scope, member),
  );
  if (!membership?.identity) return null;
  return (
    queryClient.getQueryData<StackSnapshot>(stackSnapshotQueryKey(scope, membership.identity)) ??
    null
  );
}

/** Drop every stack record, as Refresh does, so no member opens with one from before it. */
export function dropStackSnapshots(queryClient: QueryClient): void {
  queryClient.removeQueries({ queryKey: [STACK_SNAPSHOT_QUERY] });
  queryClient.removeQueries({ queryKey: [STACK_MEMBERSHIP_QUERY] });
}
