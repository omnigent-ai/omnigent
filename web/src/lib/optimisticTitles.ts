// Optimistic sidebar labels for newly created sessions: the client's copy
// of the first prompt, shown until the server's title lands.
//
// Never sent to the server — a create-body title would persist as a USER
// title, which rename-wins pins forever. Derivation mirrors the server's
// synthesize_conversation_title so the swap is invisible.

import { useSyncExternalStore } from "react";

// Keep in sync with _ATTACHMENT_MARKER_RE in omnigent/entities/conversation.py
// and the preamble buildMentionPreamble emits in composerMentions.ts.
const ATTACHMENT_MARKER_RE =
  /^(?:\[Attached(?: file)?: .+\]|\[Attachment [^\]]+ could not be loaded\])$/;

// Same default limit as synthesize_conversation_title.
export const OPTIMISTIC_TITLE_LIMIT = 60;

/**
 * Derive the same one-line title the server seed would produce for a first
 * message: attachment-marker lines dropped, whitespace collapsed, truncated
 * with an ellipsis. Returns ``null`` when no usable text remains.
 */
export function synthesizeOptimisticTitle(
  text: string,
  limit: number = OPTIMISTIC_TITLE_LIMIT,
): string | null {
  const keptLines = text.split("\n").filter((line) => !ATTACHMENT_MARKER_RE.test(line.trim()));
  const collapsed = keptLines.join(" ").split(/\s+/).filter(Boolean).join(" ");
  if (!collapsed) return null;
  // Code points, not UTF-16 units, so the cap matches Python's len() and
  // never splits a surrogate pair.
  const chars = [...collapsed];
  if (chars.length <= limit) return collapsed;
  return (
    chars
      .slice(0, Math.max(0, limit - 1))
      .join("")
      .trimEnd() + "…"
  );
}

const optimisticTitles = new Map<string, string>();
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notifyListeners(): void {
  for (const listener of listeners) listener();
}

/**
 * Stash the label for a session the user just created. ``promptText`` is the
 * exact first message handed to the chat (mention preamble included); empty
 * or attachment-only prompts record nothing.
 */
export function recordOptimisticTitle(conversationId: string, promptText: string): void {
  const title = synthesizeOptimisticTitle(promptText);
  if (title === null) return;
  optimisticTitles.set(conversationId, title);
  notifyListeners();
}

/**
 * The stashed label for a session, if any. Read reactively via
 * ``useOptimisticTitle``; this plain getter serves non-component callers
 * (e.g. conversationDisplayLabel).
 */
export function getOptimisticTitle(conversationId: string): string | undefined {
  return optimisticTitles.get(conversationId);
}

/** The stashed label, reactive — re-renders when it is recorded or cleared. */
export function useOptimisticTitle(conversationId: string): string | undefined {
  return useSyncExternalStore(
    subscribe,
    () => getOptimisticTitle(conversationId),
    () => undefined,
  );
}

/** Clear all stashed labels — for logout/reset flows and isolated tests. */
export function clearOptimisticTitles(): void {
  optimisticTitles.clear();
  notifyListeners();
}
