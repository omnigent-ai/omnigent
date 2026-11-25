// One view of a review conversation and its Resolve or Unresolve: the command, the claim on
// ⌘K while this view was worked in last, and a call once a change started here shows in the
// thread, so the view can fold or open and keep focus where the person was.

import { useEffect, useRef } from "react";

import type { ReviewThread } from "@/lib/pullRequestReviewThreads";

import {
  reviewThreadFocus,
  type ThreadResolutionCommand,
  useReviewThreadResolution,
} from "./useReviewThreadResolution";

export function useResolutionHere(
  thread: Pick<ReviewThread, "id" | "resolved" | "can_resolve" | "can_unresolve">,
  context: { conversationId: string; prUrl: string },
  onChangedHere: (resolved: boolean) => void,
): {
  command: ThreadResolutionCommand | null;
  focus: ReturnType<typeof reviewThreadFocus.useLastFocused>;
} {
  // The state a change from this view asked for, until the thread shows it or it isn't made.
  const asked = useRef<boolean | null>(null);
  const command = useReviewThreadResolution(thread, context, () => {
    asked.current = null;
  });
  const focus = reviewThreadFocus.useLastFocused();
  const latest = useRef(onChangedHere);
  latest.current = onChangedHere;
  useEffect(() => {
    if (asked.current === null || asked.current !== thread.resolved) return;
    asked.current = null;
    latest.current(thread.resolved);
  }, [thread.resolved]);
  return {
    command: command && {
      ...command,
      run: () => {
        asked.current = command.resolves;
        command.run();
      },
    },
    focus,
  };
}
