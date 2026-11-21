// Find's Replace and Replace all over a CodeMirror document. A plain
// replacement works from the find index's matches; one that uses a regular
// expression's groups reads them with the same full-document cursor the index
// uses, in short slices, so lookaround keeps its context and a large file stays
// responsive. Replace all applies one undoable edit.

import { getSearchQuery, type SearchQuery } from "@codemirror/search";
import type { ChangeSpec, Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import type { FindMatchIndex } from "./codeMirrorFind";
import { type FindScheduler, timeoutScheduler } from "./findMatchSource";

/** How long one slice of reading matches may run before yielding. */
const SLICE_MILLISECONDS = 8;

/** CodeMirror's rule for a pattern that can span lines, which it matches across the document. */
const MULTILINE_PATTERN = /\\[sWDnr]|\n|\r|\[\^/;

interface CursorMatch {
  from: number;
  to: number;
  match?: RegExpExecArray;
}

/** The replacement text as typed, with `\n`, `\t`, `\r` and `\\` read the way find reads them. */
function unquotedReplacement(query: SearchQuery): string {
  if (query.literal) return query.replace;
  return query.replace.replace(/\\([nrt\\])/g, (_whole, character: string) =>
    character === "n" ? "\n" : character === "r" ? "\r" : character === "t" ? "\t" : "\\",
  );
}

/** Whether a regular-expression replacement refers to the match, so each match needs its own text. */
function refersToMatch(query: SearchQuery): boolean {
  return query.regexp && /\$(&|\d)/.test(unquotedReplacement(query));
}

/**
 * The text that replaces one match. A regular expression's `$&` is the match,
 * `$1`…`$99` its groups (empty when a group didn't take part), and `$$` a dollar sign.
 */
export function replacementText(query: SearchQuery, match: RegExpExecArray | null): string {
  const replacement = unquotedReplacement(query);
  if (!query.regexp) return replacement;
  return replacement.replace(/\$([$&]|\d+)/g, (whole, reference: string) => {
    if (reference === "$") return "$";
    if (reference === "&") return match?.[0] ?? "";
    // The longest group number the match has, so "$10" with one group is "$1" then "0".
    for (let length = reference.length; length > 0; length -= 1) {
      const group = Number(reference.slice(0, length));
      if (match && group > 0 && group < match.length) {
        return (match[group] ?? "") + reference.slice(length);
      }
    }
    return whole;
  });
}

/**
 * Where to start reading so a match at `from` sees the same text the index saw:
 * its line for a single-line pattern, which is matched line by line, or the
 * top of the document for one that can span lines.
 */
function contextRange(doc: Text, query: SearchQuery, from: number, to: number) {
  if (MULTILINE_PATTERN.test(query.search)) return { from: 0, to: doc.length };
  return { from: doc.lineAt(from).from, to: doc.lineAt(to).to };
}

/** A Replace or Replace all reading matches, valid while its document and query stay. */
interface ReplaceJob {
  doc: Text;
  query: SearchQuery;
  /** The selection a Replace started from; it must still be selected when the edit lands. */
  selection?: { from: number; to: number };
  /** Waits for a complete index before starting. */
  needsIndex: boolean;
  started: boolean;
  start: () => void;
  cancelSlice?: () => void;
}

/** Runs Replace and Replace all for one editor, one at a time. */
export class FindReplacer {
  private job: ReplaceJob | undefined;
  private readonly index: FindMatchIndex;
  private readonly scheduler: FindScheduler;
  private readonly onBusyChange: () => void;

  /** `onBusyChange` runs when a replacement starts waiting or finishes. */
  constructor(
    index: FindMatchIndex,
    scheduler: FindScheduler = timeoutScheduler,
    onBusyChange: () => void = () => {},
  ) {
    this.index = index;
    this.scheduler = scheduler;
    this.onBusyChange = onBusyChange;
  }

  /** Whether a replacement is waiting for the index or still reading matches. */
  get busy(): boolean {
    return this.job !== undefined;
  }

  /**
   * Replaces the selected match, then calls `onReplaced` with where to look for
   * the next one, or null when the selection isn't a match that can be replaced.
   * A replacement that uses groups may finish after this returns.
   */
  replaceSelected(view: EditorView, onReplaced: (resumeFrom: number | null) => void): void {
    this.cancel();
    const { state } = view;
    const { from, to } = state.selection.main;
    // A match covering part of a character would replace more than it matched.
    if (state.readOnly || this.index.status(state).ordinal === 0 || !this.index.isPrecise(from)) {
      onReplaced(null);
      return;
    }
    const query = getSearchQuery(state);
    const replace = (insert: string) => {
      view.dispatch({
        changes: { from, to, insert },
        selection: { anchor: from + insert.length },
        userEvent: "input.replace",
      });
      onReplaced(from + insert.length);
    };
    if (!refersToMatch(query)) {
      replace(replacementText(query, null));
      return;
    }
    const range = contextRange(state.doc, query, from, to);
    const cursor = query.getCursor(state.doc, range.from, range.to) as Iterator<CursorMatch>;
    this.run(view, {
      doc: state.doc,
      query,
      selection: { from, to },
      needsIndex: false,
      step: () => {
        const result = cursor.next();
        if (result.done || result.value.from > from) return { done: true };
        if (result.value.from === from && result.value.to === to) {
          return { done: true, found: result.value.match ?? null };
        }
        return { done: false };
      },
      finish: (found) => {
        if (found === undefined) onReplaced(null);
        else replace(replacementText(query, found));
      },
    });
  }

  /** Replaces every match as one edit. A plain replacement waits for the index to finish. */
  replaceAll(view: EditorView): void {
    this.cancel();
    const { state } = view;
    if (state.readOnly) return;
    const query = getSearchQuery(state);
    const changes: ChangeSpec[] = [];
    if (!refersToMatch(query)) {
      const insert = replacementText(query, null);
      this.run(view, {
        doc: state.doc,
        query,
        needsIndex: true,
        step: () => {
          const { froms, tos } = this.index.matches();
          for (let match = 0; match < froms.length; match += 1) {
            const from = froms[match]!;
            if (this.index.isPrecise(from)) changes.push({ from, to: tos[match]!, insert });
          }
          return { done: true };
        },
        finish: () => this.applyAll(view, changes),
      });
      return;
    }
    // Groups come from the index's own full-document cursor, so they match what find counted.
    const cursor = query.getCursor(state.doc) as Iterator<CursorMatch>;
    this.run(view, {
      doc: state.doc,
      query,
      needsIndex: false,
      step: () => {
        const result = cursor.next();
        if (result.done) return { done: true };
        const { from, to, match } = result.value;
        changes.push({ from, to, insert: replacementText(query, match ?? null) });
        return { done: false };
      },
      finish: () => this.applyAll(view, changes),
    });
  }

  /** Continues a replacement waiting for the index, or drops one that no longer applies. */
  resume(view: EditorView): void {
    const job = this.job;
    if (!job) return;
    if (!this.stillWanted(view, job)) {
      this.cancel();
      return;
    }
    if (!job.started && (!job.needsIndex || this.index.completeFor(view.state))) {
      job.started = true;
      job.start();
    }
  }

  /** Drops a waiting or running replacement, as when find closes. */
  cancel(): void {
    this.job?.cancelSlice?.();
    this.setJob(undefined);
  }

  /**
   * Starts `step` once the job may start, calling it until it is done or a
   * slice runs out of time, then yields. Each slice first checks the job still applies.
   */
  private run(
    view: EditorView,
    {
      step,
      finish,
      ...spec
    }: Omit<ReplaceJob, "started" | "start" | "cancelSlice"> & {
      step: () => { done: boolean; found?: RegExpExecArray | null };
      finish: (found: RegExpExecArray | null | undefined) => void;
    },
  ): void {
    const job: ReplaceJob = { ...spec, started: false, start: () => slice() };
    const slice = () => {
      job.cancelSlice = undefined;
      if (this.job !== job) return;
      if (!this.stillWanted(view, job)) {
        this.cancel();
        return;
      }
      const deadline = this.scheduler.now() + SLICE_MILLISECONDS;
      let outcome: { done: boolean; found?: RegExpExecArray | null };
      do outcome = step();
      while (!outcome.done && this.scheduler.now() < deadline);
      if (!outcome.done) {
        job.cancelSlice = this.scheduler.schedule(slice);
        return;
      }
      this.setJob(undefined);
      finish(outcome.found);
    };
    this.setJob(job);
    this.resume(view);
  }

  private applyAll(view: EditorView, changes: ChangeSpec[]): void {
    if (changes.length === 0) return;
    view.dispatch({
      changes,
      effects: EditorView.announce.of(
        `Replaced ${changes.length} ${changes.length === 1 ? "match" : "matches"}.`,
      ),
      userEvent: "input.replace.all",
    });
  }

  private setJob(job: ReplaceJob | undefined): void {
    const wasBusy = this.job !== undefined;
    this.job = job;
    if (wasBusy !== (job !== undefined)) this.onBusyChange();
  }

  private stillWanted(view: EditorView, job: ReplaceJob): boolean {
    const { state } = view;
    if (state.doc !== job.doc || state.readOnly || !getSearchQuery(state).eq(job.query)) {
      return false;
    }
    const selection = state.selection.main;
    return (
      !job.selection || (selection.from === job.selection.from && selection.to === job.selection.to)
    );
  }
}
