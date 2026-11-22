// The session's web chat, rendered in its column or, while a split shows beside it, in the chat
// pane's body: the same transcript and composer either way.

import type { ReactNode } from "react";
import { createPortal } from "react-dom";

import { CHAT_PANE_BODY_ATTRIBUTE } from "@/lib/paneSurfaces";

/**
 * Render `children` in place, or in `host`, the chat pane's body, when there is one. Where the
 * panes view shows the chat, `marked` marks it in place too, so its keys reach the panes before a
 * first split. The marker names the session, so only its own view's pane keys take the chat's.
 */
export function ChatPaneBody({
  sessionId,
  host,
  marked = false,
  children,
}: {
  sessionId: string;
  host: HTMLElement | null;
  marked?: boolean;
  children: ReactNode;
}) {
  const marker = { [CHAT_PANE_BODY_ATTRIBUTE]: sessionId };
  if (host) {
    return createPortal(
      <div {...marker} className="flex min-h-0 flex-1 flex-col">
        {children}
      </div>,
      host,
    );
  }
  if (!marked) return children;
  // `contents`: the marker leaves the chat's column layout as it was.
  return (
    <div {...marker} className="contents">
      {children}
    </div>
  );
}
