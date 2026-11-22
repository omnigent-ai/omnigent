// Collects every match of a find query with CodeMirror's own full-document
// cursor, in steps that pause and resume, so matching keeps its exact rules
// (multiline patterns, ^ and $) while the work is spread over time.

import { SearchQuery } from "@codemirror/search";
import type { Text } from "@codemirror/state";

/** A find query as plain data, so it can cross to a worker. */
export interface FindQuerySpec {
  search: string;
  caseSensitive: boolean;
  wholeWord: boolean;
  regexp: boolean;
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

export function findQuerySpec(query: SearchQuery): FindQuerySpec {
  return {
    search: query.search,
    caseSensitive: query.caseSensitive,
    wholeWord: query.wholeWord,
    regexp: query.regexp,
  };
}

export class MatchCollector {
  private readonly cursor: Iterator<{ from: number; to: number; precise?: boolean }>;
  private done = false;

  constructor(doc: Text, spec: FindQuerySpec) {
    const query = new SearchQuery(spec);
    this.cursor = query.getCursor(doc);
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
      batch.froms.push(result.value.from);
      batch.tos.push(result.value.to);
      if (result.value.precise === false) batch.imprecise.push(result.value.from);
    } while (now() < deadline);
    batch.done = this.done;
    return batch;
  }
}
