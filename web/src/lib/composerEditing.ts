/**
 * Edits the composer makes as the user types Markdown: tidying a stray space
 * inside emphasis, turning a typed ``` into a code block, and Enter, Alt+Enter
 * and ArrowDown inside a code block. Each is a pure function of the draft (as
 * the textarea holds it, LF line breaks) and the caret, returning the edit to
 * apply or null.
 * The composer applies an edit as one visible, undoable change; nothing here
 * rewrites what is sent.
 */
import {
  MD_BOLD,
  MD_CODE,
  MD_FENCE,
  MD_ITALIC,
  MD_MARKER,
  tokenizeComposerMarkdown,
  type ComposerMarkdown,
  type MarkdownBlock,
} from "./composerMarkdown";

/** Replace `[start, end)` with `insert`, then put the caret at `caret`. */
export interface ComposerEdit {
  start: number;
  end: number;
  insert: string;
  caret: number;
}

const PUNCTUATION_RE = /[\p{P}\p{S}]/u;
const DIGIT_RE = /[0-9]/;

/**
 * After a typed closing `*`, `**`, `***`, `_`, `__` or `___`: when the pair it
 * closes has stray spaces just inside it (`** Is this bold?!**`, `**bold **`
 * or `** both sides **`), remove them so it becomes the emphasis the user
 * meant. Never for list bullets (in quotes and nested lists too), arithmetic
 * (`2 * 3 * 4`, `2 ** 3 ** 4`, `2 * 3*`), intraword underscores, escaped
 * markers, code, a pair across a blank line or an empty pair; and only when
 * the tidied text really is that emphasis.
 */
export function tidyEmphasisAfterTyping(text: string, caret: number): ComposerEdit | null {
  const char = text[caret - 1];
  if (char !== "*" && char !== "_") return null;
  // The closer is the run ending at the caret, and the caret ends it.
  if (text[caret] === char) return null;
  let closerStart = caret - 1;
  while (closerStart > 0 && text[closerStart - 1] === char) closerStart--;
  const size = caret - closerStart;
  if (size > 3 || isEscaped(text, closerStart)) return null;
  const paragraphStart = paragraphStartBefore(text, closerStart);
  // The nearest earlier run of exactly the same size opens the pair.
  let openerEnd = -1;
  let openerStart = -1;
  for (let i = closerStart - 1; i >= paragraphStart; i--) {
    if (text[i] !== char) continue;
    let start = i;
    while (start > paragraphStart && text[start - 1] === char) start--;
    if (i + 1 - start === size) {
      openerStart = start;
      openerEnd = i + 1;
      break;
    }
    i = start;
  }
  if (openerStart === -1 || isEscaped(text, openerStart)) return null;
  const content = text.slice(openerEnd, closerStart);
  const lead = /^[ \t]*/.exec(content)![0].length;
  const trail = /[ \t]*$/.exec(content)![0].length;
  const inner = content.slice(lead, content.length - trail);
  if (inner === "" || (lead === 0 && trail === 0)) return null;
  // The opener starts a word: at a line start, or after whitespace or punctuation.
  const before = openerStart === 0 ? "\n" : text[openerStart - 1];
  if (before !== "\n" && before !== " " && before !== "\t" && !PUNCTUATION_RE.test(before))
    return null;
  if (before === "\\" || before === char) return null;
  // `* item`, even inside a quote or another list item, is a bullet, not an opener.
  const lineStart = text.lastIndexOf("\n", openerStart - 1) + 1;
  if (size === 1 && isListMarker(text, lineStart, openerStart)) return null;
  // `2 * 3*`: arithmetic between numbers, not emphasis.
  const operand = /\S(?=[ \t]*$)/.exec(text.slice(lineStart, openerStart))?.[0];
  if (operand && DIGIT_RE.test(operand) && DIGIT_RE.test(inner[0])) return null;
  const markdown = tokenizeComposerMarkdown(text);
  if (flagsAt(markdown, closerStart) & (MD_CODE | MD_FENCE)) return null;
  if (flagsAt(markdown, openerStart) & (MD_CODE | MD_FENCE)) return null;

  const edit: ComposerEdit =
    lead > 0
      ? { start: openerEnd, end: caret, insert: inner + char.repeat(size), caret: 0 }
      : { start: closerStart - trail, end: caret, insert: char.repeat(size), caret: 0 };
  edit.caret = edit.start + edit.insert.length;
  // Only tidy into real emphasis: both runs are markers and the text is styled.
  const tidied = applyEdit(text, edit);
  const result = tokenizeComposerMarkdown(tidied);
  const newCloser = edit.caret - size;
  const style = size === 1 ? MD_ITALIC : size === 2 ? MD_BOLD : MD_ITALIC | MD_BOLD;
  if (!(flagsAt(result, openerStart) & MD_MARKER) || !(flagsAt(result, newCloser) & MD_MARKER))
    return null;
  if ((flagsAt(result, openerEnd) & style) !== style) return null;
  return edit;
}

