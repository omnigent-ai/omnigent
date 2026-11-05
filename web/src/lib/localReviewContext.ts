import { useSyncExternalStore } from "react";
import type { Comment } from "@/hooks/useComments";
import { getSessionDraft, setSessionDraft } from "./sessionDrafts";

export { useSessionReviewComments as usePendingLocalReviewContexts } from "./sessionDrafts";

const navigationTargets = new Map<string, Comment>();
const navigationListeners = new Set<() => void>();

const notifyNavigation = () => navigationListeners.forEach((listener) => listener());

function setContexts(sessionId: string, reviewComments: Comment[]) {
  setSessionDraft(sessionId, {
    ...(getSessionDraft(sessionId) ?? { text: "", files: [] }),
    reviewComments,
  });
}

/** Queue a saved PR comment as context for its session's chat composer. */
export function requestLocalReviewContext(comment: Comment) {
  if (!comment.anchor_metadata || comment.status !== "draft") return;
  const current = getSessionDraft(comment.conversation_id)?.reviewComments ?? [];
  if (current.some((existing) => existing.id === comment.id)) return;
  setContexts(comment.conversation_id, [...current, comment]);
}

/** Keep already-attached context current without reattaching removed chips. */
export function updateLocalReviewContext(comment: Comment) {
  const current = getSessionDraft(comment.conversation_id)?.reviewComments;
  if (!current?.some((item) => item.id === comment.id)) return;
  if (comment.status !== "draft") removeLocalReviewContext(comment.conversation_id, comment.id);
  else
    setContexts(
      comment.conversation_id,
      current.map((item) => (item.id === comment.id ? comment : item)),
    );
}

export function removeLocalReviewContext(sessionId: string, commentId: string) {
  const current = getSessionDraft(sessionId)?.reviewComments;
  if (current?.some((comment) => comment.id === commentId))
    setContexts(
      sessionId,
      current.filter((comment) => comment.id !== commentId),
    );
}

export function clearLocalReviewContexts(sessionId: string) {
  setContexts(sessionId, []);
}

/** Ask the PR Changes tab to reveal and focus a saved local comment. */
export function requestLocalReviewNavigation(comment: Comment) {
  navigationTargets.set(comment.conversation_id, comment);
  notifyNavigation();
}

export function consumeLocalReviewNavigation(sessionId: string) {
  navigationTargets.delete(sessionId);
  notifyNavigation();
}

export function useLocalReviewNavigation(sessionId: string): Comment | undefined {
  return useSyncExternalStore(
    (listener) => {
      navigationListeners.add(listener);
      return () => {
        navigationListeners.delete(listener);
      };
    },
    () => navigationTargets.get(sessionId),
    () => undefined,
  );
}
