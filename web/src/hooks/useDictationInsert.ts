// Shared composer glue for dictation transcripts.
//
// The mic button emits two kinds of text (see ComposerMicButton):
//   - final utterances (onTranscript), pinned permanently, and
//   - interim partials (onInterim, server dictation only) — a revisable
//     region that forms live while the user speaks and is rewritten on
//     every update until an utterance finalizes.
//
// Text lands at the user's caret, not at the end of the draft: paste a block
// of context, click above it, dictate, and the words go where the caret is.
// The caret is read from the textarea itself at insert time: the browser
// keeps selectionStart on the element across blur, so it survives the mic
// button taking focus and needs no mirroring here. The composer only has to
// report that the field has been focused (noteFocus): until then there is no
// caret on screen to insert at, so text goes to the end of the draft, which
// is what dictation always did.
//
// The hook takes the draft as a value rather than reading it inside a setDraft
// updater. Transcripts arrive off a socket, where React batches and DEFERS the
// updater, so anything the updater learned (where the text landed, where the
// caret goes) would be written back too late for the next partial to use, and
// a streaming interim region would append instead of revise. Taking the draft
// as a value keeps every offset computable up front and leaves setDraft a
// plain value assignment, pure by construction under StrictMode.
//
// Offsets this hook derives are only meaningful against the draft they were
// measured in, so they are stored with the exact string it produced and used
// only while the draft is still verbatim that string. A send, an Esc revert,
// or the user editing all fail that check, and the hook falls back to the
// live caret. Dictation must never delete or displace text it didn't write,
// and string identity is what proves nothing else has landed since.

import { type RefObject, useCallback, useLayoutEffect, useRef } from "react";

/** The draft this hook produced, and how to keep building on it.
 *
 *  - ``region`` is the pending interim span (offset + exact text), or null
 *    once an utterance is pinned; a pending span is replaced in place by the
 *    next partial rather than walking down the draft.
 *  - ``tail`` is where that text ended, so a following utterance continues
 *    after it. Preferred over the DOM caret because several transcripts can
 *    land in one batch, before the caret this hook requested has been applied.
 *
 *  Valid only while the live draft still equals ``value``. */
interface Produced {
  value: string;
  region: { start: number; text: string } | null;
  tail: number;
}

/** Leading whitespace, or punctuation that hugs the word before it. No
 *  separating space is wanted when dictated text lands ahead of these. */
const HUGS_LEFT = /^[\s,.;:!?)\]}>'"]/;

/**
 * Splice *text* into *base* at *at*, padded with single spaces so dictated
 * words never fuse with the draft on either side.
 *
 * Returns the new draft, the span inserted (padding included, so removing it
 * restores *base* exactly), and where the caret belongs: directly after the
 * dictated words, ahead of any trailing space.
 */
function splice(
  base: string,
  at: number,
  text: string,
): { next: string; region: { start: number; text: string }; caret: number } {
  const start = Math.min(Math.max(at, 0), base.length);
  const before = base.slice(0, start);
  const after = base.slice(start);
  const lead = text && before && !/\s$/.test(before) ? " " : "";
  const trail = text && after && !HUGS_LEFT.test(after) ? " " : "";
  const body = lead + text + trail;
  return {
    next: before + body + after,
    region: { start, text: body },
    caret: start + lead.length + text.length,
  };
}

export function useDictationInsert(
  draft: string,
  setDraft: (next: string) => void,
  textareaRef?: RefObject<HTMLTextAreaElement | null>,
): {
  /** Pin a final utterance, replacing any pending interim region. */
  appendFinal: (text: string) => void;
  /** Replace the pending interim region ("" clears it). */
  replaceInterim: (text: string) => void;
  /** Report that the composer has been focused, so its caret is real. */
  noteFocus: () => void;
} {
  // The draft as last seen. Refreshed on every render so external changes
  // win, and written on insert so a second transcript in the same batch
  // doesn't read a pre-render value.
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const producedRef = useRef<Produced | null>(null);
  // Whether the textarea has ever held focus. Until it has, its selectionStart
  // is 0 by default rather than a caret the user placed and can see.
  const focusedRef = useRef(false);
  // Caret to apply once React has rendered the spliced draft.
  const wantCaretRef = useRef<number | null>(null);

  // Applied in a layout effect because the draft is React state: setting the
  // value moves the caret to the end, and the value to select into only
  // exists in the DOM after the render that carries it.
  useLayoutEffect(() => {
    const position = wantCaretRef.current;
    if (position === null) return;
    wantCaretRef.current = null;
    // Deliberately does not focus: a take is usually driven from the mic
    // button, and stealing focus drops its Enter-commit / Esc-cancel keys.
    textareaRef?.current?.setSelectionRange(position, position);
  });

  const insert = useCallback(
    (text: string, pin: boolean) => {
      const current = draftRef.current;
      // Our bookkeeping only holds while the draft is verbatim what we wrote.
      const mine = producedRef.current?.value === current ? producedRef.current : null;
      const region = mine?.region ?? null;
      // Lift a pending interim out so this update replaces it.
      const base = region
        ? current.slice(0, region.start) + current.slice(region.start + region.text.length)
        : current;
      const ta = textareaRef?.current;
      // The live caret, or null while the field has never been focused (where
      // selectionStart's default 0 is not a caret the user placed).
      const live = focusedRef.current && ta ? ta.selectionStart : null;
      // While a caret write is still pending, the DOM caret is stale: several
      // transcripts can land in one React batch and the write is a layout
      // effect that runs only after the batch flushes. The remembered tail is
      // then the only offset that advances per utterance. A stale caret and a
      // caret the user just moved are indistinguishable by value, so this
      // tracks the outstanding write rather than comparing offsets.
      const stale = wantCaretRef.current !== null;
      // Where this insert goes, most specific first: a pending interim keeps
      // its start so partials revise in place; then our tail while the caret
      // is stale; then the user's caret; then the end of the draft, which is
      // what dictation did before it was positional.
      const at = region?.start ?? (stale ? (mine?.tail ?? base.length) : (live ?? base.length));
      const spliced = splice(base, at, text);

      draftRef.current = spliced.next;
      producedRef.current = {
        value: spliced.next,
        region: pin || !text ? null : spliced.region,
        tail: spliced.caret,
      };
      wantCaretRef.current = spliced.caret;
      setDraft(spliced.next);
    },
    [setDraft, textareaRef],
  );

  return {
    appendFinal: useCallback((text: string) => insert(text, true), [insert]),
    replaceInterim: useCallback((text: string) => insert(text, false), [insert]),
    noteFocus: useCallback(() => {
      focusedRef.current = true;
    }, []),
  };
}
