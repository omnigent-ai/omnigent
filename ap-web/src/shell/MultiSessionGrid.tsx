// Side-by-side multi-session grid: renders one self-contained, fully
// interactive chat pane per session so an engineer can watch and drive several
// agents in parallel. Each pane binds its OWN live SSE stream (acquire on
// mount, release on unmount — aborting only that session's stream) and reads /
// drives its own session via the `ChatSessionScopeContext`, so the panes are
// truly isolated rather than snapshots of one active session.

import { useEffect } from "react";
import { ChatPage } from "@/pages/ChatPage";
import { acquireSession, ChatSessionScopeContext, releaseSession } from "@/store/chatStore";

/** Minimum readable pane width before the grid switches to horizontal scroll. */
const PANE_MIN_WIDTH_PX = 360;

/**
 * One pane: owns its session's bind lifecycle (lazy per pane — the stream
 * binds when this pane mounts/visible and aborts when it unmounts) and scopes
 * the reused ChatPage to its session.
 */
function SessionPane({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  useEffect(() => {
    acquireSession(sessionId);
    return () => releaseSession(sessionId);
  }, [sessionId]);

  return (
    <ChatSessionScopeContext.Provider value={sessionId}>
      <ChatPage paneSessionId={sessionId} embedded onClose={onClose} />
    </ChatSessionScopeContext.Provider>
  );
}

export interface MultiSessionGridProps {
  /** Sessions to show side by side, left to right. */
  sessionIds: string[];
  /** Remove one pane (its session's stream is released on unmount). */
  onCloseSession: (sessionId: string) => void;
}

/**
 * Unbounded horizontal row of panes. Panes share width evenly while they fit
 * (`flex-1`); once they'd shrink below {@link PANE_MIN_WIDTH_PX} the row
 * overflows and scrolls horizontally instead.
 */
export function MultiSessionGrid({ sessionIds, onCloseSession }: MultiSessionGridProps) {
  return (
    <div data-testid="multi-session-grid" className="flex min-h-0 min-w-0 flex-1 overflow-x-auto">
      {sessionIds.map((id) => (
        <div
          key={id}
          className="flex min-h-0 flex-1 flex-col border-r border-border last:border-r-0"
          style={{ minWidth: `${PANE_MIN_WIDTH_PX}px` }}
        >
          <SessionPane sessionId={id} onClose={() => onCloseSession(id)} />
        </div>
      ))}
    </div>
  );
}