/**
 * After a typed third backtick that makes a line exactly ``` after its
 * containers (quote markers, list items), and opens a new fence there (not
 * closing an open one): the line becomes a code block with the caret on its
 * empty middle line, its lines continuing those containers.
 */
export function codeFenceAfterTyping(text: string, caret: number): ComposerEdit | null {
  if (text[caret - 1] !== "`") return null;
  const lineStart = text.lastIndexOf("\n", caret - 1) + 1;
  const newline = text.indexOf("\n", caret);
  const lineEnd = newline === -1 ? text.length : newline;
  if (lineEnd !== caret) return null;
  const block = blockAt(tokenizeComposerMarkdown(text), lineStart);
  const fence = block?.fence;
  if (!block || !fence?.open || block.start !== lineStart) return null;
  if (text.slice(lineStart + fence.lines[0], lineEnd) !== "```") return null;
  const { prefix } = fence;
  return {
    start: caret,
    end: caret,
    insert: `\n${prefix}\n${prefix}\`\`\``,
    caret: caret + 1 + prefix.length,
  };
}

/** A fenced block around the caret, with its lines and container prefix. */
interface FenceAround {
  open: boolean;
  run: string;
  /** What starts a new line of it: its containers' markers and indents, or "". */
  prefix: string;
  /** Each line's start, content start (after the prefix) and end (before its break). */
  lines: { start: number; content: number; end: number }[];
  /** The closing fence line's index, or null when open. */
  close: number | null;
  /** The index of the line holding the caret. */
  at: number;
}

function fenceAround(text: string, caret: number, markdown: ComposerMarkdown): FenceAround | null {
  const block = blockAt(markdown, caret);
  if (!block?.fence) return null;
  const { fence } = block;
  const end = block.start + block.text.length;
  // The block's own lines: an open block at the draft's end holds the empty
  // line after a final newline; otherwise a trailing break starts the next line.
  const contentEnd =
    fence.open && end === text.length ? end : end - (block.text.endsWith("\n") ? 1 : 0);
  if (caret > contentEnd) return null;
  // Each line's text starts where the parse found its containers' prefix end;
  // an open block's empty line after a final newline has none yet.
  const lines: FenceAround["lines"] = [];
  for (let start = block.start; start <= contentEnd;) {
    const newline = text.indexOf("\n", start);
    const lineEnd = newline === -1 || newline > contentEnd ? contentEnd : newline;
    const content = Math.min(
      block.start + (fence.lines[lines.length] ?? start - block.start),
      lineEnd,
    );
    lines.push({ start, content, end: lineEnd });
    if (lineEnd >= contentEnd) break;
    start = lineEnd + 1;
  }
  const close =
    fence.closeLine === null
      ? null
      : lines.findIndex((line) => line.start === block.start + fence.closeLine!);
  const at = lines.findIndex((line) => caret >= line.start && caret <= line.end);
  if (at === -1) return null;
  return {
    open: fence.open,
    run: fence.run,
    prefix: fence.prefix,
    lines,
    close: close === -1 ? null : close,
    at,
  };
}

/**
 * Enter inside a fenced block, given the draft's current parse. Null outside
 * one (Enter keeps its usual job). Inside, Enter makes a newline (in a quote or
 * list item, one that continues its containers, so the block goes on); on an empty last
 * body line it exits the block instead: the empty line goes, the block closes
 * if it was open, and the caret lands on a new line after the closing fence.
 */
export function fenceEnter(
  text: string,
  selectionStart: number,
  selectionEnd: number,
  markdown: ComposerMarkdown,
): { kind: "newline" } | { kind: "edit"; edit: ComposerEdit } | null {
  const fence = fenceAround(text, selectionStart, markdown);
  if (!fence) return null;
  const newline = () => {
    const edit = continueFence(fence, selectionStart, selectionEnd);
    return edit ? ({ kind: "edit", edit } as const) : ({ kind: "newline" } as const);
  };
  const line = fence.lines[fence.at];
  const lastBody = fence.open ? fence.lines.length - 1 : (fence.close ?? 0) - 1;
  const empty = text.slice(line.content, line.end).trim() === "";
  if (selectionStart !== selectionEnd || fence.at === 0 || fence.at !== lastBody || !empty)
    return newline();
  if (fence.open) {
    const insert = `${fence.prefix}${fence.run}\n${fence.prefix}`;
    return {
      kind: "edit",
      edit: { start: line.start, end: line.end, insert, caret: line.start + insert.length },
    };
  }
  const closing = fence.lines[fence.close!];
  const previous = fence.lines[fence.at - 1];
  return {
    kind: "edit",
    edit: afterClosing(
      text,
      fence,
      previous.end,
      closing,
      `\n${text.slice(closing.start, closing.end)}`,
    ),
  };
}

/**
 * A line break inside a fenced block that keeps its quote or list item going
 * (the block's container prefix after it), for a newline chord that never
 * exits the block. Null outside a block or where a bare line break does.
 */
