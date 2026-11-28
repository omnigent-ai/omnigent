// Markdown formatting on a plain textarea: the formats the toolbar, the keys and ⌘K share, the
// text edit each one makes, URL-over-selection links, and edits the browser can undo natively.

import {
  BoldIcon,
  CodeIcon,
  HeadingIcon,
  ItalicIcon,
  LinkIcon,
  ListIcon,
  ListOrderedIcon,
  ListTodoIcon,
  type LucideIcon,
  SquareCodeIcon,
  StrikethroughIcon,
  TextQuoteIcon,
} from "lucide-react";

import { type Chord, chordMatches, type KeyChordInput } from "@/lib/shortcuts/chord";

export type MarkdownFormat =
  | "heading"
  | "bold"
  | "italic"
  | "code"
  | "link"
  | "quote"
  | "unordered-list"
  | "ordered-list"
  | "task-list"
  | "strikethrough"
  | "code-block";

/** A textarea's text and selection, before or after an edit. */
export interface MarkdownEdit {
  value: string;
  selectionStart: number;
  selectionEnd: number;
}

export interface MarkdownFormatCommand {
  format: MarkdownFormat;
  label: string;
  /** The ⌘K row's icon; the toolbar draws its own glyphs. */
  icon: LucideIcon;
  keywords: string[];
  chord?: Chord;
  /** Shown in the toolbar; the rest are ⌘K rows and chords only. */
  toolbar: boolean;
}

const mod = (code: string, shift = false): Chord => ({ mod: true, shift, code });

/** Every format, once: the toolbar, the editor's keys and the ⌘K rows all read this table. */
export const MARKDOWN_FORMATS: readonly MarkdownFormatCommand[] = [
  { format: "heading", label: "Heading", icon: HeadingIcon, keywords: ["title"], toolbar: true },
  {
    format: "bold",
    label: "Bold",
    icon: BoldIcon,
    keywords: ["strong"],
    chord: mod("KeyB"),
    toolbar: true,
  },
  {
    format: "italic",
    label: "Italic",
    icon: ItalicIcon,
    keywords: ["emphasis"],
    chord: mod("KeyI"),
    toolbar: true,
  },
  {
    format: "code",
    label: "Inline Code",
    icon: CodeIcon,
    keywords: ["monospace"],
    chord: mod("KeyE"),
    toolbar: true,
  },
  // No chord: ⌘K opens the command palette.
  { format: "link", label: "Link", icon: LinkIcon, keywords: ["url", "hyperlink"], toolbar: true },
  {
    format: "quote",
    label: "Quote",
    icon: TextQuoteIcon,
    keywords: ["blockquote"],
    chord: mod("Period", true),
    toolbar: true,
  },
  {
    format: "unordered-list",
    label: "Bulleted List",
    icon: ListIcon,
    keywords: ["unordered", "bullets"],
    chord: mod("Digit8", true),
    toolbar: true,
  },
  {
    format: "ordered-list",
    label: "Numbered List",
    icon: ListOrderedIcon,
    keywords: ["ordered", "numbers"],
    chord: mod("Digit7", true),
    toolbar: true,
  },
  {
    format: "task-list",
    label: "Task List",
    icon: ListTodoIcon,
    keywords: ["checklist", "checkbox", "todo"],
    toolbar: true,
  },
  {
    format: "strikethrough",
    label: "Strikethrough",
    icon: StrikethroughIcon,
    keywords: ["strike"],
    chord: mod("KeyX", true),
    toolbar: false,
  },
  {
    format: "code-block",
    label: "Code Block",
    icon: SquareCodeIcon,
    keywords: ["fence", "snippet"],
    toolbar: false,
  },
];

/** Switches between Write and Preview. */
export const TOGGLE_PREVIEW_CHORD: Chord = mod("KeyP", true);

export type MarkdownEditorShortcut = MarkdownFormat | "toggle-preview";

