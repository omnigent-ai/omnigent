// Readline's editing boundaries and kill ring over plain text, independent of any editor.
// Each boundary helper takes the text and a caret offset and returns where the motion or
// kill from that caret ends. Editors map these onto their own transactions.

const WHITESPACE = /\s/u;

function scanBackward(text: string, from: number, matches: (character: string) => boolean) {
  let position = from;
  while (position > 0 && matches(text[position - 1])) position -= 1;
  return position;
}

const isWhitespace = (character: string) => WHITESPACE.test(character);

/** ⌃W (unix-word-rubout): back over whitespace, then to the previous whitespace. */
export function shellWordStartBefore(text: string, offset: number): number {
  const afterSpace = scanBackward(text, offset, isWhitespace);
  return scanBackward(text, afterSpace, (character) => !isWhitespace(character));
}

/** ⌃U: the start of the line, or across the line break when already there. */
export function lineStartBefore(text: string, offset: number): number {
  const lineStart = text.lastIndexOf("\n", offset - 1) + 1;
  return offset > lineStart ? lineStart : Math.max(offset - 1, 0);
}

/** ⌃K: the end of the line, or across the line break when already there. */
export function lineEndAfter(text: string, offset: number): number {
  const newline = text.indexOf("\n", offset);
  const lineEnd = newline < 0 ? text.length : newline;
  return offset < lineEnd ? lineEnd : Math.min(offset + 1, text.length);
}

const MAX_KILL_RING_ENTRIES = 30;

/**
 * Killed text, newest first. Consecutive kills join into one entry, so ⌃W ⌃W then ⌃Y
 * restores both words. `extending` says whether the next kill joins the newest entry.
 */
export interface KillRing {
  readonly entries: readonly string[];
  readonly extending: boolean;
}

export const EMPTY_KILL_RING: KillRing = { entries: [], extending: false };

/** Record a kill; `backward` kills (⌃W, ⌃U) join in front of the newest entry. */
export function pushKill(ring: KillRing, text: string, backward: boolean): KillRing {
  const [newest, ...older] = ring.entries;
  if (ring.extending && newest !== undefined) {
    return { entries: [backward ? text + newest : newest + text, ...older], extending: true };
  }
  return { entries: [text, ...ring.entries].slice(0, MAX_KILL_RING_ENTRIES), extending: true };
}

/** Any edit or caret move that isn't a kill ends the run of joined kills. */
export function endKillRun(ring: KillRing): KillRing {
  return ring.extending ? { entries: ring.entries, extending: false } : ring;
}

/** The text ⌃Y inserts, or null with nothing killed yet. */
export function yankText(ring: KillRing): string | null {
  return ring.entries[0] ?? null;
}
