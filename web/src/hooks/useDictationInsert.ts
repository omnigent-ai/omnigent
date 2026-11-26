// Shared composer glue for dictation transcripts.
//
// The mic button emits two kinds of text (see ComposerMicButton):
//   - final utterances (onTranscript), pinned permanently, and
//   - interim partials (onInterim, server dictation only), a revisable
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
// measured in, so they live together in one "produced" record that is dropped
// the moment a draft arrives that the hook didn't write. Ownership is tracked
// rather than re-derived by comparing text: an edit away and back (an undo, or
// the same string retyped) restores equality, and acting on that would slice a
// spent interim span out of characters that by then belong to the user.
// Dictation must never delete or displace text it didn't write.

import { type RefObject, useCallback, useLayoutEffect, useRef, useState } from "react";
import {
  isComposerTargetFocused,
  type ComposerTextTarget,
} from "@/components/composer/engine/composerInput";

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

/** What follows the insertion point when no separating space belongs after the
 *  dictated text: whitespace, or punctuation that hugs the word before it.
 *  Quotes are deliberately absent. They are ambiguous (`"` opens as often as it
 *  closes) and guessing wrong fuses words, so they get a space like any other
 *  character. */
const HUGS_LEFT = /^[\s,.;:!?)\]}>]/;

/** What precedes the insertion point when no separating space belongs before
 *  the dictated text: start of draft, whitespace, or an opening delimiter the
 *  text should sit flush against. */
