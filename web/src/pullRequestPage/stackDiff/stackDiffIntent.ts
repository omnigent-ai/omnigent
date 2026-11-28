// Which changes a stacked PR shows, as the person chose on this device only, per reader and
// PR. Automatic is stored as nothing, so it keeps following the policy as that changes.

import { useCallback } from "react";

import {
  createScopedReviewMemory,
  useScopedReviewMemory,
} from "@/components/diff-review/scopedReviewMemory";
import { isStackDiffIntent, type StackDiffIntent } from "./stackDiffMode";

/** How many PRs' choices are kept across readers; past it, the oldest goes back to Automatic. */
export const STACK_DIFF_INTENT_LIMIT = 200;

export const stackDiffIntentMemory = createScopedReviewMemory({
  storageKey: "omnigent.pullRequests.stackDiffIntent",
  changeEvent: "omnigent:stack-diff-intent-change",
  maxEntries: STACK_DIFF_INTENT_LIMIT,
});

const NO_READER = "";

/** `reader`'s choice for the PR at `url`, and how to change it; nothing is kept without a reader. */
export function useStackDiffIntent(
  reader: string | null,
  url: string | null,
): [StackDiffIntent, (intent: StackDiffIntent) => void] {
  const choices = useScopedReviewMemory(stackDiffIntentMemory, reader ?? NO_READER);
  const key = url?.toLowerCase() ?? null;
  const stored = key && reader !== null ? choices.get(key) : undefined;
  const intent = isStackDiffIntent(stored) ? stored : "automatic";
  const choose = useCallback(
    (next: StackDiffIntent) => {
      if (reader === null || key === null) return;
      stackDiffIntentMemory.write(reader, key, next === "automatic" ? null : next);
    },
    [reader, key],
  );
  return [intent, choose];
}
