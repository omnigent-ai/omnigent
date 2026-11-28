// Collects every match of a find query with CodeMirror's own full-document
// cursor, in steps that pause and resume, so matching keeps its exact rules
// (multiline patterns, ^ and $) while the work is spread over time.

import { SearchQuery } from "@codemirror/search";
import type { Text } from "@codemirror/state";
import { NOTHING_FOUND, plainMatches } from "./plainTextMatches";

export { NOTHING_FOUND };

/** A find query as plain data, so it can cross to a worker. */
export interface FindQuerySpec {
  search: string;
  caseSensitive: boolean;
  wholeWord: boolean;
  regexp: boolean;
  /** Limits matches to these ranges, in document order, for Find in selection. */
  scope?: readonly { from: number; to: number }[] | null;
}

/** Matches found since the previous step, in document order. */
export interface MatchBatch {
  froms: number[];
  tos: number[];
  /** Starts of matches that cover only part of a character, as text normalization can find. */
  imprecise: number[];
  done: boolean;
  /** Earlier batches are void: matching started over from the top of the document. */
  restart?: boolean;
}

export function findQuerySpec(
  query: SearchQuery,
  scope: readonly { from: number; to: number }[] | null = null,
): FindQuerySpec {
  return {
    search: query.search,
    caseSensitive: query.caseSensitive,
    wholeWord: query.wholeWord,
    regexp: query.regexp,
    scope,
  };
}

interface CursorResult {
  from: number;
  to: number;
  precise?: boolean;
  match?: RegExpExecArray;
}

/** CodeMirror's rule for a pattern that can span lines, which it matches across the document. */
const MULTILINE_PATTERN = /\\[sWDnr]|\n|\r|\[\^/;

/**
 * What a match between `from` and `to` is read in, so it sees the same text a whole-file
 * search does: its lines for a pattern matched line by line, or the whole document for a
 * regular expression that can span lines.
 */
export function matchContext(
  doc: Text,
  query: SearchQuery,
  from: number,
  to: number,
): { from: number; to: number } {
  if (query.regexp && MULTILINE_PATTERN.test(query.search)) return { from: 0, to: doc.length };
  return { from: doc.lineAt(from).from, to: doc.lineAt(to).to };
}

function cursorIn(query: SearchQuery, doc: Text, from: number, to: number) {
  const cursor = plainMatches(query, doc, from, to) ?? query.getCursor(doc, from, to);
  return { [Symbol.iterator]: () => cursor as Iterator<CursorResult | typeof NOTHING_FOUND> };
}

/**
 * Every match of `query` in `doc`, or only those wholly inside `scope`'s sorted ranges. A
 * scoped search reads each range in its whole-file context, so `^`, `$` and lookaround
 * answer as they would across the file, and a match that runs past the scope is left out.
 * An empty scope has no matches. Each match left out, or whole-word candidate turned
 * down, yields `NOTHING_FOUND`, so a caller working to a time budget can pause between them.
 */
export function* matchesIn(
  query: SearchQuery,
  doc: Text,
  scope: readonly { from: number; to: number }[] | null | undefined,
): Generator<CursorResult | typeof NOTHING_FOUND> {
  if (!scope) {
    yield* cursorIn(query, doc, 0, doc.length);
    return;
  }
  const contexts: { from: number; to: number }[] = [];
  for (const range of scope) {
    const context = matchContext(doc, query, range.from, range.to);
    const last = contexts.at(-1);
    if (last && context.from <= last.to) last.to = Math.max(last.to, context.to);
    else contexts.push({ ...context });
  }
  let rangeIndex = 0;
  for (const context of contexts) {
    for (const result of cursorIn(query, doc, context.from, context.to)) {
      if (result === NOTHING_FOUND) {
        yield NOTHING_FOUND;
        continue;
      }
      while (rangeIndex < scope.length && scope[rangeIndex]!.to < result.from) rangeIndex += 1;
      const range = scope[rangeIndex];
      yield range && result.from >= range.from && result.to <= range.to ? result : NOTHING_FOUND;
    }
    // A context with no matches still took time, so the caller can pause before the next.
    yield NOTHING_FOUND;
  }
}

export class MatchCollector {
  private readonly cursor: Iterator<CursorResult | typeof NOTHING_FOUND>;
  private done = false;

  constructor(doc: Text, spec: FindQuerySpec) {
    const query = new SearchQuery(spec);
    this.cursor = matchesIn(query, doc, spec.scope);
    this.done = !query.valid || !query.search;
  }

  /** Advances until `budgetMilliseconds` have passed or the document is exhausted. */
  step(budgetMilliseconds: number, now: () => number): MatchBatch {
    const batch: MatchBatch = { froms: [], tos: [], imprecise: [], done: this.done };
    if (this.done) return batch;
    const deadline = now() + budgetMilliseconds;
    do {
      const result = this.cursor.next();
      if (result.done) {
        this.done = true;
        break;
      }
      if (result.value === NOTHING_FOUND) continue;
      batch.froms.push(result.value.from);
      batch.tos.push(result.value.to);
      if (result.value.precise === false) batch.imprecise.push(result.value.from);
    } while (now() < deadline);
    batch.done = this.done;
    return batch;
  }
}
