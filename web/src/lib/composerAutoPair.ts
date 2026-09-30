/**
 * Auto-paired inline markers, code-editor style. Typing an opener (`` ` ``,
 * `*`, `_`) inserts its closer after the caret, so the span is real, closed
 * Markdown while it's typed; typing the marker again at that closer steps over
 * it. The draft only ever holds what the user typed plus the closers inserted
 * here, and each edit is applied as one undoable change. Pure functions of the
 * draft, the caret and the closers the composer inserted (`AutoPair`).
 */
import {
  MD_BOLD,
  MD_CODE,
  MD_FENCE,
  MD_MARKER,
  tokenizeComposerMarkdown,
  type ComposerMarkdown,
} from "./composerMarkdown";
import {
  applyEdit,
  codeFenceAfterTyping,
  tidyEmphasisAfterTyping,
  type ComposerEdit,
} from "./composerEditing";

export type PairChar = "*" | "_" | "`";

/** An inserted closer and the opener it closes: `size` copies of `char` at each. */
export interface AutoPair {
  char: PairChar;
  /** The opener's start; its text starts `size` later. */
  open: number;
  /** The closer's start: where the span's text ends. */
  close: number;
  size: number;
}

/** An edit and the closers tracked after it. */
export interface PairEdit {
  edit: ComposerEdit;
  pairs: AutoPair[];
  /** Markers to drop if typed next, right there (the rest of a stepped-over closer). */
  swallow?: Swallow | null;
}

/** `count` more of `char`, typed right at `at`, are dropped: they'd repeat a closer. */
export interface Swallow {
  at: number;
  char: PairChar;
  count: number;
}

/** The longest run a pair grows to: `***` bold italic, `__` bold, ``` `` ``` code. */
const MAX_SIZE: Record<PairChar, number> = { "*": 3, _: 2, "`": 2 };
const OPENING = new Set(["(", "[", "{", '"', "'", "“", "‘", "«"]);
const CLOSING = new Set([")", "]", "}", '"', "'", "”", "’", "»", ".", ",", ";", ":", "!", "?"]);
const SPACE_RE = /\s/u;
const WORD_RE = /[\p{L}\p{N}_]/u;
/** An otherwise empty line, containers aside: `` ` `` there may start a ``` fence. */
const EMPTY_LINE_START_RE = /^(?:[ \t]*(?:>|[-+*](?=[ \t])|\d{1,9}[.)](?=[ \t])))*[ \t]*$/;

export const isPairChar = (char: string | null | undefined): char is PairChar =>
  char === "*" || char === "_" || char === "`";

/** The pairs still in the draft: opener and closer runs where they were left. */
function live(text: string, pairs: readonly AutoPair[]): AutoPair[] {
  return pairs.filter(
    (pair) =>
      text.slice(pair.open, pair.open + pair.size) === pair.char.repeat(pair.size) &&
      text.slice(pair.close, pair.close + pair.size) === pair.char.repeat(pair.size),
  );
}

/**
 * The pairs after `[from, to)` of the draft became `length` characters: an
 * edit before a pair moves it, one inside its text moves its closer, and one
 * touching its markers ends it.
 */
export function shiftPairs(
  pairs: readonly AutoPair[],
  from: number,
  to: number,
  length: number,
): AutoPair[] {
  const delta = length - (to - from);
  const shifted: AutoPair[] = [];
  for (const pair of pairs) {
    if (to <= pair.open)
      shifted.push({ ...pair, open: pair.open + delta, close: pair.close + delta });
    else if (from >= pair.open + pair.size && to <= pair.close)
      shifted.push({ ...pair, close: pair.close + delta });
    else if (from >= pair.close + pair.size) shifted.push(pair);
  }
  return shifted;
}

/** The pairs whose text holds the caret; a caret moved out leaves its closer untracked. */
export function pairsAround(pairs: readonly AutoPair[], caret: number): AutoPair[] {
  return pairs.filter((pair) => caret >= pair.open + pair.size && caret <= pair.close);
}

/**
 * After a typed pair marker (at `caret - 1`, the caret collapsed after it):
 * - inside an empty inserted pair, it grows the pair (`*|*` to `**|**`);
 * - right before an inserted closer, it steps over the whole closer, so one
 *   `*` ends `**bold|**` (the tidy then trims stray spaces inside);
 * - where it can open a span, it inserts the closer (`*` to `*|*`).
 * An opener must follow a line start, whitespace or opening punctuation, and
 * precede whitespace, a line end or closing punctuation, outside code. A
 * backtick on an otherwise empty line isn't paired, so ``` still makes a fence.
 */
