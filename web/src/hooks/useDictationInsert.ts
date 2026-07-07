// Shared composer glue for dictation transcripts.
//
// The mic button emits two kinds of text (see ComposerMicButton):
//   - final utterances (onTranscript) — append permanently, and
//   - interim partials (onInterim, server dictation only) — a revisable
//     trailing region that forms live while the user speaks and is
//     rewritten on every update until an utterance finalizes.
//
// Both composers (ChatPage's Composer and NewChatDialog) hold their draft in
// a plain useState string, so the revisable region is implemented as a value
// transform: the hook remembers the exact interim text it last inserted and
// strips it only when the draft still ends with it verbatim. If it doesn't —
// the user typed after it, sent the message, or edited the draft — the
// marker is simply dropped and nothing is removed: dictation must never
// delete text it didn't write.
//
// The marker ref is read before and written after each setDraft call, never
// inside the updater — updaters must stay pure because React StrictMode
// double-invokes them.

import { useCallback, useRef } from "react";

type SetDraft = (updater: (prev: string) => string) => void;

/** Separator so dictated text never fuses with existing draft words. */
function joined(base: string, text: string): string {
  if (!text) return base;
  if (!base || base.endsWith(" ") || base.endsWith("\n")) return base + text;
  return `${base} ${text}`;
}

/**
 * Remove the tracked interim region, but only if the draft still ends
 * with it verbatim; also drop the single space separator we added.
 */
function stripMarker(prev: string, marker: string): string {
  if (!marker || !prev.endsWith(marker)) return prev;
  const base = prev.slice(0, prev.length - marker.length);
  return base.endsWith(" ") ? base.slice(0, -1) : base;
}

export function useDictationInsert(setDraft: SetDraft): {
  /** Append a final utterance, replacing any pending interim region. */
  appendFinal: (text: string) => void;
  /** Replace the pending interim region ("" clears it). */
  replaceInterim: (text: string) => void;
} {
  const interimRef = useRef("");

  const replaceInterim = useCallback(
    (text: string) => {
      const marker = interimRef.current;
      interimRef.current = text;
      setDraft((prev) => joined(stripMarker(prev, marker), text));
    },
    [setDraft],
  );

  const appendFinal = useCallback(
    (text: string) => {
      const marker = interimRef.current;
      interimRef.current = "";
      setDraft((prev) => joined(stripMarker(prev, marker), text));
    },
    [setDraft],
  );

  return { appendFinal, replaceInterim };
}
