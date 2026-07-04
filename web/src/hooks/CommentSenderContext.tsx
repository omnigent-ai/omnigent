// `useSendCommentsToAgent` requires a non-null agentId, but FileViewer
// renders before knowing if an agent exists (Rules of Hooks forbid the
// conditional hook call). This provider absorbs the null branch so the
// hook keeps its strict signature.

import { type ReactNode, createContext, useContext, useMemo } from "react";
import { useSendCommentsToAgent } from "@/hooks/useComments";

interface CommentSender {
  /** Fire the POST /comments/send mutation. */
  mutate: ReturnType<typeof useSendCommentsToAgent>["mutate"];
  /** True while a `mutate` call is in flight. */
  isPending: boolean;
}

const CommentSenderContext = createContext<CommentSender | null>(null);

/**
 * Provides a `CommentSender` to descendants when `agentId` is non-null.
 * When `agentId` is `null`, the context value is `null` and descendants
 * see "no sender available" — they should hide any send-comments UI.
 *
 * Pass `agentId` directly; this component handles the null branch
 * internally so the caller doesn't need a Rules-of-Hooks-safe
 * conditional render.
 *
 * @param sessionId - Active conversation id, e.g. ``"conv_abc123"``.
 * @param agentId - Resolved agent id, or null when no agent is
 *   registered for the current server.
 */
export function CommentSenderProvider({
  sessionId,
  agentId,
  children,
}: {
  sessionId: string;
  agentId: string | null;
  children: ReactNode;
}) {
  if (agentId === null) {
    return <CommentSenderContext.Provider value={null}>{children}</CommentSenderContext.Provider>;
  }
  return (
    <AgentBoundSenderProvider sessionId={sessionId} agentId={agentId}>
      {children}
    </AgentBoundSenderProvider>
  );
}

function AgentBoundSenderProvider({
  sessionId,
  agentId,
  children,
}: {
  sessionId: string;
  agentId: string;
  children: ReactNode;
}) {
  const mutation = useSendCommentsToAgent(sessionId, agentId);
  const value = useMemo(
    () => ({ mutate: mutation.mutate, isPending: mutation.isPending }),
    [mutation.mutate, mutation.isPending],
  );
  return <CommentSenderContext.Provider value={value}>{children}</CommentSenderContext.Provider>;
}

/**
 * Read the ambient `CommentSender` from context.
 *
 * Returns `null` when not wrapped in a provider, or when the provider
 * was given a null `agentId`. Callers MUST null-check before invoking
 * `mutate` — and should hide any "Send to agent" UI in that case.
 */
export function useOptionalCommentSender(): CommentSender | null {
  return useContext(CommentSenderContext);
}
