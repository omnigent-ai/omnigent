// How tall each message box is: compact, growing with the draft up to its cap, or expanded to
// a tall editor. Each session's composer keeps its own choice for as long as the app runs, so
// maximizing one never carries over to another, and a new session starts compact.

import { useCallback, useState, useSyncExternalStore } from "react";

export type ComposerHeight = "compact" | "expanded";

export const DEFAULT_COMPOSER_HEIGHT: ComposerHeight = "compact";

/** A paste with at least this many lines, or characters, maximizes an empty message box. */
export const PASTE_EXPANDS_COMPOSER = { lines: 15, characters: 1500 } as const;

const expandedScopes = new Set<string>();
const listeners = new Set<() => void>();
let composerCount = 0;

/** A scope of its own, for a composer that isn't tied to a session. */
export function newComposerHeightScope(): string {
  composerCount += 1;
  return `composer:${composerCount}`;
}

/** The height of the composer for `scope`; compact until it's expanded. */
export function composerHeightOf(scope: string): ComposerHeight {
  return expandedScopes.has(scope) ? "expanded" : DEFAULT_COMPOSER_HEIGHT;
}

export function setComposerHeight(scope: string, height: ComposerHeight): void {
  if (composerHeightOf(scope) === height) return;
  if (height === "expanded") expandedScopes.add(scope);
  else expandedScopes.delete(scope);
  for (const listener of listeners) listener();
}

/** Switch the composer for `scope` between compact and expanded. */
export function toggleComposerHeight(scope: string): void {
  setComposerHeight(scope, composerHeightOf(scope) === "expanded" ? "compact" : "expanded");
}

/** Whether pasting `text` into an empty message box should maximize it. */
export function pasteExpandsComposer(text: string): boolean {
  if (text.length >= PASTE_EXPANDS_COMPOSER.characters) return true;
  return text.split("\n", PASTE_EXPANDS_COMPOSER.lines).length >= PASTE_EXPANDS_COMPOSER.lines;
}

/** Hear every composer's height change. */
export function subscribeComposerHeights(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The live height of the composer for `scope`. */
export function useComposerHeight(scope: string): ComposerHeight {
  return useSyncExternalStore(
    subscribeComposerHeights,
    () => composerHeightOf(scope),
    () => DEFAULT_COMPOSER_HEIGHT,
  );
}

/**
 * The height scope for a composer showing `sessionKey`'s draft, or its own while it has none,
 * and a way to return it to compact, as sending a message does.
 */
export function useComposerHeightScope(sessionKey: string | null | undefined): {
  scope: string;
  collapse: () => void;
} {
  const [ownScope] = useState(newComposerHeightScope);
  const scope = sessionKey ? `session:${sessionKey}` : ownScope;
  const collapse = useCallback(() => setComposerHeight(scope, "compact"), [scope]);
  return { scope, collapse };
}

/** Forget every composer's height, for tests. */
export function resetComposerHeightsForTests(): void {
  expandedScopes.clear();
  for (const listener of listeners) listener();
}
