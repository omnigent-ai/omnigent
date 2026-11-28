// Lets the PR references in a PR's view read against that PR: `#123` in its repository, looked
// up through the view's session's host, within one cap for the view. What the view already read,
// its own PR and its stack's members, shows without a lookup.

import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useMemo } from "react";

import { createPageLookupBudget } from "@/lib/pullRequestReferences/lookupQueue";
import { referenceContextFor } from "@/lib/pullRequestReferences/reference";
import {
  pullRequestPageReferenceScope,
  PullRequestReferenceScopeContext,
} from "@/lib/pullRequestReferences/scope";
import {
  type ReferenceSeed,
  seedReferencedPullRequests,
} from "@/lib/pullRequestReferences/useReferencedPullRequest";
import type { StackMembersAnswer } from "@/lib/pullRequestStacks/stackMembers";
import type { PullRequestSuggestion } from "@/lib/pullRequestSuggestions";

/**
 * The seeds a view's reads give: its own PR as read at `knownReadAt`, and its stack's members
 * as the session's host read them, at the time the read went out. Members that stand in for
 * another read, as a list's, carry no read time and seed nothing.
 */
export function viewReferenceSeeds(
  known: PullRequestSuggestion | null,
  knownReadAt: number,
  stack: StackMembersAnswer | null,
): ReferenceSeed[] {
  const seeds: ReferenceSeed[] = known ? [{ pr: known, readAt: knownReadAt }] : [];
  const readAt = stack?.readAt;
  if (!stack || readAt === undefined) return seeds;
  for (const member of stack.members) {
    seeds.push({
      pr: {
        url: member.url,
        number: member.number,
        title: member.title,
        state: member.state,
        is_draft: member.is_draft,
        repository: member.base_repository,
        updated_at: member.updated_at,
      },
      readAt,
    });
  }
  return seeds;
}

export function PullRequestReferenceScope({
  pullRequestUrl,
  known = null,
  knownReadAt = 0,
  stack = null,
  conversationId,
  children,
}: {
  /** The view's own PR; without one, references stay as written. */
  pullRequestUrl: string | null | undefined;
  /** The view's own PR as it read it, so a reference to it needs no lookup. */
  known?: PullRequestSuggestion | null;
  /** When `known` was read; 0 when unknown, so any later answer stays. */
  knownReadAt?: number;
  /** The view's stack's members as it read them. */
  stack?: StackMembersAnswer | null;
  /** The session whose host looks up the PRs the view references. */
  conversationId?: string;
  children: ReactNode;
}) {
  const queryClient = useQueryClient();
  const context = useMemo(() => referenceContextFor(pullRequestUrl), [pullRequestUrl]);
  // A new PR starts a new cap, as its lookups are new too.
  const value = useMemo(
    () =>
      context
        ? pullRequestPageReferenceScope(context, createPageLookupBudget(), conversationId)
        : null,
    [context, conversationId],
  );
  // Seeded while rendering, before any reference below subscribes and looks its PR up, and
  // again only when what the view read changes. The cache tells its observers later, batched.
  const seedsKey = JSON.stringify(viewReferenceSeeds(known, knownReadAt, stack));
  useMemo(() => {
    if (context) {
      seedReferencedPullRequests(
        queryClient,
        context,
        JSON.parse(seedsKey) as ReferenceSeed[],
        conversationId ?? null,
      );
    }
  }, [queryClient, context, seedsKey, conversationId]);
  return (
    <PullRequestReferenceScopeContext.Provider value={value}>
      {children}
    </PullRequestReferenceScopeContext.Provider>
  );
}
