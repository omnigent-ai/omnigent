// Quotes a thingy sends into a session's composer, the way Reply and Ask in side chat add a
// selection from the transcript. The session may not be on screen yet (its page is opening), so
// quotes wait in a short-lived queue until that session's composer can take them.
import { useEffect, useSyncExternalStore } from "react";

export interface ThingyComposerQuote {
  sessionId: string;
  kind: "reply" | "side_chat";
  text: string;
  at: number;
}

// A quote waits this long for its session's composer, for example while the session opens.
const MAX_AGE_MS = 60_000;
let queue: ThingyComposerQuote[] = [];
let version = 0;
const listeners = new Set<() => void>();

function fresh(quote: ThingyComposerQuote, now: number): boolean {
  return now - quote.at < MAX_AGE_MS;
}

function notify(): void {
  version += 1;
  for (const listener of listeners) listener();
}

/** Queues a quote for the session's composer; the session's chat adds it once it shows. */
export function queueThingyComposerQuote(
  sessionId: string,
  kind: "reply" | "side_chat",
  text: string,
): void {
  const now = Date.now();
  queue = [...queue.filter((quote) => fresh(quote, now)), { sessionId, kind, text, at: now }];
  notify();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function currentVersion(): number {
  return version;
}

/**
 * Applies the quotes queued for `sessionId`, in order. `apply` returns false when it can't add
 * one yet (the composer isn't ready); that quote and the ones after it stay queued.
 */
export function useThingyComposerQuotes(
  sessionId: string | null,
  apply: (quote: ThingyComposerQuote) => boolean,
): void {
  useSyncExternalStore(subscribe, currentVersion, currentVersion);
  // No dependencies: a quote that couldn't be added is tried again on the next render.
  useEffect(() => {
    if (sessionId === null) return;
    const now = Date.now();
    const applied = new Set<ThingyComposerQuote>();
    for (const quote of queue) {
      if (quote.sessionId !== sessionId || !fresh(quote, now)) continue;
      if (!apply(quote)) break;
      applied.add(quote);
    }
    if (applied.size > 0) queue = queue.filter((quote) => !applied.has(quote));
  });
}

/** Clears the queue; for tests. */
export function resetThingyComposerQuotes(): void {
  queue = [];
  notify();
}
