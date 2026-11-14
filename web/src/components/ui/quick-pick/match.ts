// Fuzzy matching of one query token against one picker field. Matches rank by
// class (exact, prefix, word-boundary substring, substring, fuzzy subsequence)
// and then by a penalty within the class; ranges locate the matched characters.

/** A half-open `[start, end)` range of UTF-16 offsets into the source text. */
export interface MatchRange {
  start: number;
  end: number;
}

/**
 * - `fuzzy`: any in-order subsequence, preferring the last path component, for paths and titles.
 * - `contiguous`: substrings only.
 */
export type MatchStrategy = "fuzzy" | "contiguous";

export const MatchClass = {
  exact: 0,
  prefix: 1,
  boundarySubstring: 2,
  substring: 3,
  subsequence: 4,
} as const;

export interface FieldMatch {
  matchClass: number;
  /** Lower is better within a class. */
  penalty: number;
  /** The field's weight, passed through so callers can break ties between fields. */
  weight: number;
  ranges: MatchRange[];
}

interface FoldedCharacter {
  value: string;
  sourceOffset: number;
  sourceLength: number;
  isWord: boolean;
}

/** Text prepared once so each keystroke only scans folded buffers. */
export interface FoldedText {
  characters: FoldedCharacter[];
  values: string[];
}

const COMBINING_MARKS = /\p{M}/gu;
const WORD_CHARACTER = /[\p{L}\p{N}]/u;
const MAX_FUZZY_STARTS = 16;

function foldCharacter(character: string): string {
  const folded = character.normalize("NFD").replace(COMBINING_MARKS, "").toLowerCase();
  return folded.length > 0 ? folded : character;
}

const COMBINING_MARK = /^\p{M}$/u;

export function foldText(value: string): FoldedText {
  const characters: FoldedCharacter[] = [];
  let offset = 0;
  for (const character of value) {
    const previous = characters.at(-1);
    // A decomposed accent belongs to its base letter, so "e" + U+0301 matches and highlights as "é".
    if (previous && COMBINING_MARK.test(character)) {
      previous.sourceLength += character.length;
      offset += character.length;
      continue;
    }
    characters.push({
      value: foldCharacter(character),
      sourceOffset: offset,
      sourceLength: character.length,
      isWord: WORD_CHARACTER.test(character),
    });
    offset += character.length;
  }
  return { characters, values: characters.map((character) => character.value) };
}

function isSeparator(value: string): boolean {
  return value === "/" || value === "\\";
}

function isWordBoundary(value: string): boolean {
  return isSeparator(value) || value === "_" || value === "-" || value === "." || value === " ";
}

function sourceRange(target: FoldedText, first: number, last: number): MatchRange {
  const end = target.characters[last];
  return {
    start: target.characters[first].sourceOffset,
    end: end.sourceOffset + end.sourceLength,
  };
}

export function mergeRanges(ranges: readonly MatchRange[]): MatchRange[] {
  const sorted = [...ranges].sort((first, second) => first.start - second.start);
  const merged: MatchRange[] = [];
  for (const range of sorted) {
    const previous = merged.at(-1);
    if (previous && range.start <= previous.end) {
      previous.end = Math.max(previous.end, range.end);
    } else {
      merged.push({ ...range });
    }
  }
  return merged;
}

function contiguousStart(query: readonly string[], target: readonly string[], from = 0): number {
  const last = target.length - query.length;
  outer: for (let start = from; start <= last; start++) {
    for (let index = 0; index < query.length; index++) {
      if (target[start + index] !== query[index]) continue outer;
    }
    return start;
  }
  return -1;
}

// The first occurrence at a word start, else the first anywhere, so "set"
// marks "settings" in "Reset settings" rather than "Reset".
function bestSubstring(
  query: readonly string[],
  target: FoldedText,
): { start: number; atWordStart: boolean } | null {
  const values = target.values;
  let first = -1;
  for (let start = contiguousStart(query, values); start !== -1;) {
    if (start === 0 || !target.characters[start - 1].isWord) return { start, atWordStart: true };
    if (first === -1) first = start;
    start = contiguousStart(query, values, start + 1);
  }
  return first === -1 ? null : { start: first, atWordStart: false };
}

interface SubsequenceMatch {
  score: number;
  offsets: number[];
}

