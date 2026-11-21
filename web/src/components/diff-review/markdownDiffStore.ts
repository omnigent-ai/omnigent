// Keeps each Markdown file's rich diff for one review. A preview is prepared
// only when asked for, reused while its text is unchanged, and replaced when a
// refresh brings new text; the work for an older version is cancelled.

import type { FileDiffLoadedFiles, FileDiffMetadata } from "@pierre/diffs";
import type { MarkdownDiffSection } from "./markdownDiff";
import { isAbortError, type MarkdownDiffComputer } from "./markdownDiffComputation";

/** Where one file's rich diff stands. `previous` is the last version shown, kept while a newer one loads. */
export type MarkdownPreviewState =
  | { status: "loading"; previous?: readonly MarkdownDiffSection[] }
  | { status: "ready"; sections: readonly MarkdownDiffSection[] }
  /** The file is beyond the rich diff's limits; only the source diff can show it. */
  | { status: "unavailable"; message: string }
  | { status: "failed"; message: string; previous?: readonly MarkdownDiffSection[] };

export const FULL_TEXT_FAILED_MESSAGE = "Couldn't load this file's full text for a rich diff.";
export const NO_FULL_TEXT_MESSAGE = "This review can't load the file's full text for a rich diff.";

export interface MarkdownDiffStoreOptions {
  computer: MarkdownDiffComputer;
  /** Loads a patch's full text; without it, only diffs computed from full text can preview. */
  loadFullText?: (fileDiff: FileDiffMetadata) => Promise<FileDiffLoadedFiles>;
}

interface Entry {
  fileDiff: FileDiffMetadata;
  text?: { before: string | null; after: string | null };
  state: MarkdownPreviewState;
  controller?: AbortController;
}

/** The before and after text a full-text diff holds; null for the side that doesn't exist. */
export function fullTextOf(
  fileDiff: FileDiffMetadata,
  loaded?: FileDiffLoadedFiles,
): { before: string | null; after: string | null } {
  const before = loaded ? (loaded.oldFile?.contents ?? null) : fileDiff.deletionLines.join("");
  const after = loaded ? loaded.newFile.contents : fileDiff.additionLines.join("");
  return {
    before: fileDiff.type === "new" ? null : before,
    after: fileDiff.type === "deleted" ? null : after,
  };
}

function shownSections(state: MarkdownPreviewState | undefined) {
  if (!state) return undefined;
  if (state.status === "ready") return state.sections;
  if (state.status === "loading" || state.status === "failed") return state.previous;
  return undefined;
}

export class MarkdownDiffStore {
  private readonly entries = new Map<string, Entry>();
  private readonly listeners = new Set<() => void>();
  private readonly computer: MarkdownDiffComputer;
  private readonly loadFullText: MarkdownDiffStoreOptions["loadFullText"];
  private snapshot: ReadonlyMap<string, MarkdownPreviewState> = new Map();
  private notifyScheduled = false;
  private disposed = false;

  constructor(options: MarkdownDiffStoreOptions) {
    this.computer = options.computer;
    this.loadFullText = options.loadFullText;
  }

  /** Each requested file's rich diff, as an immutable map that changes identity on updates. */
  get states(): ReadonlyMap<string, MarkdownPreviewState> {
    return this.snapshot;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Prepare `path`'s rich diff for `fileDiff`, unless it is already prepared or on its way. */
  request(path: string, fileDiff: FileDiffMetadata): void {
    if (this.disposed) return;
    const entry = this.entries.get(path);
    if (entry?.fileDiff === fileDiff) return;
    entry?.controller?.abort();
    const next: Entry = {
      fileDiff,
      text: entry?.text,
      state: { status: "loading", previous: shownSections(entry?.state) },
    };
    this.entries.set(path, next);
    this.publish();
    void this.prepare(path, next, entry);
  }

  /** Prepare a failed rich diff again. */
  retry(path: string): void {
    const entry = this.entries.get(path);
    if (!entry || entry.state.status !== "failed") return;
    const next: Entry = {
      fileDiff: entry.fileDiff,
      text: undefined,
      state: { status: "loading", previous: entry.state.previous },
    };
    this.entries.set(path, next);
    this.publish();
    void this.prepare(path, next, undefined);
  }

  dispose(): void {
    this.disposed = true;
    for (const entry of this.entries.values()) entry.controller?.abort();
    this.listeners.clear();
    this.computer.dispose();
  }

  private async prepare(path: string, entry: Entry, previous: Entry | undefined): Promise<void> {
    const current = () => !this.disposed && this.entries.get(path) === entry;
    const controller = new AbortController();
    entry.controller = controller;
    if (entry.fileDiff.isPartial && !this.loadFullText) {
      this.settle(entry, { status: "unavailable", message: NO_FULL_TEXT_MESSAGE });
      return;
    }
    let text: { before: string | null; after: string | null };
    try {
      text = await this.textOf(entry.fileDiff);
    } catch {
      if (current()) this.settle(entry, this.failure(FULL_TEXT_FAILED_MESSAGE, entry));
      return;
    }
    if (!current()) return;
    // A refresh that left the text as it was keeps the prepared preview.
    if (
      previous?.text &&
      previous.state.status !== "failed" &&
      previous.state.status !== "loading" &&
      previous.text.before === text.before &&
      previous.text.after === text.after
    ) {
      entry.text = text;
      this.settle(entry, previous.state);
      return;
    }
    entry.text = text;
    try {
      const result = await this.computer.compute(text, controller.signal);
      if (!current()) return;
      this.settle(
        entry,
        result.status === "ready"
          ? { status: "ready", sections: result.sections }
          : { status: "unavailable", message: result.message },
      );
    } catch (error) {
      if (isAbortError(error) || !current()) return;
      this.settle(
        entry,
        this.failure(error instanceof Error ? error.message : String(error), entry),
      );
    }
  }

  private async textOf(fileDiff: FileDiffMetadata) {
    if (!fileDiff.isPartial || !this.loadFullText) return fullTextOf(fileDiff);
    return fullTextOf(fileDiff, await this.loadFullText(fileDiff));
  }

  private failure(message: string, entry: Entry): MarkdownPreviewState {
    return { status: "failed", message, previous: shownSections(entry.state) };
  }

  private settle(entry: Entry, state: MarkdownPreviewState): void {
    entry.state = state;
    entry.controller = undefined;
    this.publish();
  }

  private publish(): void {
    if (this.notifyScheduled) return;
    this.notifyScheduled = true;
    queueMicrotask(() => {
      this.notifyScheduled = false;
      if (this.disposed) return;
      this.snapshot = new Map([...this.entries].map(([path, entry]) => [path, entry.state]));
      for (const listener of this.listeners) listener();
    });
  }
}