export function fenceNewline(
  text: string,
  selectionStart: number,
  selectionEnd: number,
  markdown: ComposerMarkdown,
): ComposerEdit | null {
  const fence = fenceAround(text, selectionStart, markdown);
  return fence && continueFence(fence, selectionStart, selectionEnd);
}

function continueFence(
  fence: Pick<FenceAround, "prefix">,
  selectionStart: number,
  selectionEnd: number,
): ComposerEdit | null {
  if (fence.prefix === "") return null;
  return {
    start: selectionStart,
    end: selectionEnd,
    insert: `\n${fence.prefix}`,
    caret: selectionStart + 1 + fence.prefix.length,
  };
}

/**
 * ArrowDown on a fenced block's last body line (or its closing fence line),
 * given the draft's current parse: move past the block, closing it first if
 * it's open and adding a line if it ends the draft. Null elsewhere.
 */
export function fenceArrowDown(
  text: string,
  selectionStart: number,
  selectionEnd: number,
  markdown: ComposerMarkdown,
): { move: number } | { edit: ComposerEdit } | null {
  if (selectionStart !== selectionEnd) return null;
  const fence = fenceAround(text, selectionStart, markdown);
  if (!fence || fence.at === 0) return null;
  const line = fence.lines[fence.at];
  if (fence.open) {
    if (fence.at !== fence.lines.length - 1) return null;
    const closing = `${fence.prefix}${fence.run}\n${fence.prefix}`;
    const empty = text.slice(line.content, line.end).trim() === "";
    const edit = empty
      ? { start: line.start, end: line.end, insert: closing, caret: line.start + closing.length }
      : {
          start: line.end,
          end: line.end,
          insert: `\n${closing}`,
          caret: line.end + 1 + closing.length,
        };
    return { edit };
  }
  if (fence.at !== fence.close && fence.at !== (fence.close ?? 0) - 1) return null;
  const closing = fence.lines[fence.close!];
  if (closing.end < text.length) return { move: closing.end + 1 };
  return {
    edit: {
      start: text.length,
      end: text.length,
      insert: `\n${fence.prefix}`,
      caret: text.length + 1 + fence.prefix.length,
    },
  };
}

/**
 * Replace `[from, closing.end)` with `insert` (which ends with the closing
 * fence line) and land on the line after it: the next line if there is one,
 * else a new one (keeping the quote's markers).
 */
function afterClosing(
  text: string,
  fence: FenceAround,
  from: number,
  closing: { end: number },
  insert: string,
): ComposerEdit {
  if (closing.end < text.length)
    return { start: from, end: closing.end, insert, caret: from + insert.length + 1 };
  const withLine = `${insert}\n${fence.prefix}`;
  return { start: from, end: closing.end, insert: withLine, caret: from + withLine.length };
}

/**
 * Whether the `*` at `at` is a list bullet: followed by a space, and first on
 * its line once the line's containers (quote markers, outer list markers) and
 * their indentation are set aside.
 */
function isListMarker(text: string, lineStart: number, at: number): boolean {
  if (text[at] !== "*" || (text[at + 1] !== " " && text[at + 1] !== "\t")) return false;
  let p = lineStart;
  for (;;) {
    while (p < at && (text[p] === " " || text[p] === "\t")) p++;
    if (p === at) return true;
    const char = text[p];
    if (char === ">") {
      p++;
      continue;
    }
    if ((char === "-" || char === "+" || char === "*") && /[ \t]/.test(text[p + 1] ?? "")) {
      p++;
      continue;
    }
    const ordered = /^\d{1,9}[.)](?=[ \t])/.exec(text.slice(p, p + 11));
    if (ordered) {
      p += ordered[0].length;
      continue;
    }
    return false;
  }
}

export function applyEdit(text: string, edit: ComposerEdit): string {
  return text.slice(0, edit.start) + edit.insert + text.slice(edit.end);
}

function blockAt(markdown: ComposerMarkdown, offset: number): MarkdownBlock | null {
  const { blocks } = markdown;
  let low = 0;
  let high = blocks.length - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (blocks[mid].start <= offset) low = mid;
    else high = mid - 1;
  }
  const block = blocks[low];
  return block && offset >= block.start && offset <= block.start + block.text.length ? block : null;
}

function flagsAt(markdown: ComposerMarkdown, offset: number): number {
  const block = blockAt(markdown, offset);
  if (!block) return 0;
  const at = offset - block.start;
  for (const segment of block.segments)
    if (at >= segment.start && at < segment.end) return segment.flags;
  return 0;
}

function paragraphStartBefore(text: string, offset: number): number {
  const blank = /\n[ \t]*\n/g;
  let start = 0;
  for (let match = blank.exec(text); match && match.index < offset; match = blank.exec(text))
    start = match.index + match[0].length;
  return Math.min(start, offset);
}

function isEscaped(text: string, at: number): boolean {
  let slashes = 0;
  for (let k = at - 1; k >= 0 && text[k] === "\\"; k--) slashes++;
  return slashes % 2 === 1;
}
