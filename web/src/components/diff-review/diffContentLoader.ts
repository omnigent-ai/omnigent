// Loads every changed file's diff for the review, a few at a time, with the
// file the person opened first. A refresh reloads only files whose listing
// changed, and keeps showing their previous diff until the new one is ready or,
// if the refresh fails, marks that diff as out of date until a retry succeeds.

import type { FileDiffMetadata } from "@pierre/diffs";
import type { FileDiffComputer } from "./fileDiffComputation";
import type { DiffReviewContent } from "./reviewItems";

/** A file's before and after text, or why the source can't diff it. */
export type DiffSourceText =
  { before: string | null; after: string | null } | { unavailable: string };

/** One file to load, with a fingerprint that changes whenever its content may have. */
export interface DiffContentRequest {
  path: string;
  fingerprint: string;
}

/** How one file's text is asked for. */
export interface DiffTextRequest {
  /** Bypass caches: the file changed since its text was last fetched. */
  fresh: boolean;
  /** The listing's fingerprint for the version being loaded. */
  fingerprint: string;
  /** Aborts when the loader is disposed or the file is no longer listed. */
  signal: AbortSignal;
}

export interface DiffContentLoaderOptions {
  /** Fetch one file's text. */
  fetchText: (path: string, request: DiffTextRequest) => Promise<DiffSourceText>;
  computer: FileDiffComputer;
  /**
   * Whether a failure stands until the person retries it: neither a new
   * listing of the same version nor {@link DiffContentLoader.retryFailures} loads it again.
   */
  holdsFailure?: (cause: unknown) => boolean;
  /**
   * Whether a failure only means the answer came too late to show, as after a
   * lost connection: the file stays as it was and loads with the next listing.
   */
  isSuperseded?: (cause: unknown) => boolean;
  /** Files fetched at once. */
  concurrency?: number;
  ignoreWhitespace?: boolean;
}

interface Entry {
  fingerprint: string;
  /** The latest fetched text. */
  text?: DiffSourceText;
  /** Bumped whenever the file may have changed, so it must be fetched again. */
  changeRevision: number;
  /** The change revision the latest fetched text is known to include. */
  textRevision: number;
  /** The last content that loaded, and the text and whitespace setting it came from. */
  shown?: { text: DiffSourceText; ignoreWhitespace: boolean; content: DiffReviewContent };
  /** A change arrived while this path was loading; load again afterwards. */
  reloadAfterLoad: boolean;
  /** The last failure stands until an explicit retry or a new version. */
  held: boolean;
  queued: boolean;
  content: DiffReviewContent;
}

export const DEFAULT_DIFF_LOAD_CONCURRENCY = 4;

function errorMessage(cause: unknown): string {
  const detail = cause instanceof Error && cause.message ? cause.message : "Try again.";
  return `Couldn't load this diff. ${detail}`;
}

export class DiffContentLoader {
  private readonly entries = new Map<string, Entry>();
  private readonly queue: string[] = [];
  /** One load runs per path at a time, so a request never joins an older in-flight one. */
  private readonly loadingPaths = new Set<string>();
  /** Each fetch in flight, by path, so dropping the file or the loader stops it. */
  private readonly fetches = new Map<string, AbortController>();
  private readonly listeners = new Set<() => void>();
  private readonly fetchText: DiffContentLoaderOptions["fetchText"];
  private readonly holdsFailure: (cause: unknown) => boolean;
  private readonly isSuperseded: (cause: unknown) => boolean;
  private readonly computer: FileDiffComputer;
  private readonly concurrency: number;
  private ignoreWhitespace: boolean;
  private active = 0;
  private disposed = false;
  /** While paused, loads already running finish and nothing new starts. */
  private paused = false;
  private snapshot: ReadonlyMap<string, DiffReviewContent> = new Map();
  private notifyScheduled = false;

  constructor(options: DiffContentLoaderOptions) {
    this.fetchText = options.fetchText;
    this.holdsFailure = options.holdsFailure ?? (() => false);
    this.isSuperseded = options.isSuperseded ?? (() => false);
    this.computer = options.computer;
    this.concurrency = options.concurrency ?? DEFAULT_DIFF_LOAD_CONCURRENCY;
    this.ignoreWhitespace = options.ignoreWhitespace ?? false;
  }