/** The editor action a keydown asks for, matched as ⌘ on macOS and Ctrl elsewhere. */
export function markdownEditorShortcut(
  event: KeyChordInput & { isComposing?: boolean },
  mac: boolean,
): MarkdownEditorShortcut | undefined {
  if (event.isComposing) return undefined;
  if (chordMatches(TOGGLE_PREVIEW_CHORD, event, mac)) return "toggle-preview";
  return MARKDOWN_FORMATS.find(
    (command) => command.chord && chordMatches(command.chord, event, mac),
  )?.format;
}

export type WrapFormat = "bold" | "italic" | "code" | "link" | "strikethrough";
type LineFormat = "heading" | "quote" | "unordered-list" | "ordered-list" | "task-list";

const WRAPPERS: Record<WrapFormat, [before: string, after: string, placeholder: string]> = {
  bold: ["**", "**", "bold text"],
  italic: ["_", "_", "italic text"],
  code: ["`", "`", "code"],
  link: ["[", "](https://)", "link text"],
  strikethrough: ["~~", "~~", "strikethrough text"],
};

const LINE_PREFIXES: Record<LineFormat, { prefix: (index: number) => string; pattern: RegExp }> = {
  heading: { prefix: () => "## ", pattern: /^#{1,6} / },
  quote: { prefix: () => "> ", pattern: /^> / },
  "unordered-list": { prefix: () => "- ", pattern: /^- (?!\[[ xX]\] )/ },
  "ordered-list": { prefix: (index) => `${index + 1}. `, pattern: /^\d+\. / },
  "task-list": { prefix: () => "- [ ] ", pattern: /^- \[[ xX]\] / },
};

const LIST_FORMATS = new Set<LineFormat>(["unordered-list", "ordered-list", "task-list"]);
const ANY_LIST_MARKER = /^(?:- \[[ xX]\] |- |\d+\. )/;

/** How a format treats an empty selection, and what a new link selects. */
export interface MarkdownFormatOptions {
  /** `placeholder` inserts selected sample text; `word` wraps the word at the caret, or empty markers. */
  emptySelection?: "placeholder" | "word";
  /** What a new link selects: its text, or its URL so the next keystrokes replace it. */
  linkSelection?: "text" | "url";
}

const WORD_CHARACTER = /[\p{L}\p{N}_]/u;

/** The word around `offset`, or null when the caret touches none. */
export function wordAt(value: string, offset: number): { start: number; end: number } | null {
  let start = offset;
  let end = offset;
  while (start > 0 && WORD_CHARACTER.test(value[start - 1]!)) start -= 1;
  while (end < value.length && WORD_CHARACTER.test(value[end]!)) end += 1;
  return start === end ? null : { start, end };
}

/** Whether the selection already sits inside, or spans, `format`'s markers. */
export function selectionHasFormat(edit: MarkdownEdit, format: WrapFormat): boolean {
  const { value, selectionStart: start, selectionEnd: end } = edit;
  const [before, after] = WRAPPERS[format];
  const selected = value.slice(start, end);
  if (
    selected.length >= before.length + after.length &&
    selected.startsWith(before) &&
    selected.endsWith(after)
  ) {
    return true;
  }
  // `**` also reads as two italic `*`, so a bold run never counts as italic here.
  if (format === "italic" && value.slice(start - 2, start) === "**") return false;
  return (
    value.slice(start - before.length, start) === before &&
    value.slice(end, end + after.length) === after
  );
}

/** The marker that opens and closes `format`, as this table writes it. */
export function wrapMarker(format: WrapFormat): string {
  return WRAPPERS[format][0];
}

function wrapWordAtCaret(edit: MarkdownEdit, format: WrapFormat): MarkdownEdit {
  const { value, selectionStart: caret } = edit;
  const [before, after] = WRAPPERS[format];
  const word = wordAt(value, caret);
  if (!word) {
    return {
      value: value.slice(0, caret) + before + after + value.slice(caret),
      selectionStart: caret + before.length,
      selectionEnd: caret + before.length,
    };
  }
  const wrapped = wrapSelection(
    { value, selectionStart: word.start, selectionEnd: word.end },
    format,
  );
  // The caret keeps its place in the word, which moved by the markers added or removed.
  const shift = wrapped.selectionStart - word.start;
  return { value: wrapped.value, selectionStart: caret + shift, selectionEnd: caret + shift };
}

function wrapSelection(
  edit: MarkdownEdit,
  format: WrapFormat,
  options: MarkdownFormatOptions = {},
): MarkdownEdit {
  const { value, selectionStart: start, selectionEnd: end } = edit;
  const [before, after, placeholder] = WRAPPERS[format];
  if (start === end && options.emptySelection === "word") return wrapWordAtCaret(edit, format);
  const selected = value.slice(start, end);
  if (
    selected.length >= before.length + after.length &&
    selected.startsWith(before) &&
    selected.endsWith(after)
  ) {
    const inner = selected.slice(before.length, selected.length - after.length);
    return {
      value: value.slice(0, start) + inner + value.slice(end),
      selectionStart: start,
      selectionEnd: start + inner.length,
    };
  }
  if (
    value.slice(start - before.length, start) === before &&
    value.slice(end, end + after.length) === after
  ) {
    return {
      value: value.slice(0, start - before.length) + selected + value.slice(end + after.length),
      selectionStart: start - before.length,
      selectionEnd: end - before.length,
    };
  }
  const content = selected || placeholder;
  const wrappedValue = value.slice(0, start) + before + content + after + value.slice(end);
  if (format === "link" && options.linkSelection === "url") {
    // Selects the `https://` placeholder inside `](…)`.
    const url = start + before.length + content.length + 2;
    return { value: wrappedValue, selectionStart: url, selectionEnd: url + after.length - 3 };
  }
  return {
    value: wrappedValue,
    selectionStart: start + before.length,
    selectionEnd: start + before.length + content.length,
  };
}

/** The lines a selection touches; one ending just after a newline leaves the next line alone. */
function selectedLines(edit: MarkdownEdit): { from: number; to: number } {
  const { value, selectionStart, selectionEnd } = edit;
  // lastIndexOf clamps a negative start to 0, so the first line is found explicitly.
  const from = selectionStart === 0 ? 0 : value.lastIndexOf("\n", selectionStart - 1) + 1;
  const last =
    selectionEnd > selectionStart && value[selectionEnd - 1] === "\n"
      ? selectionEnd - 1
      : selectionEnd;
  const newline = value.indexOf("\n", last);
  return { from, to: newline === -1 ? value.length : newline };
}

function prefixLines(edit: MarkdownEdit, format: LineFormat): MarkdownEdit {
  const { value } = edit;
  const { from, to } = selectedLines(edit);
  const { prefix, pattern } = LINE_PREFIXES[format];
  const lines = value.slice(from, to).split("\n");
  const remove = lines.every((line) => pattern.test(line));
  // Switching list kinds swaps the marker instead of stacking a second one.
  const replaced = LIST_FORMATS.has(format) ? ANY_LIST_MARKER : pattern;
  const replacement = lines
    .map((line, index) =>
      remove ? line.replace(pattern, "") : prefix(index) + line.replace(replaced, ""),
    )
    .join("\n");
  return {
    value: value.slice(0, from) + replacement + value.slice(to),
    selectionStart: from,
    selectionEnd: from + replacement.length,
  };
}

/** Fence the selection as a code block, or put an empty fence at the caret. */
function fenceSelection(edit: MarkdownEdit): MarkdownEdit {
  const { value, selectionStart: start, selectionEnd: end } = edit;
  const selected = value.slice(start, end);
  const before = start > 0 && value[start - 1] !== "\n" ? "\n" : "";
  const fenced = `${before}\`\`\`\n${selected}\n\`\`\`\n`;
  const caret = start + before.length + 4;
  return {
    value: value.slice(0, start) + fenced + value.slice(end),
    selectionStart: caret,
    selectionEnd: caret + selected.length,
  };
}

/** The text and selection after applying `format` to the selection: toggled when already applied. */
export function formatMarkdownSelection(
  edit: MarkdownEdit,
  format: MarkdownFormat,
  options?: MarkdownFormatOptions,
): MarkdownEdit {
  if (format === "code-block") return fenceSelection(edit);
  if (format in WRAPPERS) return wrapSelection(edit, format as WrapFormat, options);
  return prefixLines(edit, format as LineFormat);
}

/** A line's heading level: 0 for body text. */
export type HeadingLevel = 0 | 1 | 2 | 3;

export interface HeadingLevelCommand {
  level: HeadingLevel;
  /** The ⌘K row and the picker's menu item. */
  label: string;
  /** The picker's trigger, which names the level the caret's line has. */
  shortLabel: string;
  keywords: string[];
}

/** The levels a heading picker offers; the picker, its menu and ⌘K all read this list. */
export const HEADING_LEVELS: readonly HeadingLevelCommand[] = [
  { level: 0, label: "Normal Text", shortLabel: "Text", keywords: ["paragraph", "body", "plain"] },
  { level: 1, label: "Heading 1", shortLabel: "Heading 1", keywords: ["title", "h1"] },
  { level: 2, label: "Heading 2", shortLabel: "Heading 2", keywords: ["subtitle", "h2"] },
  { level: 3, label: "Heading 3", shortLabel: "Heading 3", keywords: ["h3"] },
];

const HEADING_MARKER = /^#{1,6}[ \t]+/;

/** The heading level of the line holding `offset`; levels past 3 read as 3. */
export function headingLevelAt(value: string, offset: number): HeadingLevel {
  const lineStart = offset === 0 ? 0 : value.lastIndexOf("\n", offset - 1) + 1;
  const marker = HEADING_MARKER.exec(value.slice(lineStart));
  if (!marker) return 0;
  return Math.min(marker[0].trimEnd().length, 3) as HeadingLevel;
}

/**
 * Make every selected line a heading of `level`, or body text at 0. The selection keeps the
 * same text selected, moved by the markers added or removed.
 */
export function setHeadingLevel(edit: MarkdownEdit, level: HeadingLevel): MarkdownEdit {
  const { value, selectionStart, selectionEnd } = edit;
  const { from, to } = selectedLines(edit);
  const marker = level === 0 ? "" : `${"#".repeat(level)} `;
  const lines: { oldStart: number; oldEnd: number; oldMarker: number; newStart: number }[] = [];
  let oldStart = from;
  let newStart = from;
  const replaced = value
    .slice(from, to)
    .split("\n")
    .map((line) => {
      const existing = HEADING_MARKER.exec(line)?.[0].length ?? 0;
      lines.push({ oldStart, oldEnd: oldStart + line.length, oldMarker: existing, newStart });
      oldStart += line.length + 1;
      newStart += marker.length + line.length - existing + 1;
      return marker + line.slice(existing);
    })
    .join("\n");
  const delta = replaced.length - (to - from);
  // An offset inside an old marker lands at the start of its line's text.
  const map = (position: number) => {
    if (position < from) return position;
    const line = lines.find((candidate) => position <= candidate.oldEnd);
    if (!line) return position + delta;
    const intoText = Math.max(0, position - line.oldStart - line.oldMarker);
    return line.newStart + marker.length + intoText;
  };
  return {
    value: value.slice(0, from) + replaced + value.slice(to),
    selectionStart: map(selectionStart),
    selectionEnd: map(selectionEnd),
  };
}

export function isWebURL(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

/** Pasting a web URL over selected text links it, `[text](url)`; otherwise undefined. */
export function markdownLinkPaste(
  edit: MarkdownEdit,
  pastedText: string,
): MarkdownEdit | undefined {
  const { value, selectionStart: start, selectionEnd: end } = edit;
  if (start === end) return undefined;
  const url = pastedText.trim();
  if (/\s/.test(url) || !isWebURL(url)) return undefined;
  const selected = value.slice(start, end);
  return {
    value: `${value.slice(0, start)}[${selected}](${url})${value.slice(end)}`,
    selectionStart: start + 1,
    selectionEnd: start + 1 + selected.length,
  };
}

/** Insert `text` over the selection, as typing or a paste would. */
export function insertText(edit: MarkdownEdit, text: string): MarkdownEdit {
  const { value, selectionStart: start, selectionEnd: end } = edit;
  const caret = start + text.length;
  return {
    value: value.slice(0, start) + text + value.slice(end),
    selectionStart: caret,
    selectionEnd: caret,
  };
}

/**
 * Make `edit` in the textarea as one native undo step, replacing only the span that changed.
 * Without `insertText` (some engines, jsdom) it edits the value and reports the input itself.
 */
export function applyUndoableTextEdit(textarea: HTMLTextAreaElement, edit: MarkdownEdit): void {
  const current = textarea.value;
  if (current !== edit.value) {
    let start = 0;
    while (
      start < current.length &&
      start < edit.value.length &&
      current[start] === edit.value[start]
    )
      start += 1;
    let currentEnd = current.length;
    let updatedEnd = edit.value.length;
    while (
      currentEnd > start &&
      updatedEnd > start &&
      current[currentEnd - 1] === edit.value[updatedEnd - 1]
    ) {
      currentEnd -= 1;
      updatedEnd -= 1;
    }
    const text = edit.value.slice(start, updatedEnd);
    textarea.focus();
    textarea.setSelectionRange(start, currentEnd);
    let inserted: boolean;
    try {
      // insertText makes one native undo step; assigning the value would clear the undo stack.
      inserted =
        typeof document.execCommand === "function" &&
        (text
          ? textarea.ownerDocument.execCommand("insertText", false, text)
          : textarea.ownerDocument.execCommand("delete"));
    } catch {
      inserted = false;
    }
    // Fall back only when nothing changed, so a partial native edit is never applied twice.
    if (!inserted && textarea.value === current) {
      textarea.setRangeText(text, start, currentEnd, "end");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
  textarea.setSelectionRange(edit.selectionStart, edit.selectionEnd);
}

/** The textarea's text and selection now. */
export function textareaEdit(textarea: HTMLTextAreaElement): MarkdownEdit {
  return {
    value: textarea.value,
    selectionStart: textarea.selectionStart,
    selectionEnd: textarea.selectionEnd,
  };
}

/** A textarea's text, which reads every line break as LF, in the loaded body's line endings. */
export function inLineEndingsOf(source: string, text: string): string {
  // Unchanged text keeps the loaded bytes, mixed line endings included.
  if (text === source.replace(/\r\n/g, "\n")) return source;
  const crlf = source.includes("\r\n") && !/(^|[^\r])\n/.test(source);
  return crlf ? text.replace(/\r?\n/g, "\r\n") : text;
}

/**
 * Replace the textarea's text with `value` as one undo step, keeping the caret and selection
 * where they were relative to the text around the change.
 */
export function replaceKeepingSelection(textarea: HTMLTextAreaElement, value: string): void {
  const before = textarea.value;
  if (before === value) return;
  let prefix = 0;
  while (prefix < before.length && prefix < value.length && before[prefix] === value[prefix])
    prefix += 1;
  let suffix = 0;
  while (
    suffix < before.length - prefix &&
    suffix < value.length - prefix &&
    before[before.length - 1 - suffix] === value[value.length - 1 - suffix]
  )
    suffix += 1;
  // Offsets before the change stay; after it they move with it; inside it they go to its end.
  const map = (offset: number) =>
    offset <= prefix
      ? offset
      : offset >= before.length - suffix
        ? offset + value.length - before.length
        : value.length - suffix;
  applyUndoableTextEdit(textarea, {
    value,
    selectionStart: map(textarea.selectionStart),
    selectionEnd: map(textarea.selectionEnd),
  });
}
