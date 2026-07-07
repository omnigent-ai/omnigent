// Shared composer glue for dictation transcripts.
//
// The mic button emits two kinds of text (see ComposerMicButton):
//   - final utterances (onTranscript) — append permanently, and
//   - interim partials (onInterim, server dictation only) — a revisable
//     trailing region that forms live while the user speaks and is
//     rewritten on every update until an utterance finalizes.
//
// Both composers (ChatPage's Composer and NewChatDialog) hold their draft in
// a plain useState string, so this hook implements the replaceable region as
// a pure value transform: it tracks how many characters of the current value
// belong to the pending interim and slices them off before applying the next
// update. Typing while an interim is pending is the one hazard (the slice
// could eat typed characters); dictation is a hands-off flow, so the trade
// is acceptable and matches what the take will overwrite anyway.

import { useCallback, useRef } from "react";

type SetDraft = (updater: (prev: string) => string) => void;

/** Separator so dictated text never fuses with existing draft words. */
function joined(base: string, text: string): string {
  if (!text) return base;
  if (!base || base.endsWith(" ") || base.endsWith("\n")) return base + text;
  return `${base} ${text}`;
}

// Math.max guards the case where the draft shrank underneath a pending
// interim (send cleared it, user deleted text): drop the stale region
// marker instead of slicing into whatever the draft holds now.
function stripInterim(prev: string, interimLen: number): string {
  return prev.slice(0, Math.max(0, prev.length - interimLen));
}

export function useDictationInsert(setDraft: SetDraft): {
  /** Append a final utterance, clearing any pending interim region. */
  appendFinal: (text: string) => void;
  /** Replace the pending interim region ("" clears it). */
  replaceInterim: (text: string) => void;
} {
  // Length of the interim region currently sitting at the end of the
  // draft (including the separator we added), 0 when none is pending.
  const interimLenRef = useRef(0);

  const replaceInterim = useCallback(
    (text: string) => {
      setDraft((prev) => {
        const base = stripInterim(prev, interimLenRef.current);
        const next = joined(base, text);
        interimLenRef.current = next.length - base.length;
        return next;
      });
    },
    [setDraft],
  );

  const appendFinal = useCallback(
    (text: string) => {
      setDraft((prev) => {
        const base = stripInterim(prev, interimLenRef.current);
        interimLenRef.current = 0;
        return joined(base, text);
      });
    },
    [setDraft],
  );

  return { appendFinal, replaceInterim };
}
