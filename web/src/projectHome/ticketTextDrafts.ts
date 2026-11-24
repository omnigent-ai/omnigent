import { useCallback, useState } from "react";

const DRAFT_PREFIX = "omnigent.ticketDraft:";

/** Storage key for a ticket's unsent comment. */
export function ticketCommentDraftKey(projectId: string, ticketId: string): string {
  return `comment:${projectId}:${ticketId}`;
}

/** Storage key for one field of a project's unsaved new-ticket form. */
export function newTicketDraftKey(projectName: string, field: "title" | "description"): string {
  return `new:${projectName}:${field}`;
}

export function readTicketDraft(key: string): string | null {
  try {
    return window.localStorage.getItem(DRAFT_PREFIX + key);
  } catch {
    return null;
  }
}

/** Save *text* under *key*; blank text removes the draft. */
export function writeTicketDraft(key: string, text: string): void {
  try {
    if (text.trim()) window.localStorage.setItem(DRAFT_PREFIX + key, text);
    else window.localStorage.removeItem(DRAFT_PREFIX + key);
  } catch {
    // Storage full or unavailable: the text still lives in component state.
  }
}

type TextUpdate = string | ((previous: string) => string);

/**
 * Text state persisted to localStorage under *key*, so it survives navigation
 * and reloads. Without a key it behaves like plain `useState`.
 */
export function useTicketDraft(
  key: string | undefined,
  fallback = "",
): [string, (update: TextUpdate) => void] {
  const [text, setText] = useState(() => (key && readTicketDraft(key)) ?? fallback);
  const update = useCallback(
    (next: TextUpdate) =>
      setText((previous) => {
        const value = typeof next === "function" ? next(previous) : next;
        if (key) writeTicketDraft(key, value);
        return value;
      }),
    [key],
  );
  return [text, update];
}