export function pairAfterTyping(
  text: string,
  caret: number,
  tracked: readonly AutoPair[],
): PairEdit | null {
  const char = text[caret - 1];
  if (!isPairChar(char)) return null;
  const pairs = live(text, tracked);
  const at = pairs.find((pair) => pair.char === char && pair.close === caret);
  if (at) {
    const others = pairs.filter((pair) => pair !== at);
    if (at.open + at.size === caret - 1 && at.size < MAX_SIZE[char])
      return {
        edit: { start: caret, end: caret, insert: char, caret },
        pairs: [...others, { ...at, size: at.size + 1 }],
      };
    const over: ComposerEdit = {
      start: caret - 1,
      end: caret,
      insert: "",
      caret: caret - 1 + at.size,
    };
    const stepped = applyEdit(text, over);
    const tidy = char === "`" ? null : tidyEmphasisAfterTyping(stepped, over.caret);
    const edit = tidy ? composeEdits(text, over, tidy) : over;
    // Whoever types the whole closer (`**`) gets its rest dropped, not doubled.
    const swallow = at.size > 1 ? { at: edit.caret, char, count: at.size - 1 } : null;
    return { edit, pairs: others, swallow };
  }
  const lineStart = text.lastIndexOf("\n", caret - 2) + 1;
  const before = caret - 2 >= lineStart ? text[caret - 2] : "\n";
  const after = text[caret] ?? "\n";
  if (!(before === "\n" || SPACE_RE.test(before) || OPENING.has(before))) return null;
  if (!(after === "\n" || SPACE_RE.test(after) || CLOSING.has(after))) return null;
  if (char === "`") {
    const newline = text.indexOf("\n", caret);
    const rest = text.slice(caret, newline === -1 ? text.length : newline);
    if (EMPTY_LINE_START_RE.test(text.slice(lineStart, caret - 1)) && rest === "") return null;
  }
  // Typed inside code (or a fence), a marker is code text; after an open run
  // of it in the paragraph, it's likely that run's closer (the tidy and the
  // `**` completion handle it).
  const untyped = text.slice(0, caret - 1) + text.slice(caret);
  const markdown = tokenizeComposerMarkdown(untyped);
  if (flagsAt(markdown, caret - 1) & (MD_CODE | MD_FENCE)) return null;
  if (hasOpenRun(untyped, markdown, caret - 1, char)) return null;
  return {
    edit: { start: caret, end: caret, insert: char, caret },
    pairs: [
      ...shiftPairs(pairs, caret, caret, 1),
      { char, open: caret - 1, close: caret, size: 1 },
    ],
  };
}

/**
 * A space typed right after an inserted opener, in an empty pair, means it
 * wasn't one (a `* bullet`, `2 * 3`, `2 ** 3`): its closer goes. Backticks
 * keep theirs: a code span may start with a space.
 */
export function spaceAfterOpener(
  text: string,
  caret: number,
  tracked: readonly AutoPair[],
): PairEdit | null {
  if (text[caret - 1] !== " ") return null;
  const pairs = live(text, tracked);
  const at = pairs.find(
    (pair) => pair.char !== "`" && pair.open + pair.size === caret - 1 && pair.close === caret,
  );
  if (!at) return null;
  return {
    edit: { start: caret, end: caret + at.size, insert: "", caret },
    pairs: pairs.filter((pair) => pair !== at),
  };
}

/** Backspace in an empty inserted pair takes a marker from each side (`**|**` to `*|*`). */
export function backspaceInPair(
  text: string,
  caret: number,
  tracked: readonly AutoPair[],
): PairEdit | null {
  const pairs = live(text, tracked);
  const at = pairs.find((pair) => pair.open + pair.size === caret && pair.close === caret);
  if (!at) return null;
  const others = pairs.filter((pair) => pair !== at);
  return {
    edit: { start: caret - 1, end: caret + 1, insert: "", caret: caret - 1 },
    pairs: at.size > 1 ? [...others, { ...at, size: at.size - 1, close: caret - 1 }] : others,
  };
}

/**
 * A single `*` typed after text that an open `**` precedes (`**text*`), which
 * would otherwise close it as italic, completes the bold (`**text**`), with
 * the tidy's trims and its exclusions (code, escapes, arithmetic); only when
 * the result really is bold.
 */
export function completeBoldAfterTyping(text: string, caret: number): ComposerEdit | null {
  if (text[caret - 1] !== "*" || text[caret - 2] === "*" || text[caret] === "*") return null;
  const before = text[caret - 2];
  if (before === undefined || before === "\n" || before === "\\") return null;
  if (flagsAt(tokenizeComposerMarkdown(text), caret - 1) & (MD_CODE | MD_FENCE)) return null;
  const add: ComposerEdit = { start: caret, end: caret, insert: "*", caret: caret + 1 };
  const candidate = applyEdit(text, add);
  if (isBoldCloser(candidate, caret - 1)) {
    // `2 **3*`: arithmetic between numbers, not emphasis.
    const opener = candidate.lastIndexOf("**", caret - 3);
    const operand = /\S(?=[ \t]*$)/.exec(candidate.slice(0, opener))?.[0];
    if (operand && /[0-9]/.test(operand) && /[0-9]/.test(candidate[opener + 2] ?? "")) return null;
    return add;
  }
  const tidy = tidyEmphasisAfterTyping(candidate, caret + 1);
  return tidy ? composeEdits(text, add, tidy) : null;
}

