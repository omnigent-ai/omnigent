// Find's plain-text fast path: indexOf over the folded text, with exactly SearchCursor's matches.
// Where folding would move positions it returns null, and the caller uses SearchCursor.

import type { SearchQuery } from "@codemirror/search";
import { CharCategory, EditorState, type Text } from "@codemirror/state";

export interface PlainMatch {
  from: number;
  to: number;
  precise: boolean;
}

interface FoldedDocument {
  ascii: boolean;
  /** Folded text by case sensitivity; null where folding moves positions. */
  folded: Map<boolean, string | null>;
  text: string;
}

const folds = new WeakMap<Text, FoldedDocument>();
const NON_ASCII_RUNS = /[\u0080-\uffff]+/g;
const HAS_NON_ASCII = /[\u0080-\uffff]/;
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

/** The text a plain query looks for, with `\\n`, `\\r`, `\\t` and `\\\\` read as SearchQuery reads them. */
function unquoted(query: SearchQuery): string {
  if (query.literal) return query.search;
  return query.search.replace(/\\([nrt\\])/g, (_, escape: string) =>
    escape === "n" ? "\n" : escape === "r" ? "\r" : escape === "t" ? "\t" : "\\",
  );
}

/** How SearchCursor compares text: NFKD, then lowercase unless case matters. */
function foldOf(text: string, caseSensitive: boolean): string {
  const decomposed = text.normalize("NFKD");
  return caseSensitive ? decomposed : decomposed.toLowerCase();
}

/**
 * SearchCursor folds each code point on its own. Where every code point folds to as many
 * code units as it has, and astral ones to themselves, the folded text keeps positions.
 */
function foldRun(run: string, caseSensitive: boolean): string | null {
  let folded = "";
  for (const character of run) {
    const fold = foldOf(character, caseSensitive);
    if (fold.length !== character.length || (character.length > 1 && fold !== character)) {
      return null;
    }
    folded += fold;
  }
  return folded;
}

function foldedDocument(doc: Text, text?: string): FoldedDocument {
  let entry = folds.get(doc);
  if (!entry) {
    const source = text ?? doc.toString();
    entry = { text: source, ascii: !HAS_NON_ASCII.test(source), folded: new Map() };
    folds.set(doc, entry);
  }
  return entry;
}

/** Lets a caller that already holds `doc`'s text, as the find worker does, skip copying it. */
export function rememberDocumentText(doc: Text, text: string): void {
  foldedDocument(doc, text);
}

/** `doc`'s text as SearchCursor compares it, or null where folding would move positions. */
export function foldedText(doc: Text, caseSensitive: boolean): string | null {
  const entry = foldedDocument(doc);
  const cached = entry.folded.get(caseSensitive);
  if (cached !== undefined) return cached;
  let folded: string | null;
  if (entry.ascii) {
    folded = caseSensitive ? entry.text : entry.text.toLowerCase();
  } else {
    // ASCII folds to itself, lowercased; only the other runs need a closer look.
    const ascii = (from: number, to?: number) => {
      const piece = entry.text.slice(from, to);
      return caseSensitive ? piece : piece.toLowerCase();
    };
    const pieces: string[] = [];
    let last = 0;
    folded = "";
    for (const run of entry.text.matchAll(NON_ASCII_RUNS)) {
      const runFold = foldRun(run[0], caseSensitive);
      if (runFold === null) {
        folded = null;
        break;
      }
      pieces.push(ascii(last, run.index), runFold);
      last = run.index + run[0].length;
    }
    if (folded !== null) folded = pieces.join("") + ascii(last);
  }
  entry.folded.set(caseSensitive, folded);
  return folded;
}

/** A step of matching that found nothing, so a caller on a time budget can pause after it. */
export const NOTHING_FOUND = null;

/**
 * SearchCursor's whole-word test. It reads a match's neighbours in the piece of text it was
 * reading, a line clipped to the searched range or a line break, unless the match spans
 * pieces, when it reads the document. ASCII only, so each character is one cluster.
 * Candidates come in document order, so the line around them is followed forward, once.
 */
// The default word characters, which are all whole-word search uses; built once.
let defaultCategorizer: ((char: string) => CharCategory) | undefined;

function wordTest(text: string, rangeFrom: number, rangeTo: number) {
  defaultCategorizer ??= EditorState.create().charCategorizer(0);
  const categorize = defaultCategorizer;
  const isWord = (position: number, start: number, end: number) =>
    position >= start && position < end && categorize(text[position]!) === CharCategory.Word;
  // The start of the candidate's line, clipped to the range, and the first line break from it.
  let lineStart = rangeFrom;
  let lineBreak = text.indexOf("\n", rangeFrom);
  return (from: number, to: number) => {
    while (lineBreak >= 0 && lineBreak < from) {
      lineStart = lineBreak + 1;
      lineBreak = text.indexOf("\n", lineStart);
    }
    let start = 0;
    let end = text.length;
    if (lineBreak < 0 || lineBreak >= to) {
      start = Math.max(rangeFrom, lineStart);
      end = Math.min(rangeTo, lineBreak < 0 ? text.length : lineBreak);
    } else if (to - from === 1) {
      return true;
    }
    return (
      (!isWord(from - 1, start, end) || !isWord(from, start, end)) &&
      (!isWord(to, start, end) || !isWord(to - 1, start, end))
    );
  };
}

/**
 * The matches CodeMirror's cursor gives for a plain `query` between `from` and `to`,
 * or null when this path can't give exactly those, such as for a regular expression.
 * Each whole-word candidate turned down yields `NOTHING_FOUND`.
 */
export function plainMatches(
  query: SearchQuery,
  doc: Text,
  from: number,
  to: number,
): Iterator<PlainMatch | typeof NOTHING_FOUND> | null {
  if (query.regexp || !query.valid) return null;
  const needle = foldOf(unquoted(query), query.caseSensitive);
  if (!needle || LONE_SURROGATE.test(needle)) return null;
  if (query.wholeWord && !foldedDocument(doc).ascii) return null;
  const haystack = foldedText(doc, query.caseSensitive);
  if (haystack === null) return null;
  const accepts = query.wholeWord ? wordTest(foldedDocument(doc).text, from, to) : () => true;
  // Ends the search at `to`, so a scoped range never reads the rest of the file after it.
  const searched = to < haystack.length ? haystack.slice(0, to) : haystack;
  return (function* () {
    let position = from;
    for (;;) {
      const found = searched.indexOf(needle, position);
      if (found < 0) return;
      const end = found + needle.length;
      if (accepts(found, end)) {
        yield { from: found, to: end, precise: true };
        // SearchCursor drops partial matches once one is found, so matches don't overlap.
        position = end;
      } else {
        yield NOTHING_FOUND;
        position = found + 1;
      }
    }
  })();
}