// Score a subsequence starting at `startIndex`: gaps and starts away from
// word boundaries cost more, so compact and initial-letter matches win.
function scanToEnd(
  query: readonly string[],
  target: readonly string[],
  startIndex: number,
  endIndex: number,
): SubsequenceMatch | null {
  let score = 1;
  const offsets = [startIndex];
  let lastIndex = startIndex;
  let lastSequential = false;
  if (startIndex > 0 && !isWordBoundary(target[startIndex - 1])) score += 2;
  for (let queryIndex = 1; queryIndex < query.length; queryIndex++) {
    let index = -1;
    for (let candidate = lastIndex + 1; candidate < endIndex; candidate++) {
      if (target[candidate] === query[queryIndex]) {
        index = candidate;
        break;
      }
    }
    if (index === -1) return null;
    if (index === lastIndex + 1) {
      if (!lastSequential) {
        lastSequential = true;
        score += 1;
      }
    } else {
      if (!isWordBoundary(target[index - 1])) score += 2;
      lastSequential = false;
      score += index - lastIndex;
    }
    offsets.push(index);
    lastIndex = index;
  }
  return { score, offsets };
}

// Try a bounded, evenly spread set of starting positions so an early,
// scattered match does not hide a compact later one.
function scanFromEachStart(
  query: readonly string[],
  target: readonly string[],
  from: number,
  to: number,
): SubsequenceMatch | null {
  const starts: number[] = [];
  for (let index = from; index < to; index++) {
    if (target[index] === query[0]) starts.push(index);
  }
  if (starts.length === 0) return null;
  const limit = Math.min(starts.length, MAX_FUZZY_STARTS);
  let best: SubsequenceMatch | null = null;
  for (let rank = 0; rank < limit; rank++) {
    const startIndex =
      starts[limit === 1 ? 0 : Math.floor((rank * (starts.length - 1)) / (limit - 1))];
    const match = scanToEnd(query, target, startIndex, to);
    if (match && (!best || match.score < best.score)) best = match;
  }
  return best;
}

// Path-like targets prefer matches inside the last path component.
function subsequenceMatch(
  query: readonly string[],
  target: readonly string[],
): SubsequenceMatch | null {
  let filenameStart = 0;
  for (let index = target.length - 1; index >= 0; index--) {
    if (isSeparator(target[index])) {
      filenameStart = index + 1;
      break;
    }
  }
  if (filenameStart < target.length) {
    const match = scanFromEachStart(query, target, filenameStart, target.length);
    if (match) {
      const filenameLength = target.length - filenameStart;
      const score =
        query.length === filenameLength
          ? match.score / 4
          : (match.score / 2) * (2 - query.length / filenameLength);
      return { score, offsets: match.offsets };
    }
  }
  if (filenameStart === 0) return null;
  const match = scanFromEachStart(query, target, 0, target.length);
  return match ? { score: match.score * 10, offsets: match.offsets } : null;
}

export function matchField(
  query: readonly string[],
  target: FoldedText,
  weight: number,
  matching: MatchStrategy = "fuzzy",
): FieldMatch | null {
  const values = target.values;
  if (query.length === 0 || values.length === 0 || query.length > values.length) return null;
  const isPrefix = query.every((value, index) => values[index] === value);
  if (isPrefix) {
    const range = [sourceRange(target, 0, query.length - 1)];
    return query.length === values.length
      ? { matchClass: MatchClass.exact, penalty: 0, weight, ranges: range }
      : {
          matchClass: MatchClass.prefix,
          penalty: values.length - query.length,
          weight,
          ranges: range,
        };
  }
  const substring = bestSubstring(query, target);
  if (substring) {
    const { start, atWordStart } = substring;
    return {
      matchClass: atWordStart ? MatchClass.boundarySubstring : MatchClass.substring,
      penalty: start + values.length - query.length,
      weight,
      ranges: [sourceRange(target, start, start + query.length - 1)],
    };
  }
  if (matching === "contiguous") return null;
  const match = subsequenceMatch(query, values);
  if (!match) return null;
  return {
    matchClass: MatchClass.subsequence,
    penalty: match.score,
    weight,
    ranges: mergeRanges(match.offsets.map((offset) => sourceRange(target, offset, offset))),
  };
}