/**
 * A marker typed over a selection on one line, outside code and with no
 * space at its ends, wraps it (`*` over `word` makes `*word*`), keeping it
 * selected so another `*` makes it bold. Null for anything else: typing then
 * replaces the selection as usual. `_` wraps only whole words.
 */
export function wrapSelection(
  text: string,
  start: number,
  end: number,
  char: PairChar,
): (ComposerEdit & { selection: [number, number] }) | null {
  const selected = text.slice(start, end);
  if (selected === "" || selected.includes("\n") || selected.trim() !== selected) return null;
  if (char === "_" && (WORD_RE.test(text[start - 1] ?? "") || WORD_RE.test(text[end] ?? "")))
    return null;
  const markdown = tokenizeComposerMarkdown(text);
  if ((flagsAt(markdown, start) | flagsAt(markdown, end - 1)) & (MD_CODE | MD_FENCE)) return null;
  return {
    start,
    end,
    insert: `${char}${selected}${char}`,
    caret: end + 2,
    selection: [start + 1, end + 1],
  };
}

/**
 * Whether the paragraph before `end` holds an unpaired run of `char` that
 * could open a span, forgivingly (the tidy accepts `** text`): not escaped or
 * in code, with text after it, and not a bullet (`* item`), arithmetic
 * (`2 * 3`) or inside a word (`snake_case`).
 */
function hasOpenRun(
  text: string,
  markdown: ComposerMarkdown,
  end: number,
  char: PairChar,
): boolean {
  let start = 0;
  const blank = /\n[ \t]*\n/g;
  for (let match = blank.exec(text); match && match.index < end; match = blank.exec(text))
    start = match.index + match[0].length;
  for (let i = start; i < end; i++) {
    if (text[i] !== char) continue;
    let runEnd = i;
    while (runEnd < end && text[runEnd] === char) runEnd++;
    const lineStart = text.lastIndexOf("\n", i - 1) + 1;
    const lead = text.slice(lineStart, i);
    const spaced = SPACE_RE.test(text[runEnd] ?? "\n");
    const operand = /\S(?=[ \t]*$)/.exec(lead)?.[0] ?? "";
    const opens =
      runEnd < end &&
      text.slice(runEnd, end).trim() !== "" &&
      !/(^|[^\\])(\\\\)*\\$/.test(text.slice(start, i)) &&
      !(flagsAt(markdown, i) & (MD_CODE | MD_FENCE | MD_MARKER)) &&
      !(spaced && runEnd - i === 1 && EMPTY_LINE_START_RE.test(lead)) &&
      !(spaced && /[0-9]/.test(operand)) &&
      (char !== "_" || !WORD_RE.test(text[i - 1] ?? ""));
    if (opens) return true;
    i = runEnd - 1;
  }
  return false;
}

/** Whether `**` at `closer` in `text` closes real bold. */
function isBoldCloser(text: string, closer: number): boolean {
  const markdown = tokenizeComposerMarkdown(text);
  return (
    (flagsAt(markdown, closer) & MD_MARKER) !== 0 &&
    (flagsAt(markdown, closer + 1) & MD_MARKER) !== 0 &&
    (flagsAt(markdown, closer - 1) & MD_BOLD) !== 0
  );
}

/** One edit doing `first` then `second` (made on `first`'s result). */
export function composeEdits(
  text: string,
  first: ComposerEdit,
  second: ComposerEdit,
): ComposerEdit {
  const result = applyEdit(applyEdit(text, first), second);
  let start = 0;
  while (start < text.length && start < result.length && text[start] === result[start]) start++;
  let tail = 0;
  while (
    tail < text.length - start &&
    tail < result.length - start &&
    text[text.length - 1 - tail] === result[result.length - 1 - tail]
  )
    tail++;
  return {
    start,
    end: text.length - tail,
    insert: result.slice(start, result.length - tail),
    caret: second.caret,
  };
}

function flagsAt(markdown: ComposerMarkdown, offset: number): number {
  for (const block of markdown.blocks) {
    if (offset < block.start || offset >= block.start + block.text.length) continue;
    const at = offset - block.start;
    for (const segment of block.segments)
      if (at >= segment.start && at < segment.end) return segment.flags;
  }
  return 0;
}

