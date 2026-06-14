// localStorage-backed shell-style prompt recall for the chat composer.
//
// Mirrors the terminal REPL's up-arrow history (prompt-toolkit's FileHistory
// against ~/.omnigent_history). Web-side, history persists across tabs and
// reloads via localStorage; the recall cursor is in-memory only, matching
// shell semantics where each new login starts at the bottom.
//
// History is scoped per conversation: each chat keeps its own recall stack
// under a conversation-specific key, so ArrowUp in one chat never surfaces a
// prompt typed in another. A surface with no bound conversation falls back to
// the bare legacy key.

import { useCallback, useEffect, useRef } from "react";

const STORAGE_PREFIX = "omnigent:prompt-history";
const MAX_ENTRIES = 100;

/**
 * Resolve the localStorage key for a given history scope.
 *
 * @param scope The conversation id the history belongs to, e.g.
 *   ``"conv_abc123"``. When `null`/`undefined` (no bound conversation), returns
 *   the bare legacy key so unscoped surfaces share one stack.
 * @returns The localStorage key, e.g. ``"omnigent:prompt-history:conv_abc123"``.
 */
function scopedKey(scope: string | null | undefined): string {
  return scope ? `${STORAGE_PREFIX}:${scope}` : STORAGE_PREFIX;
}

function readHistory(key: string): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is string => typeof x === "string");
  } catch {
    return [];
  }
}

function writeHistory(key: string, entries: string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(entries));
  } catch {
    // Quota exceeded or storage disabled — non-fatal; recall just stops
    // persisting until the next successful write.
  }
}

/**
 * Append `text` to the persisted prompt history for `scope` (localStorage),
 * trimming first and skipping empty / consecutive-duplicate entries (matching
 * shell ``HISTCONTROL=ignoredups``). Caps at {@link MAX_ENTRIES} with FIFO
 * eviction.
 *
 * Standalone (not part of the hook) so non-composer surfaces can contribute
 * to a conversation's history — notably the home-page landing composer, which
 * already knows the new session id and writes under it so the first message is
 * recallable with ArrowUp in the freshly-opened chat (the chat composer's
 * `usePromptHistory` reads that same scoped key on mount).
 *
 * @param text The prompt text to record, e.g. ``"read the README"``.
 * @param scope The conversation id to scope the entry to, e.g.
 *   ``"conv_abc123"``; `null`/omitted writes to the shared legacy key.
 * @returns The new persisted history (unchanged on a skipped duplicate), or
 *   `null` when `text` was empty/whitespace and nothing was written.
 */
export function appendPromptHistoryEntry(
  text: string,
  scope?: string | null,
): string[] | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const key = scopedKey(scope);
  const history = readHistory(key);
  // Collapse consecutive duplicates against the persisted tail, so the same
  // prompt sent from the landing composer and then again in-chat isn't stored
  // twice.
  if (history.length > 0 && history[history.length - 1] === trimmed) return history;
  const next = [...history, trimmed];
  const capped = next.length > MAX_ENTRIES ? next.slice(-MAX_ENTRIES) : next;
  writeHistory(key, capped);
  return capped;
}

export interface PromptHistory {
  /**
   * Append `text` to history and reset the recall cursor. Trims first;
   * skips empty / whitespace-only and consecutive duplicates.
   */
  appendEntry: (text: string) => void;

  /**
   * Walk back to an older entry. On first call, captures `currentText` as
   * the draft so it can be restored later. Returns the recalled string,
   * or null when there's nothing to recall (history empty, or already at
   * the oldest entry).
   */
  recallPrevious: (currentText: string) => string | null;

  /**
   * Walk forward toward newer entries. Returns the recalled string, or
   * the saved draft (possibly "") when stepping past the newest entry.
   * Returns null when not currently recalling.
   */
  recallNext: () => string | null;

  /** Drop the recall cursor and any saved draft. Call on user edit / submit. */
  resetCursor: () => void;
}

/**
 * Imperative recall API, scoped to a single conversation. The hook does not
 * trigger re-renders — callers read history values from the returned methods
 * inside event handlers (keydown, submit) and apply them to whatever input
 * state they own.
 *
 * Cursor convention: 0 = newest, increasing = older. When `cursor` is
 * `null`, no recall is in progress and `draft` is undefined.
 *
 * @param scope The conversation id whose history to recall, e.g.
 *   ``"conv_abc123"``. Changing it re-hydrates from that conversation's key and
 *   drops any in-progress recall, so ArrowUp never surfaces another chat's
 *   prompts. `null`/omitted uses the shared legacy key (no bound conversation).
 */
export function usePromptHistory(scope?: string | null): PromptHistory {
  const historyRef = useRef<string[]>([]);
  const cursorRef = useRef<number | null>(null);
  const draftRef = useRef<string | null>(null);
  // Live scope for the stable appendEntry callback (avoids recreating it per switch).
  const scopeRef = useRef(scope);
  scopeRef.current = scope;

  // Re-read the active conversation's stack on mount and each switch; reset the
  // cursor so the previous chat's in-progress recall can't bleed in.
  useEffect(() => {
    historyRef.current = readHistory(scopedKey(scope));
    cursorRef.current = null;
    draftRef.current = null;
  }, [scope]);

  const resetCursor = useCallback(() => {
    cursorRef.current = null;
    draftRef.current = null;
  }, []);

  const appendEntry = useCallback(
    (text: string) => {
      // Persist via the shared module helper (trims, dedupes, caps), then
      // sync our in-memory ref to the returned history so recall sees the new
      // entry without a re-read. `null` means nothing was written (empty text).
      const capped = appendPromptHistoryEntry(text, scopeRef.current);
      if (capped !== null) historyRef.current = capped;
      resetCursor();
    },
    [resetCursor],
  );

  const recallPrevious = useCallback((currentText: string): string | null => {
    const history = historyRef.current;
    if (history.length === 0) return null;

    if (cursorRef.current === null) {
      // Entering recall mode: stash the in-progress text so ArrowDown can
      // restore it. Captures the literal value (including whitespace) —
      // we're not the source of truth for trim semantics here.
      draftRef.current = currentText;
      cursorRef.current = 0;
    } else if (cursorRef.current < history.length - 1) {
      cursorRef.current += 1;
    } else {
      // Already at the oldest entry — no change. Returning null lets the
      // caller skip preventDefault, but our keydown gate already swallowed
      // the event by triggering recall mode at all, so behavior is "stay
      // on oldest" either way.
      return null;
    }

    return history[history.length - 1 - cursorRef.current];
  }, []);

  const recallNext = useCallback((): string | null => {
    if (cursorRef.current === null) return null;
    const history = historyRef.current;
    if (cursorRef.current > 0) {
      cursorRef.current -= 1;
      return history[history.length - 1 - cursorRef.current];
    }
    // Stepping past the newest entry — drop back to the saved draft. Empty
    // string is a valid draft (user had nothing typed when recall began).
    const draft = draftRef.current ?? "";
    cursorRef.current = null;
    draftRef.current = null;
    return draft;
  }, []);

  return { appendEntry, recallPrevious, recallNext, resetCursor };
}
