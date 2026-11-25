// Asking the Overview's Activity to show something: an entry, a comment posted from this page
// once its read arrives, or the section itself. A request belongs to one pull request, reader
// and pane, and is dropped when any of them changes before it's shown.

import { useCallback, useEffect, useRef, useState } from "react";

/** How long a revealed entry stays highlighted; matches the message-highlight animation. */
export const ACTIVITY_HIGHLIGHT_MS = 1500;

/** The attribute naming an entry's key on its row, which a reveal scrolls to and focuses. */
export const ACTIVITY_ID_ATTRIBUTE = "data-pull-request-activity";

export type ActivityRevealTarget =
  /** An entry by its timeline key: `comment:…`, `thread:<id>` or `event:<id>`. */
  | { kind: "entry"; key: string }
  /** A comment posted from this page, by its `postedCommentKey`, shown once the read has it. */
  | { kind: "posted"; key: string }
  /** The section with every kind, where the Comments tab's links now land. */
  | { kind: "section" };

/** Whose activity a request is for: the pane, the pull request and the account reading it. */
export interface ActivityRevealScope {
  conversationId: string;
  prUrl: string;
  account: string | null;
}

export interface ActivityRevealRequest {
  id: number;
  scope: ActivityRevealScope;
  target: ActivityRevealTarget;
}

function sameRevealScope(left: ActivityRevealScope, right: ActivityRevealScope): boolean {
  return (
    left.conversationId === right.conversationId &&
    left.prUrl === right.prUrl &&
    left.account === right.account
  );
}

/**
 * One waiting request at a time, the newest. It's dropped once Activity has shown its target,
 * or when the pane, pull request or reader changes before then.
 */
export function useActivityRevealRequest(scope: ActivityRevealScope | null) {
  const [request, setRequest] = useState<ActivityRevealRequest | null>(null);
  const nextId = useRef(0);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const reveal = useCallback((target: ActivityRevealTarget) => {
    const current = scopeRef.current;
    if (!current) return;
    nextId.current += 1;
    setRequest({ id: nextId.current, scope: current, target });
  }, []);
  const settle = useCallback(
    (id: number) => setRequest((current) => (current?.id === id ? null : current)),
    [],
  );
  const conversationId = scope?.conversationId;
  const prUrl = scope?.prUrl;
  const account = scope?.account;
  useEffect(() => {
    setRequest((current) =>
      current &&
      conversationId !== undefined &&
      prUrl !== undefined &&
      sameRevealScope(current.scope, { conversationId, prUrl, account: account ?? null })
        ? current
        : null,
    );
  }, [conversationId, prUrl, account]);
  // Checked as it's handed out too: effects below this one run first, so a request made for
  // the last scope must never reach them in the render where the scope changes.
  const current = request && scope && sameRevealScope(request.scope, scope) ? request : null;
  return { request: current, reveal, settle };
}
