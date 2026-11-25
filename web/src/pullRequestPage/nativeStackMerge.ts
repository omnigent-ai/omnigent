// Merging a PR in GitHub's own stack: GitHub merges the stack together, and only on GitHub,
// so every merge entry point (the merge bar's button and ⌘K) hands off there, defined once.

import { openLink } from "@/lib/openLinkInApp";
import { type NativeStackAnswer, readNativeStack } from "@/lib/pullRequestStacks/nativeStack";
import { stackPullRequestRef } from "@/lib/pullRequestStacks/stackModel";
import { mergesOnGitHub } from "@/lib/pullRequestStacks/stackTrust";

import type { PullRequestStackView } from "./usePullRequestStack";

/** A merge this page leaves to GitHub. */
export interface NativeStackMergeHandoff {
  /** The PR on GitHub, where it merges. */
  url: string;
  /** The unmerged PRs below this one, which merge with it; null where the read can't say. */
  unmergedBelow: number | null;
}

export const MERGE_ON_GITHUB = {
  label: "Merge on GitHub",
  paletteLabel: "Merge Pull Request on GitHub",
  keywords: ["merge", "stack", "github", "pull request", "pr"],
} as const;

/** The merge `stack` hands off to GitHub for the PR at `url`, or null where it merges here. */
export function nativeStackMergeHandoff(
  stack: PullRequestStackView | null,
  url: string,
): NativeStackMergeHandoff | null {
  if (!stack || !mergesOnGitHub(stack.parse)) return null;
  return { url, unmergedBelow: countUnmergedBelow(stack) };
}

function countUnmergedBelow(stack: PullRequestStackView): number | null {
  // A stack read in part knows its PRs below only when there are none.
  if (stack.partial) return stack.position.level === 1 ? 0 : null;
  return stack.parse.nodes.slice(0, stack.parse.selfIndex).filter((node) => !node.markers.merged)
    .length;
}

/**
 * The merge GitHub's `answer` for the PR at `url` hands off, as a list's read gives it, or null
 * where the PR merges here or the answer can't say. A stack read in part counts the PRs below
 * only where there are none.
 */
export function nativeStackAnswerHandoff(
  answer: NativeStackAnswer | undefined,
  url: string,
): NativeStackMergeHandoff | null {
  const self = answer ? stackPullRequestRef(url) : null;
  if (!answer || !self) return null;
  const read = readNativeStack(answer, self);
  if (read.kind === "stacked") {
    const position = read.parse.selfIndex + 1;
    const below = read.entries.filter(
      (entry) => entry.position < position && entry.member.state !== "MERGED",
    );
    return { url, unmergedBelow: below.length };
  }
  if (read.kind === "stacked-partial") {
    return { url, unmergedBelow: read.position === 1 ? 0 : null };
  }
  return null;
}

/** Why the merge happens on GitHub, as the merge bar and ⌘K both say it. */
export function mergeOnGitHubDescription({ unmergedBelow }: NativeStackMergeHandoff): string {
  if (unmergedBelow === 0) return "This pull request is in a GitHub stack, so it merges on GitHub.";
  if (unmergedBelow === null) {
    return "GitHub merges a stack together: merging this pull request also merges any unmerged pull requests below it.";
  }
  return unmergedBelow === 1
    ? "GitHub merges a stack together: merging this pull request also merges the unmerged pull request below it."
    : `GitHub merges a stack together: merging this pull request also merges the ${unmergedBelow} unmerged pull requests below it.`;
}

/** Open the PR on GitHub to merge it there; nothing is merged from here. */
export function mergeOnGitHub({ url }: NativeStackMergeHandoff): void {
  openLink(undefined, url);
}
