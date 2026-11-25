// Opening a shell in the workspace from outside a component, such as a toast's action or a
// pane's close: the session's shell answers with the same open its own entry points use, which
// selects the shell's tab and reveals the sidebar.

import { useEffect, useRef } from "react";

export interface TerminalOpenRequest {
  key: string;
  /** Give the shell the keyboard once it shows. */
  focus: boolean;
}

type Opener = (request: TerminalOpenRequest) => void;

const openers = new Map<string, Opener>();
// Asked before the session's shell mounted; it takes the latest when it does.
const pending = new Map<string, TerminalOpenRequest>();

/** Open ``request.key`` in ``conversationId``'s workspace, now or once it mounts. */
export function requestTerminalOpen(conversationId: string, request: TerminalOpenRequest): void {
  const opener = openers.get(conversationId);
  if (opener) opener(request);
  else pending.set(conversationId, request);
}

/** Answer ``conversationId``'s open requests with ``open`` while mounted. */
export function useTerminalOpenRequests(conversationId: string | null, open: Opener): void {
  const latest = useRef(open);
  latest.current = open;
  useEffect(() => {
    if (conversationId === null) return;
    const opener: Opener = (request) => latest.current(request);
    openers.set(conversationId, opener);
    const waiting = pending.get(conversationId);
    pending.delete(conversationId);
    if (waiting) opener(waiting);
    return () => {
      if (openers.get(conversationId) === opener) openers.delete(conversationId);
    };
  }, [conversationId]);
}

/** Forget every opener and waiting request. */
export function resetTerminalOpenRequestsForTests(): void {
  openers.clear();
  pending.clear();
}
