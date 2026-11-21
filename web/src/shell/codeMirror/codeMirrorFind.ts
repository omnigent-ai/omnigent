// Find and replace over a CodeMirror document: applying the bar's query and
// options and replacement text, and counting matches for its "3 of 12" status.

import { SearchQuery, getSearchQuery, setSearchQuery } from "@codemirror/search";
import type { EditorState, Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import type { FindOptions } from "@/components/FindBar";
import { findQuerySpec } from "./findMatchCollector";
import { inlineMatchSource, type MatchSource } from "./findMatchSource";

/** What the find bar shows for the current query and selection. */
export interface FindStatus {
  query: string;
  options: FindOptions;
  /** What Replace puts in place of a match. */
  replace: string;
  /** False for an invalid regular expression. */
  valid: boolean;
  /** False while matches are still being collected; `matchCount` is a lower bound then. */
  complete: boolean;
  matchCount: number;
  /** 1-based index of the selected match, or 0 when the selection isn't one. */
  ordinal: number;
}

/** CodeMirror's single-line search field writes line breaks as `\n`. */
export function findQueryFromSelection(text: string): string {
  // Search runs on the document's "\n" breaks, whatever breaks the file uses.
  return text.replace(/\r\n?|\n/g, "\\n");
}

export function findOptionsOf(query: SearchQuery): FindOptions {
  return { caseSensitive: query.caseSensitive, wholeWord: query.wholeWord, regexp: query.regexp };
}

/**
 * Sets the query. Returns whether the caller should select the first match
 * once the index finds it; a selection that already is a match stays, so
 * editing the query doesn't jump away.
 */
export function applyFindQuery(view: EditorView, search: string, options: FindOptions): boolean {
  const query = new SearchQuery({
    search,
    ...options,
    replace: getSearchQuery(view.state).replace,
  });
  const keepSelection = selectionIsMatch(view.state, query);
  view.dispatch(
    keepSelection
      ? { effects: setSearchQuery.of(query) }
      : { effects: setSearchQuery.of(query), selection: { anchor: 0 } },
  );
  return query.valid && query.search !== "" && !keepSelection;
}

/** Sets what Replace puts in place of a match; the matches themselves stay. */
export function setReplaceText(view: EditorView, replace: string): void {
  const { search, caseSensitive, literal, regexp, wholeWord } = getSearchQuery(view.state);
  const query = new SearchQuery({ search, caseSensitive, literal, regexp, wholeWord, replace });
  view.dispatch({ effects: setSearchQuery.of(query) });
}

/** Whether two queries find the same matches, whatever they replace them with. */
export function sameMatches(first: SearchQuery | undefined, second: SearchQuery): boolean {
  return (
    first !== undefined &&
    first.search === second.search &&
    first.caseSensitive === second.caseSensitive &&
    first.regexp === second.regexp &&
    first.wholeWord === second.wholeWord &&
    first.literal === second.literal
  );
}

/** Selects `match` and centers it, the way stepping through matches does. */
export function selectMatch(view: EditorView, match: { from: number; to: number }): void {
  view.dispatch({
    selection: { anchor: match.from, head: match.to },
    effects: EditorView.scrollIntoView(match.from, { y: "center" }),
    userEvent: "select.search",
  });
}

/**
 * Steps to the next or previous match from the selection. Returns false when
 * the index hasn't reached the answer yet, so the caller can retry on progress.
 */
export function stepToMatch(
  view: EditorView,
  index: FindMatchIndex,
  direction: 1 | -1,
  from: { from: number; to: number } = view.state.selection.main,
  inclusive = false,
): boolean {
  const found = index.lookup(direction, from, inclusive);
  if (found.kind === "pending") return false;
  if (found.kind === "match") selectMatch(view, found);
  return true;
}

function selectionIsMatch(state: EditorState, query: SearchQuery): boolean {
  if (!query.valid || !query.search) return false;
  const selection = state.selection.main;
  if (selection.empty) return false;
  const result = query.getCursor(state, selection.from, selection.to).next();
  return !result.done && result.value.from === selection.from && result.value.to === selection.to;
}

/** The answer to "where is the next or previous match?" */
export type MatchLookup =
  | { kind: "match"; from: number; to: number }
  /** Indexing hasn't reached the answer yet; ask again after more progress. */
  | { kind: "pending" }
  | { kind: "none" };

/**
 * The positions of every match of the current query, collected by a match
 * source that never blocks the UI thread for long. Counting, "3 of 12", and
 * stepping read the index by binary search, so none of them scans the document.
 */
export class FindMatchIndex {
  private query: SearchQuery | undefined;
  private doc: Text | undefined;
  private froms: number[] = [];
  private tos: number[] = [];
  private imprecise = new Set<number>();
  private complete = true;
  private cancelJob: (() => void) | undefined;
  private readonly onProgress: () => void;
  private readonly source: MatchSource;

  constructor(onProgress: () => void, source: MatchSource = inlineMatchSource()) {
    this.onProgress = onProgress;
    this.source = source;
  }

  /** Starts over when the query or document changed; otherwise keeps the index. */
  sync(state: EditorState): void {
    const query = getSearchQuery(state);
    if (sameMatches(this.query, query) && this.doc === state.doc) return;
    this.reset();
    this.query = query;
    this.doc = state.doc;
    this.complete = !query.search || !query.valid;
    if (this.complete) return;
    this.cancelJob = this.source.start(state.doc, findQuerySpec(query), (batch) => {
      if (batch.restart) {
        this.froms = [];
        this.tos = [];
        this.imprecise.clear();
      }
      for (const from of batch.imprecise) this.imprecise.add(from);
      for (let index = 0; index < batch.froms.length; index += 1) {
        this.froms.push(batch.froms[index]!);
        this.tos.push(batch.tos[index]!);
      }
      if (batch.done) {
        this.complete = true;
        this.cancelJob = undefined;
      }
      this.onProgress();
    });
  }

  /** Whether the index belongs to `state`'s document and query, and has every match. */
  completeFor(state: EditorState): boolean {
    return this.complete && this.belongsTo(state);
  }

  /** Whether the index belongs to `state`'s document and query, finished or not. */
  belongsTo(state: EditorState): boolean {
    return this.doc === state.doc && sameMatches(this.query, getSearchQuery(state));
  }

  /** Every match found so far, in document order. */
  matches(): { froms: readonly number[]; tos: readonly number[] } {
    return { froms: this.froms, tos: this.tos };
  }

  /**
   * False for a match that covers only part of a character, such as "e" in a
   * decomposed "é". Find still steps to it, but replacing it would change more.
   */
  isPrecise(from: number): boolean {
    return !this.imprecise.has(from);
  }

  /** Stops indexing and forgets the matches, as when find closes. */
  reset(): void {
    this.cancelJob?.();
    this.cancelJob = undefined;
    this.query = undefined;
    this.doc = undefined;
    this.froms = [];
    this.tos = [];
    this.imprecise.clear();
    this.complete = true;
  }

  dispose(): void {
    this.reset();
    this.source.dispose();
  }

  status(state: EditorState): FindStatus {
    const query = getSearchQuery(state);
    const status: FindStatus = {
      query: query.search,
      options: findOptionsOf(query),
      replace: query.replace,
      valid: query.valid,
      complete: !query.search || !query.valid,
      matchCount: 0,
      ordinal: 0,
    };
    if (status.complete || !sameMatches(this.query, query) || this.doc !== state.doc) return status;
    const selected = this.selectedIndex(state.selection.main);
    return {
      ...status,
      complete: this.complete,
      matchCount: this.froms.length,
      ordinal: selected + 1,
    };
  }

  /**
   * The match after or before `selection`. Stepping forward from a selected
   * match, including an empty one such as `^`, moves strictly past it; an
   * `inclusive` lookup, for a new query's first match, accepts one at `selection`.
   */
  lookup(
    direction: 1 | -1,
    selection: { from: number; to: number },
    inclusive = false,
  ): MatchLookup {
    const count = this.froms.length;
    if (direction === 1) {
      const strict =
        !inclusive && (selection.from !== selection.to || this.selectedIndex(selection) >= 0);
      const index = strict ? this.firstAfter(selection.from) : this.firstAtOrAfter(selection.from);
      if (index < count) return this.match(index);
      if (!this.complete) return { kind: "pending" };
      return count > 0 ? this.match(0) : { kind: "none" };
    }
    // Every match starting before `selection.from` is known once the index
    // has reached past it.
    if (!this.complete && selection.from > this.indexedTo()) return { kind: "pending" };
    const index = this.firstAtOrAfter(selection.from) - 1;
    if (index >= 0) return this.match(index);
    if (!this.complete) return { kind: "pending" };
    return count > 0 ? this.match(count - 1) : { kind: "none" };
  }

  /** Where the index is complete up to: the end of the last match found so far. */
  private indexedTo(): number {
    return this.tos.at(-1) ?? 0;
  }

  private selectedIndex(selection: { from: number; to: number }): number {
    const index = this.firstAtOrAfter(selection.from);
    return index < this.froms.length &&
      this.froms[index] === selection.from &&
      this.tos[index] === selection.to
      ? index
      : -1;
  }

  private match(index: number): MatchLookup {
    return { kind: "match", from: this.froms[index]!, to: this.tos[index]! };
  }

  private firstAtOrAfter(position: number): number {
    return this.firstWhere((from) => from >= position);
  }

  private firstAfter(position: number): number {
    return this.firstWhere((from) => from > position);
  }

  private firstWhere(test: (from: number) => boolean): number {
    let low = 0;
    let high = this.froms.length;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (test(this.froms[middle]!)) high = middle;
      else low = middle + 1;
    }
    return low;
  }
}

/** The bar's status text: empty without a query. */
export function findStatusLabel(status: FindStatus): string {
  if (!status.query) return "";
  if (!status.valid) return "Invalid pattern";
  if (!status.complete) {
    if (status.matchCount === 0) return "Searching…";
    const count = `${status.matchCount}+`;
    return status.ordinal ? `${status.ordinal} of ${count}` : count;
  }
  if (status.matchCount === 0) return "No results";
  return `${status.ordinal || 1} of ${status.matchCount}`;
}