  /** Every listed file's diff state, as an immutable map that changes identity on updates. */
  get contents(): ReadonlyMap<string, DiffReviewContent> {
    return this.snapshot;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /**
   * Make the loader track exactly `requests`. New files and files whose
   * fingerprint changed are (re)loaded; files no longer listed are dropped.
   */
  setFiles(requests: readonly DiffContentRequest[], priorityPath?: string): void {
    const listed = new Set(requests.map((request) => request.path));
    let changed = false;
    for (const path of [...this.entries.keys()]) {
      if (listed.has(path)) continue;
      this.entries.delete(path);
      this.fetches.get(path)?.abort();
      changed = true;
    }
    for (const { path, fingerprint } of requests) {
      const entry = this.entries.get(path);
      if (!entry) {
        this.entries.set(path, {
          fingerprint,
          changeRevision: 1,
          textRevision: 0,
          reloadAfterLoad: false,
          held: false,
          queued: false,
          content: { status: "loading" },
        });
        this.enqueue(path);
        changed = true;
      } else if (entry.fingerprint !== fingerprint) {
        entry.fingerprint = fingerprint;
        entry.changeRevision += 1;
        entry.held = false;
        this.enqueue(path);
      } else if (this.needsLoad(entry) && !entry.held) {
        // An earlier load of this version failed; a new listing tries it again.
        this.enqueue(path);
      }
    }
    if (priorityPath) this.prioritize(priorityPath);
    if (changed) this.publish();
    this.pump();
  }

  /** Load `path` next, ahead of files still waiting. */
  prioritize(path: string): void {
    const index = this.queue.indexOf(path);
    if (index <= 0) return;
    this.queue.splice(index, 1);
    this.queue.unshift(path);
  }

  /** Recompute every loaded diff with or without whitespace changes. */
  setIgnoreWhitespace(ignoreWhitespace: boolean): void {
    if (ignoreWhitespace === this.ignoreWhitespace) return;
    this.ignoreWhitespace = ignoreWhitespace;
    for (const [path, entry] of this.entries) {
      if (entry.text) this.enqueue(path);
    }
    this.pump();
  }

  /** Try a failed load or failed refresh again. */
  retry(path: string): void {
    const entry = this.entries.get(path);
    if (!entry) return;
    entry.held = false;
    const { content } = entry;
    if (content.status === "failed") {
      this.setContent(entry, { status: "loading" });
    } else if (content.status !== "ready" || !content.refreshError) {
      return;
    }
    this.enqueue(path);
    this.prioritize(path);
    this.pump();
  }

  /**
   * Stop starting loads, as while the workspace can't be reached, or start
   * again. Loaded contents stay as they are, and waiting files keep their place.
   */
  setPaused(paused: boolean): void {
    if (paused === this.paused) return;
    this.paused = paused;
    this.pump();
  }

  /** Try every failed load again, except failures that stand until the person retries them. */
  retryFailures(): void {
    for (const [path, entry] of this.entries) {
      if (entry.held) continue;
      const { content } = entry;
      if (content.status === "failed" || (content.status === "ready" && content.refreshError)) {
        this.retry(path);
      }
    }
  }

  dispose(): void {
    this.disposed = true;
    for (const controller of this.fetches.values()) controller.abort();
    this.fetches.clear();
    this.queue.length = 0;
    this.listeners.clear();
    this.computer.dispose();
  }

  private needsLoad(entry: Entry): boolean {
    if (entry.textRevision < entry.changeRevision) return true;
    const { shown, text } = entry;
    return !shown || shown.text !== text || shown.ignoreWhitespace !== this.ignoreWhitespace;
  }

  private enqueue(path: string): void {
    const entry = this.entries.get(path);
    if (!entry) return;
    if (this.loadingPaths.has(path)) {
      entry.reloadAfterLoad = true;
      return;
    }
    if (entry.queued) return;
    entry.queued = true;
    this.queue.push(path);
  }

  private pump(): void {
    while (
      !this.disposed &&
      !this.paused &&
      this.active < this.concurrency &&
      this.queue.length > 0
    ) {
      const path = this.queue.shift()!;
      const entry = this.entries.get(path);
      if (!entry) continue;
      entry.queued = false;
      if (this.loadingPaths.has(path)) {
        entry.reloadAfterLoad = true;
        continue;
      }
      this.loadingPaths.add(path);
      this.active += 1;
      void this.load(path, entry).finally(() => {
        this.loadingPaths.delete(path);
        this.active -= 1;
        const latest = this.entries.get(path);
        if (latest?.reloadAfterLoad) {
          latest.reloadAfterLoad = false;
          if (this.needsLoad(latest) && !latest.held) this.enqueue(path);
        }
        this.pump();
      });
    }
  }

  private async load(path: string, entry: Entry): Promise<void> {
    const current = () => !this.disposed && this.entries.get(path) === entry;
    const fail = (cause: unknown) => {
      entry.held = this.holdsFailure(cause);
      // A failed refresh keeps the diff on screen, marked as out of date.
      if (entry.content.status === "ready") {
        this.setContent(entry, { ...entry.content, refreshError: errorMessage(cause) });
      } else {
        this.setContent(entry, { status: "failed", message: errorMessage(cause) });
      }
    };
    if (entry.textRevision < entry.changeRevision) {
      // Loads for one file never overlap, so this request starts after every
      // change counted so far and its text includes them.
      const revision = entry.changeRevision;
      const controller = new AbortController();
      this.fetches.set(path, controller);
      try {
        const text = await this.fetchText(path, {
          fresh: entry.text !== undefined,
          fingerprint: entry.fingerprint,
          signal: controller.signal,
        });
        // A newer version was listed meanwhile; its own load shows it.
        if (!current() || entry.changeRevision !== revision) return;
        // Identical content keeps the shown text, so the diff isn't recomputed.
        entry.text = entry.text && sameText(entry.text, text) ? entry.text : text;
        entry.textRevision = revision;
      } catch (cause) {
        if (!current() || entry.changeRevision !== revision) return;
        // Left to load again: the next listing of this version enqueues it.
        if (this.isSuperseded(cause)) return;
        fail(cause);
        return;
      } finally {
        if (this.fetches.get(path) === controller) this.fetches.delete(path);
      }
    }
    const text = entry.text!;
    const ignoreWhitespace = this.ignoreWhitespace;
    const { shown } = entry;
    if (
      shown?.text === text &&
      shown.ignoreWhitespace === ignoreWhitespace &&
      // An undiffable file that changed again names its new revision.
      (shown.content.status !== "unavailable" || shown.content.revision === entry.fingerprint)
    ) {
      // Nothing new to show: drop any failure or loading state from the retry.
      if (entry.content !== shown.content) this.setContent(entry, shown.content);
      return;
    }
    if ("unavailable" in text) {
      this.show(entry, text, ignoreWhitespace, {
        status: "unavailable",
        message: text.unavailable,
        revision: entry.fingerprint,
      });
      return;
    }
    try {
      const fileDiff: FileDiffMetadata = await this.computer.compute({
        path,
        before: text.before,
        after: text.after,
        ignoreWhitespace,
        revision: `${path}:${entry.fingerprint}:${entry.textRevision}:${ignoreWhitespace ? "w" : ""}`,
      });
      if (!current() || entry.textRevision !== entry.changeRevision) return;
      this.show(entry, text, ignoreWhitespace, { status: "ready", fileDiff });
    } catch (cause) {
      if (current()) fail(cause);
    }
  }

  private show(
    entry: Entry,
    text: DiffSourceText,
    ignoreWhitespace: boolean,
    content: DiffReviewContent,
  ): void {
    entry.shown = { text, ignoreWhitespace, content };
    this.setContent(entry, content);
  }

  private setContent(entry: Entry, content: DiffReviewContent): void {
    entry.content = content;
    this.publish();
  }

  // Coalesce bursts of finished loads into one update per microtask.
  private publish(): void {
    if (this.notifyScheduled) return;
    this.notifyScheduled = true;
    queueMicrotask(() => {
      this.notifyScheduled = false;
      if (this.disposed) return;
      this.snapshot = new Map([...this.entries].map(([path, entry]) => [path, entry.content]));
      for (const listener of this.listeners) listener();
    });
  }
}

function sameText(left: DiffSourceText, right: DiffSourceText): boolean {
  if ("unavailable" in left || "unavailable" in right) {
    return (
      "unavailable" in left && "unavailable" in right && left.unavailable === right.unavailable
    );
  }
  return left.before === right.before && left.after === right.after;
}