const HUGS_RIGHT = /[\s([{<]$/;

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
  const lead = text && before && !HUGS_RIGHT.test(before) ? " " : "";
  const trail = text && after && !HUGS_LEFT.test(after) ? " " : "";
  const body = lead + text + trail;
  return {
    next: before + body + after,
    region: { start, text: body },
    caret: start + lead.length + text.length,
  };
}

/**
 * Where *region* of *requested* lies in *written*, found from their longest common prefix and
 * suffix: null when the span between them reaches into the region, which leaves its bounds unknown.
 */
function regionAfterEdit(
  requested: string,
  written: string,
  region: { start: number; text: string },
): [number, number] | null {
  const shortest = Math.min(requested.length, written.length);
  let prefix = 0;
  while (prefix < shortest && requested[prefix] === written[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < shortest - prefix && requested.at(-1 - suffix) === written.at(-1 - suffix))
    suffix += 1;
  const end = region.start + region.text.length;
  if (prefix >= end) return [region.start, end];
  const shift = written.length - requested.length;
  if (requested.length - suffix <= region.start) return [region.start + shift, end + shift];
  return null;
}

export function useDictationInsert(
  draft: string,
  /**
   * Writes `next`, which is `draft` with `change` made; `final` when it pins an utterance. A
   * setter that writes through the input returns the text it then holds, as an editor may adjust it.
   */
  setDraft: (
    next: string,
    change: { from: number; to: number; insert: string },
    final: boolean,
  ) => string | void,
  textareaRef?: RefObject<ComposerTextTarget | null>,
  /** The input's text now, or null: an editor the setter writes through holds edits not rendered yet. */
  readDraft?: () => string | null,
): {
  /** Pin a final utterance, replacing any pending interim region. */
  appendFinal: (text: string) => void;
  /** Replace the pending interim region ("" clears it). */
  replaceInterim: (text: string) => void;
  /** Report that the composer has been focused, so its caret is real. */
  noteFocus: () => void;
  /** End a take's hold on the draft: a partial left in it becomes text no later take replaces. */
  release: () => void;
  /** A streaming partial is in the draft, so the composer must not swap its input now. */
  hasInterim: boolean;
} {
  // The draft as last seen. Refreshed on every render so external changes
  // win, and written on insert so a second transcript in the same batch
  // doesn't read a pre-render value.
  const draftRef = useRef(draft);
  // What this hook last wrote, or null when it doesn't own the draft. Ownership
  // is tracked rather than inferred by comparing text: an edit away and back
  // (an undo, or the same string retyped) restores equality, and trusting that
  // would resurrect a spent region whose characters are by then the user's own
  // and slice them out of the middle of the draft.
  const producedRef = useRef<Produced | null>(null);
  if (draftRef.current !== draft) {
    // The draft changed under us. Released here rather than in an effect so a
    // transcript arriving before effects flush can't act on a dead region.
    if (producedRef.current?.value !== draft) producedRef.current = null;
    draftRef.current = draft;
  }
  // Whether the textarea has EVER held focus. Until it has, its selectionStart
  // is 0 by default rather than a caret the user placed and can see. Never
  // reset on blur, deliberately: clicking the mic button blurs the composer,
  // and the caret the user left behind is still the one they can see and mean.
  const focusedRef = useRef(false);
  // Caret this hook has asked for but not yet applied, or null when none is
  // outstanding. Non-null means the DOM caret is stale.
  const wantCaretRef = useRef<number | null>(null);
  // Whether a streaming partial is in the draft, as state so a change always re-renders.
  const [interimPending, setInterimPending] = useState(false);

  // Applied in a layout effect because the draft is React state: setting the
  // value moves the caret to the end, and the value to select into only
  // exists in the DOM after the render that carries it.
  useLayoutEffect(() => {
    const position = wantCaretRef.current;
    if (position === null) return;
    wantCaretRef.current = null;
    const ta = textareaRef?.current;
    // A caret asked for in a draft edited since this hook's write would land inside that edit.
    if (!ta || producedRef.current === null) return;
    // Deliberately does not focus: a take is usually driven from the mic
    // button, and stealing focus drops its Enter-commit / Esc-cancel keys.
    // Some browsers scroll an unfocused element to reveal the new selection,
    // so the scroll position is restored around the write; a dictating user
    // watching one part of a long draft shouldn't get yanked elsewhere.
    const { scrollTop, scrollLeft } = ta;
    ta.setSelectionRange(position, position);
    if (!isComposerTargetFocused(ta)) {
      ta.scrollTop = scrollTop;
      ta.scrollLeft = scrollLeft;
    }
  });

  const insert = useCallback(
    (text: string, pin: boolean) => {
      // An edit can reach the input before its render reaches here. It isn't this hook's, so
      // the hook gives up the draft, as a render would, and builds on the text the input holds.
      const held = readDraft?.() ?? null;
      if (held !== null && held !== draftRef.current) {
        producedRef.current = null;
        wantCaretRef.current = null;
        draftRef.current = held;
      }
      const current = draftRef.current;
      const mine = producedRef.current;
      const region = mine?.region ?? null;
      // Lift a pending interim out so this update replaces it.
      const base = region
        ? current.slice(0, region.start) + current.slice(region.start + region.text.length)
        : current;
      const ta = textareaRef?.current;
      // The live caret, or null while the field has never been focused (where
      // selectionStart's default 0 is not a caret the user placed).
      const live = focusedRef.current && ta ? ta.selectionStart : null;
      // Where this insert goes, most specific first: a pending interim keeps
      // its start so partials revise in place; then our own tail while a caret
      // write is still outstanding (several transcripts can land in one React
      // batch, and until it flushes the DOM caret is stale, so the tail is the
      // only offset that advances per utterance); then the user's caret; then
      // the end of the draft, dictation's pre-positional behavior.
      const stale = mine !== null && wantCaretRef.current !== null;
      const at = region?.start ?? (stale ? mine.tail : (live ?? base.length));
      const spliced = splice(base, at, text);

      const settled: Produced = {
        value: spliced.next,
        region: pin || !text ? null : spliced.region,
        tail: spliced.caret,
      };

      // The draft already reads exactly like this. Two ways to get here: the
      // empty-interim clear the mic emits at the end of a take, and a final
      // whose text matches the partial already on screen. Ownership still has
      // to settle (a final must pin, or the region would stay pending and the
      // next insert would lift the finalized text back out and delete it), but
      // no caret may be requested: setDraft would be a same-value update, which
      // React can bail out of without committing, so the layout effect would
      // never run to clear the request and every later utterance would read the
      // DOM caret as stale, pinning inserts to the tail and ignoring the user.
      if (spliced.next === current) {
        producedRef.current = mine === null ? null : settled;
        // No draft render follows, so the pinned region must re-render on its own.
        setInterimPending((producedRef.current?.region ?? null) !== null);
        return;
      }

      const change = {
        from: spliced.region.start,
        to: spliced.region.start + (region?.text.length ?? 0),
        insert: spliced.region.text,
      };
      const written = setDraft(spliced.next, change, pin) ?? spliced.next;
      draftRef.current = written;
      // A refused write changed nothing, so what this hook owned still stands.
      if (written === current) return;
      // An editor may adjust a write, as it keeps a card's line its own. What it wrote between
      // the text on either side is then the region.
      const head = base.slice(0, spliced.region.start);
      const rest = base.slice(spliced.region.start);
      let from = head.length;
      let end = written.length - rest.length;
      if (end < from || !written.startsWith(head) || !written.endsWith(rest)) {
        // It changed text beside the region too. A partial keeps a region that change missed;
        // anything else leaves nothing to revise.
        const moved = pin ? null : regionAfterEdit(spliced.next, written, spliced.region);
        if (moved === null) {
          producedRef.current = null;
          wantCaretRef.current = null;
          setInterimPending(false);
          return;
        }
        [from, end] = moved;
      }
      const body = written.slice(from, end);
      // The caret stays as far before the region's end as the splice put it, within the region.
      const gap = spliced.region.start + spliced.region.text.length - spliced.caret;
      const caret = Math.min(Math.max(end - gap, from), end);
      const owned = settled.region && body ? { start: from, text: body } : null;
      producedRef.current = { value: written, region: owned, tail: caret };
      wantCaretRef.current = caret;
      setInterimPending(owned !== null);
    },
    [setDraft, textareaRef, readDraft],
  );

  return {
    appendFinal: useCallback((text: string) => insert(text, true), [insert]),
    replaceInterim: useCallback((text: string) => insert(text, false), [insert]),
    noteFocus: useCallback(() => {
      focusedRef.current = true;
    }, []),
    release: useCallback(() => {
      producedRef.current = null;
      wantCaretRef.current = null;
      setInterimPending(false);
    }, []),
    // A draft the hook didn't write releases its region during render, before state catches up.
    hasInterim: interimPending && (producedRef.current?.region ?? null) !== null,
  };
}
