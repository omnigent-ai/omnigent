// Which of a session's terminal list answers to trust when pruning saved panes: terminals seen
// since the list was last empty, and only a complete answer newer than that reset. SSE refills
// the list one terminal at a time, so an unseen terminal is gone only per such an answer.

import { type RefObject, useEffect, useRef } from "react";

export interface TerminalListGeneration {
  conversationId: string;
  /** Terminals listed since the reset; one that leaves the list is gone. */
  seen: Set<string>;
  /** When the list was last empty, or the session changed. */
  startedAt: number;
}

/** The generation now, starting a new one once the list is empty or the session changed. */
export function currentTerminalListGeneration(
  generationRef: RefObject<TerminalListGeneration>,
  conversationId: string,
  terminalCount: number,
): TerminalListGeneration {
  const generation = generationRef.current;
  if (generation.conversationId === conversationId && terminalCount > 0) return generation;
  const next = { conversationId, seen: new Set<string>(), startedAt: Date.now() };
  generationRef.current = next;
  return next;
}

/**
 * The session's list generation, kept by a component that stays mounted while the list is
 * empty, so an answer from before an empty list isn't trusted once panes show again.
 */
export function useTerminalListGeneration(
  conversationId: string,
  terminalCount: number,
): RefObject<TerminalListGeneration> {
  const generationRef = useRef<TerminalListGeneration>({
    conversationId,
    seen: new Set(),
    startedAt: 0,
  });
  const empty = terminalCount === 0;
  useEffect(() => {
    currentTerminalListGeneration(generationRef, conversationId, empty ? 0 : 1);
  }, [conversationId, empty]);
  return generationRef;
}
