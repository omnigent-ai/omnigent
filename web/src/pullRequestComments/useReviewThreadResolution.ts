// Resolving and unresolving one review conversation: whether the session's host offers it for
// the thread, and the one command the thread's button, its ⋯ menu and ⌘K all run. A change on its
// way is kept per conversation, so every view of the thread shows it and none sends it twice.

import { useSyncExternalStore } from "react";
import { toast } from "sonner";

import { createLastFocusedScope } from "@/hooks/lastFocusedScope";
import { usePullRequestThreadResolutionWrite } from "@/newPullRequestPage/hooks/usePullRequests";
import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";

export const RESOLVE_CONVERSATION = "Resolve Conversation";
export const UNRESOLVE_CONVERSATION = "Unresolve Conversation";

export interface ThreadResolutionCommand {
  /** True when the command resolves the conversation, false when it unresolves it. */
  resolves: boolean;
  label: typeof RESOLVE_CONVERSATION | typeof UNRESOLVE_CONVERSATION;
  /** A change to this conversation is on its way; the command waits for it. */
  pending: boolean;
  run: () => void;
}

/** The review conversation worked in last, whose command ⌘K offers. */
export const reviewThreadFocus = createLastFocusedScope();

// Conversations with a change on its way, by workspace user, PR and thread.
const sending = new Set<string>();
const listeners = new Set<() => void>();
let version = 0;

function changed(): void {
  version += 1;
  for (const listener of listeners) listener();
}

const sendingKey = (owner: string, prUrl: string, threadId: string) =>
  [owner, prUrl.toLowerCase(), threadId].join("\n");

function useSendingVersion(): number {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => version,
    () => 0,
  );
}

/** Codes that say the conversation is already as asked: nothing to tell, the read shows it. */
const ALREADY = new Set(["already_resolved", "already_unresolved"]);

/**
 * The thread's Resolve or Unresolve, or null where the session's host can't make that change:
 * GitHub doesn't let the viewer, the read predates the permission, or the host doesn't serve it.
 */
export function useReviewThreadResolution(
  thread: Pick<ReviewThread, "id" | "resolved" | "can_resolve" | "can_unresolve">,
  context: { conversationId: string; prUrl: string },
  /** A change this view started that GitHub didn't make, or that went nowhere. */
  onNotMade?: () => void,
): ThreadResolutionCommand | null {
  const owner = useWorkspaceDraftOwner();
  useSendingVersion();
  const resolveWrite = usePullRequestThreadResolutionWrite(
    context.conversationId,
    "pull_request_review_thread_resolve",
  );
  const unresolveWrite = usePullRequestThreadResolutionWrite(
    context.conversationId,
    "pull_request_review_thread_unresolve",
  );
  const hostResolves = usePullRequestMethodServed(
    context.conversationId,
    "setReviewThreadResolved",
  );
  const resolves = !thread.resolved;
  const allowed = resolves ? thread.can_resolve === true : thread.can_unresolve === true;
  if (!allowed || !hostResolves) return null;
  const key = sendingKey(owner, context.prUrl, thread.id);
  const pending = sending.has(key);
  return {
    resolves,
    label: resolves ? RESOLVE_CONVERSATION : UNRESOLVE_CONVERSATION,
    pending,
    run: () => {
      if (sending.has(key)) return;
      sending.add(key);
      changed();
      const settle = () => {
        sending.delete(key);
        changed();
      };
      const write = resolves ? resolveWrite : unresolveWrite;
      write.mutate(
        { prUrl: context.prUrl, threadId: thread.id, sentAt: Date.now() },
        {
          onSuccess: settle,
          onError: (error) => {
            settle();
            if (error instanceof PullRequestWriteError && ALREADY.has(error.code)) return;
            onNotMade?.();
            toast.error(error.message);
          },
        },
      );
    },
  };
}

export function resetReviewThreadResolutionsForTests(): void {
  sending.clear();
  changed();
}
