// Query matching for pickers. Each query word must match some field of a row;
// rows rank by match class (exact, prefix, word-boundary substring, substring,
// fuzzy subsequence), then penalty, then field weight. A field holding the whole
// multi-word query as one phrase outranks words matched separately, so "show files"
// finds "Show Files" before rows that only list those words as keywords.

/** A half-open `[start, end)` range of UTF-16 offsets into the source text. */
export interface MatchRange {
  start: number;
  end: number;
}

/**
 * - `fuzzy`: any in-order subsequence, preferring the last path component, for paths and titles.
 * - `words`: a subsequence made of word beginnings ("nt" in "New tab"), or one held inside a
 *   single word ("stng" in "Settings"), for short named things such as commands.
 * - `contiguous`: substrings only.
 */
export type MatchStrategy = "fuzzy" | "words" | "contiguous";

export interface SearchField {
  /** Identifies the field whose ranges get highlighted; omit for hidden aliases. */
  key?: string;
  text: string;
  /** Higher weights win ties between equally good matches. */
  weight: number;
  matching?: MatchStrategy;
}

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

export interface CandidateMatch {
  /** One field holds the whole multi-word query, in order; `weight` is then that field's. */
  wholeQuery: boolean;
  worstClass: number;
  classTotal: number;
  penalty: number;
  weight: number;
  /** Highlight ranges keyed by `SearchField.key`. */
  ranges: Record<string, MatchRange[]>;
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

/** Each word's `[start, end)` span in folded characters. */
function wordSpans(target: FoldedText): [number, number][] {
  const spans: [number, number][] = [];
  target.characters.forEach((character, index) => {
    if (!character.isWord) return;
    const previous = spans.at(-1);
    if (previous && previous[1] === index) previous[1] = index + 1;
    else spans.push([index, index + 1]);
  });
  return spans;
}

/**
 * The query as consecutive pieces taken from words in order, each starting at its
 * word's first letter and continuing as a subsequence inside that word, so "gostng"
 * finds "Go to Settings". Fewer pieces, skipped words, and gaps score better.
 */
function wordsMatch(query: readonly string[], target: FoldedText): SubsequenceMatch | null {
  const values = target.values;
  const spans = wordSpans(target);
  const memo = new Map<number, SubsequenceMatch | null>();
  const search = (queryIndex: number, spanIndex: number): SubsequenceMatch | null => {
    if (queryIndex === query.length) return { score: 0, offsets: [] };
    const key = queryIndex * (spans.length + 1) + spanIndex;
    const known = memo.get(key);
    if (known !== undefined) return known;
    let best: SubsequenceMatch | null = null;
    for (let index = spanIndex; index < spans.length; index++) {
      const [start, end] = spans[index];
      if (values[start] !== query[queryIndex]) continue;
      // The leftmost placement of each longer piece inside this word.
      const offsets = [start];
      let gaps = 0;
      for (;;) {
        const rest = search(queryIndex + offsets.length, index + 1);
        if (rest) {
          const score = rest.score + 1 + (index - spanIndex) + gaps;
          if (!best || score < best.score) best = { score, offsets: [...offsets, ...rest.offsets] };
        }
        const next = queryIndex + offsets.length;
        if (next === query.length) break;
        let position = offsets.at(-1)! + 1;
        while (position < end && values[position] !== query[next]) position++;
        if (position === end) break;
        gaps += position - offsets.at(-1)! - 1;
        offsets.push(position);
      }
    }
    memo.set(key, best);
    return best;
  };
  return search(0, 0);
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
  const match = matching === "words" ? wordsMatch(query, target) : subsequenceMatch(query, values);
  if (!match) return null;
  return {
    matchClass: MatchClass.subsequence,
    penalty: match.score,
    weight,
    ranges: mergeRanges(match.offsets.map((offset) => sourceRange(target, offset, offset))),
  };
}

function isBetterField(candidate: FieldMatch, current: FieldMatch): boolean {
  if (candidate.matchClass !== current.matchClass) return candidate.matchClass < current.matchClass;
  if (candidate.penalty !== current.penalty) return candidate.penalty < current.penalty;
  return candidate.weight > current.weight;
}

export interface PreparedField {
  key?: string;
  target: FoldedText;
  weight: number;
  matching: MatchStrategy;
}

export function prepareFields(fields: readonly SearchField[]): PreparedField[] {
  return fields
    .filter((field) => field.text.length > 0)
    .map((field) => ({
      key: field.key,
      target: foldText(field.text),
      weight: field.weight,
      matching: field.matching ?? "fuzzy",
    }));
}

/** A query token prepared once per keystroke. */
export type QueryToken = string[];

// Pull request numbers display as `#1234` while fields usually store `1234`.
function searchableToken(token: string): string {
  return /^#\d+$/.test(token) ? token.slice(1) : token;
}

export function tokenizeQuery(query: string): QueryToken[] {
  return query
    .trim()
    .split(/\s+/)
    .filter((token) => token.length > 0)
    .map((token) => foldText(searchableToken(token)).values);
}

interface PhraseOccurrence {
  start: number;
  matchClass: number;
  penalty: number;
}

/** The phrase's best occurrence in `target`: at the start, then at a word, then anywhere. */
function phraseOccurrence(phrase: readonly string[], target: FoldedText): PhraseOccurrence | null {
  const substring = bestSubstring(phrase, target);
  if (!substring) return null;
  const { start, atWordStart } = substring;
  const length = target.values.length;
  if (start === 0) {
    return phrase.length === length
      ? { start, matchClass: MatchClass.exact, penalty: 0 }
      : { start, matchClass: MatchClass.prefix, penalty: length - phrase.length };
  }
  return {
    start,
    matchClass: atWordStart ? MatchClass.boundarySubstring : MatchClass.substring,
    penalty: start + length - phrase.length,
  };
}

/**
 * Every token in order, one space apart, inside one field. The weightiest such
 * field wins, so a label holding the phrase beats an alias holding it exactly.
 */
function wholeQueryMatch(
  tokens: readonly QueryToken[],
  fields: readonly PreparedField[],
): CandidateMatch | null {
  if (tokens.length < 2) return null;
  const phrase = tokens.flatMap((token, index) => (index === 0 ? token : [" ", ...token]));
  let best: PhraseOccurrence | null = null;
  let bestField: PreparedField | undefined;
  for (const field of fields) {
    const occurrence = phraseOccurrence(phrase, field.target);
    if (!occurrence) continue;
    const better =
      !best ||
      !bestField ||
      field.weight > bestField.weight ||
      (field.weight === bestField.weight &&
        (occurrence.matchClass < best.matchClass ||
          (occurrence.matchClass === best.matchClass && occurrence.penalty < best.penalty)));
    if (better) {
      best = occurrence;
      bestField = field;
    }
  }
  if (!best || !bestField) return null;
  const ranges: Record<string, MatchRange[]> = {};
  if (bestField.key !== undefined) {
    // Mark each word, not the spaces between them, as separately matched words are marked.
    let wordStart = best.start;
    ranges[bestField.key] = tokens.map((token) => {
      const range = sourceRange(bestField.target, wordStart, wordStart + token.length - 1);
      wordStart += token.length + 1;
      return range;
    });
  }
  return {
    wholeQuery: true,
    worstClass: best.matchClass,
    classTotal: best.matchClass,
    penalty: best.penalty,
    weight: bestField.weight,
    ranges,
  };
}

/** Returns null when any token fails to match every field. */
export function matchCandidate(
  tokens: readonly QueryToken[],
  fields: readonly PreparedField[],
): CandidateMatch | null {
  const whole = wholeQueryMatch(tokens, fields);
  if (whole) return whole;
  let worstClass = 0;
  let classTotal = 0;
  let penalty = 0;
  let weight = 0;
  const ranges: Record<string, MatchRange[]> = {};
  for (const token of tokens) {
    let best: FieldMatch | null = null;
    let bestKey: string | undefined;
    for (const field of fields) {
      const match = matchField(token, field.target, field.weight, field.matching);
      if (match && (!best || isBetterField(match, best))) {
        best = match;
        bestKey = field.key;
      }
    }
    if (!best) return null;
    worstClass = Math.max(worstClass, best.matchClass);
    classTotal += best.matchClass;
    penalty += best.penalty;
    weight += best.weight;
    if (bestKey !== undefined) (ranges[bestKey] ??= []).push(...best.ranges);
  }
  for (const key of Object.keys(ranges)) ranges[key] = mergeRanges(ranges[key]);
  return { wholeQuery: false, worstClass, classTotal, penalty, weight, ranges };
}

/**
 * Orders candidates best first; callers break ties. Whole-query matches lead, by
 * the weight of the field holding the phrase and then its quality; the rest by
 * match quality, then field weight.
 */
export function compareCandidateMatches(first: CandidateMatch, second: CandidateMatch): number {
  if (first.wholeQuery !== second.wholeQuery) return first.wholeQuery ? -1 : 1;
  if (first.wholeQuery && first.weight !== second.weight) return second.weight - first.weight;
  return (
    first.worstClass - second.worstClass ||
    first.classTotal - second.classTotal ||
    first.penalty - second.penalty ||
    second.weight - first.weight
  );
}
