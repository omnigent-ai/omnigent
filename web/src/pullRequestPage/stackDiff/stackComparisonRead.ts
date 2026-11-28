// What comparing a stacked PR with its parent asks for, worked out the same way for the PR on
// screen and for a member read ahead: whose choice of changes applies, whether a comparison is
// wanted, exactly which one, and the query that reads it.

import { queryOptions, skipToken } from "@tanstack/react-query";

import { readAtObservedPriority } from "@/hooks/pullRequestReadPriority";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import {
  descriptionDigest,
  fetchStackComparisonOnHost,
} from "@/lib/pullRequestStacks/stackCompare";
import type { StackMember } from "@/lib/pullRequestStacks/stackMembers";
import { previousInStack } from "@/lib/pullRequestStacks/stackNavigation";
import { trustedForComparison } from "@/lib/pullRequestStacks/stackTrust";
import type { PullRequestStackView } from "@/pullRequestPage/usePullRequestStack";

import { type StackComparisonIdentity, stackComparisonKey } from "./stackComparisonState";
import { isStackDiffIntent, type StackDiffIntent, wantsStackComparison } from "./stackDiffMode";
import { stackDiffIntentMemory } from "./stackDiffIntent";

export const STACK_COMPARE_QUERY = "pull-request-stack-compare";

/** The PR on screen as the page last read it. */
export interface StackDiffPullRequest {
  url: string;
  body?: string | null;
  head_sha?: string;
  base_sha?: string;
  base_ref?: string | null;
}

/** Who a choice of changes belongs to: the PR's code host, read through any session; null without one. */
export function stackDiffAccount(target: CodeHostPullRequest | null): string | null {
  return target ? JSON.stringify([target.provider, target.host.toLowerCase()]) : null;
}

/** The choice `account` made for the PR at `url`, outside any view; Automatic when none. */
export function storedStackDiffIntent(account: string | null, url: string): StackDiffIntent {
  if (account === null) return "automatic";
  const stored = stackDiffIntentMemory.read(account).get(url.toLowerCase());
  return isStackDiffIntent(stored) ? stored : "automatic";
}

/** The parent this PR's own changes are counted from, when the members read found it. */
export function directParent(stack: PullRequestStackView | null): StackMember | null {
  if (!stack || !trustedForComparison(stack.parse)) return null;
  const step = previousInStack(stack.parse);
  if (step.kind !== "go") return null;
  const status = stack.statuses.nodes[step.destination.index];
  if (status?.kind !== "member" || !status.member.head_sha) return null;
  return status.member;
}

// Repository names ignore case on GitHub; branch names don't.
const sameRepository = (left: string | null | undefined, right: string | null | undefined) =>
  !!left && !!right && left.toLowerCase() === right.toLowerCase();
const sameBranch = (left: string | null | undefined, right: string | null | undefined) =>
  !!left && left === right;

/** Whether `pr` can be compared with its parent here, whether its choice wants it, and which. */
export interface StackComparisonPlan {
  parent: StackMember | null;
  /** The session's host can compare, and the PR's parent and commits are known. */
  offered: boolean;
  /** The PR's base is its parent's branch, so all its changes are already its own. */
  baseIsParent: boolean;
  /** The choice of changes, under its policy, asks for the comparison. */
  wanted: boolean;
  /** The exact comparison it would read, or null where it can't. */
  identity: StackComparisonIdentity | null;
}

export function stackComparisonPlan({
  pr,
  stack,
  target,
  conversationId,
  intent,
  automaticPolicy,
}: {
  pr: StackDiffPullRequest | null;
  stack: PullRequestStackView | null;
  /** The PR shown, where the session's host compares it; null elsewhere. */
  target: CodeHostPullRequest | null;
  conversationId: string;
  intent: StackDiffIntent;
  automaticPolicy: boolean;
}): StackComparisonPlan {
  const parent = directParent(stack);
  const offered = !!pr?.head_sha && !!pr.base_sha && !!parent && !!target && !!conversationId;
  const baseIsParent =
    !!target &&
    sameRepository(parent?.head_repository, target.repository) &&
    sameBranch(pr?.base_ref, parent?.head_ref);
  const wanted = offered && wantsStackComparison({ intent, automaticPolicy, baseIsParent });
  const identity: StackComparisonIdentity | null =
    offered && pr?.head_sha && pr.base_sha && parent?.head_sha && target
      ? {
          kind: "stack-comparison",
          reader: JSON.stringify(["host", conversationId]),
          repository: target.repository,
          number: target.number,
          parentNumber: parent.number,
          parentState: parent.state,
          childHead: pr.head_sha,
          parentHead: parent.head_sha,
          nativeBase: pr.base_sha,
          childBody: pr.body ?? "",
          nativeStack: stack?.nativeRevision ?? null,
        }
      : null;
  return { parent, offered, baseIsParent, wanted, identity };
}

/** Every comparison of the PR at `url` through `conversationId`'s host, to drop them all at once. */
export function stackComparisonsKey(conversationId: string, url: string | null): unknown[] {
  return [STACK_COMPARE_QUERY, "host", conversationId, url];
}

/**
 * The read of one comparison, for the PR's view and for reading it ahead alike. Its answer is
 * for those exact commits and description, so it never goes stale; another is another key.
 */
export function stackComparisonQueryOptions(
  conversationId: string,
  url: string | null,
  identity: StackComparisonIdentity | null,
) {
  return queryOptions({
    queryKey: [
      ...stackComparisonsKey(conversationId, url),
      identity ? stackComparisonKey(identity) : null,
    ],
    queryFn:
      identity && url
        ? async ({ client, queryKey }) => {
            const childBodyDigest = await descriptionDigest(identity.childBody);
            return readAtObservedPriority(client, queryKey, () =>
              fetchStackComparisonOnHost(conversationId, url, {
                parentNumber: identity.parentNumber,
                parentRepository: identity.repository,
                parentOpen: identity.parentState === "OPEN",
                childHead: identity.childHead,
                nativeBase: identity.nativeBase,
                parentHead: identity.parentHead,
                childBodyDigest,
              }),
            );
          }
        : skipToken,
    staleTime: Infinity,
    retry: false,
  });
}