/** The composer's pairing state: the closers it inserted, and a completion's spot. */
export interface PairState {
  pairs: AutoPair[];
  /** Where a completed closer ends: the rest of it, typed right there, is dropped. */
  swallow: Swallow | null;
}

export const NO_PAIRS: PairState = { pairs: [], swallow: null };

/**
 * Before a keystroke lands: the rest of a closer typed right after it's in
 * place does nothing (edit null); Backspace in an empty inserted pair, or a
 * marker typed over a simple selection, is replaced by one edit (with the
 * selection to set after it). Null lets the keystroke happen.
 */
export function editBeforeKeystroke(
  text: string,
  start: number,
  end: number,
  inputType: string,
  data: string | null,
  state: PairState,
): { edit: ComposerEdit | null; selection?: [number, number]; state: PairState } | null {
  const { swallow } = state;
  if (
    swallow &&
    inputType === "insertText" &&
    data === swallow.char &&
    start === end &&
    start === swallow.at
  ) {
    // The rest of a closer already in place: the key does nothing, so there's nothing to undo.
    const left = swallow.count > 1 ? { ...swallow, count: swallow.count - 1 } : null;
    return { edit: null, state: { pairs: state.pairs, swallow: left } };
  }
  if (inputType === "deleteContentBackward" && start === end) {
    const result = backspaceInPair(text, start, state.pairs);
    return result && { edit: result.edit, state: { pairs: result.pairs, swallow: null } };
  }
  if (inputType === "insertText" && start !== end && isPairChar(data)) {
    const wrap = wrapSelection(text, start, end, data);
    return wrap && { edit: wrap, selection: wrap.selection, state: NO_PAIRS };
  }
  return null;
}

/**
 * The tracked closers after a keystroke of `inputType` turned `before` (with
 * its selection) into `after` (caret at `caret`): typing and deleting move
 * them; anything else (paste, undo, a line break) ends tracking, including a
 * line break inserted as text (Alt+Enter, a code block's Enter).
 */
export function followKeystroke(
  state: PairState,
  inputType: string,
  before: { value: string; start: number; end: number },
  after: string,
  caret: number,
): PairState {
  const inserted = caret - before.start;
  if (
    inputType === "insertText" &&
    before.value.length - (before.end - before.start) + inserted === after.length &&
    !after.slice(before.start, caret).includes("\n")
  )
    return {
      pairs: shiftPairs(state.pairs, before.start, before.end, inserted),
      swallow: state.swallow,
    };
  const removed = before.value.length - after.length;
  if (inputType === "deleteContentBackward" || inputType === "deleteContentForward")
    return { pairs: shiftPairs(state.pairs, caret, caret + removed, 0), swallow: null };
  return NO_PAIRS;
}

/**
 * After a typed character landed (at `caret - 1`): the edit to make, if any,
 * and the state after it. In order: the rest of a closer is dropped (when no
 * `beforeinput` dropped it first); a space may undo an empty pair; a backtick
 * may make a code block;
 * a marker may grow, step over or open a pair; a single `*` may complete an
 * open `**`; and a closing marker may tidy its pair.
 */
export function editAfterKeystroke(
  text: string,
  caret: number,
  typed: string,
  state: PairState,
): { edit: ComposerEdit | null; state: PairState } {
  const { pairs, swallow } = state;
  // The rest of a closer that landed anyway (no `beforeinput` dropped it).
  if (swallow && typed === swallow.char && swallow.at === caret - 1) {
    const left = swallow.count > 1 ? { ...swallow, count: swallow.count - 1 } : null;
    return {
      edit: { start: caret - 1, end: caret, insert: "", caret: caret - 1 },
      state: { pairs, swallow: left },
    };
  }
  const rest: PairState = { pairs, swallow: null };
  if (typed === " ") {
    const result = spaceAfterOpener(text, caret, pairs);
    return result
      ? { edit: result.edit, state: { pairs: result.pairs, swallow: null } }
      : { edit: null, state: rest };
  }
  if (!isPairChar(typed)) return { edit: null, state: rest };
  const fence = typed === "`" ? codeFenceAfterTyping(text, caret) : null;
  if (fence) return { edit: fence, state: NO_PAIRS };
  const paired = pairAfterTyping(text, caret, pairs);
  if (paired)
    return { edit: paired.edit, state: { pairs: paired.pairs, swallow: paired.swallow ?? null } };
  const complete = typed === "*" ? completeBoldAfterTyping(text, caret) : null;
  if (complete)
    return {
      edit: complete,
      state: { pairs: [], swallow: { at: complete.caret, char: "*", count: 1 } },
    };
  const tidy = typed === "`" ? null : tidyEmphasisAfterTyping(text, caret);
  return tidy ? { edit: tidy, state: NO_PAIRS } : { edit: null, state: rest };
}
