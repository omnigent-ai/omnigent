// Where find's matching runs: in a Web Worker when the platform has one, or
// on the main thread in short slices otherwise.

import type { Text } from "@codemirror/state";
import { type FindQuerySpec, type MatchBatch, MatchCollector } from "./findMatchCollector";
import type { FindWorkerBatch, FindWorkerRequest } from "./findMatchWorkerProtocol";

/** Starts collecting matches; `onBatch` runs per batch until one is `done`. Returns cancel. */
export interface MatchSource {
  start: (doc: Text, spec: FindQuerySpec, onBatch: (batch: MatchBatch) => void) => () => void;
  dispose: () => void;
}

/** Runs indexing slices off the current task. */
export interface FindScheduler {
  schedule: (task: () => void) => () => void;
  now: () => number;
}

export const timeoutScheduler: FindScheduler = {
  schedule: (task) => {
    const id = setTimeout(task, 0);
    return () => clearTimeout(id);
  },
  now: () => performance.now(),
};

const INLINE_SLICE_MILLISECONDS = 8;

/** Main-thread matching in time-boxed slices, for platforms without workers. */
export function inlineMatchSource(scheduler: FindScheduler = timeoutScheduler): MatchSource {
  return {
    start(doc, spec, onBatch) {
      const collector = new MatchCollector(doc, spec);
      let cancelSlice: (() => void) | undefined;
      let cancelled = false;
      const run = () => {
        cancelSlice = undefined;
        if (cancelled) return;
        const batch = collector.step(INLINE_SLICE_MILLISECONDS, scheduler.now);
        onBatch(batch);
        if (!batch.done && !cancelled) cancelSlice = scheduler.schedule(run);
      };
      run();
      return () => {
        cancelled = true;
        cancelSlice?.();
      };
    },
    dispose() {},
  };
}

/** The slice of `Worker` the worker source uses. */
export interface FindWorkerPort {
  postMessage: (message: FindWorkerRequest) => void;
  onmessage: ((event: MessageEvent<FindWorkerBatch>) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
  terminate: () => void;
}

const documentIds = new WeakMap<Text, number>();
let nextDocumentId = 1;

function documentIdOf(doc: Text): number {
  let id = documentIds.get(doc);
  if (id === undefined) {
    id = nextDocumentId++;
    documentIds.set(doc, id);
  }
  return id;
}

/**
 * Matching in a worker. The document is sent once per version; a worker that
 * fails hands the current and later searches to `fallback`.
 */
export function workerMatchSource(
  createWorker: () => FindWorkerPort,
  fallback: MatchSource = inlineMatchSource(),
): MatchSource {
  let worker: FindWorkerPort | undefined;
  let failed = false;
  let sentDocumentId = -1;
  let nextJobId = 1;
  let active:
    | { jobId: number; doc: Text; spec: FindQuerySpec; onBatch: (batch: MatchBatch) => void }
    | undefined;
  let cancelFallback: (() => void) | undefined;

  const failOver = () => {
    failed = true;
    worker?.terminate();
    worker = undefined;
    if (active) {
      const { doc, spec, onBatch } = active;
      active = undefined;
      // The fallback starts from the top, so its first batch replaces the worker's.
      let first = true;
      cancelFallback = fallback.start(doc, spec, (batch) => {
        onBatch(first ? { ...batch, restart: true } : batch);
        first = false;
      });
    }
  };

  const ensureWorker = (): FindWorkerPort | undefined => {
    if (failed) return undefined;
    if (worker) return worker;
    try {
      worker = createWorker();
    } catch {
      failed = true;
      return undefined;
    }
    worker.onmessage = (event) => {
      const batch = event.data;
      if (!active || batch.jobId !== active.jobId) return;
      const { onBatch } = active;
      if (batch.done) active = undefined;
      onBatch({
        froms: Array.from(batch.froms),
        tos: Array.from(batch.tos),
        imprecise: Array.from(batch.imprecise),
        done: batch.done,
      });
    };
    worker.onerror = () => failOver();
    return worker;
  };

  return {
    start(doc, spec, onBatch) {
      const port = ensureWorker();
      if (!port) return fallback.start(doc, spec, onBatch);
      const documentId = documentIdOf(doc);
      if (documentId !== sentDocumentId) {
        port.postMessage({ type: "document", documentId, text: doc.toString() });
        sentDocumentId = documentId;
      }
      const jobId = nextJobId++;
      active = { jobId, doc, spec, onBatch };
      port.postMessage({ type: "search", jobId, documentId, spec });
      return () => {
        if (active?.jobId === jobId) {
          active = undefined;
          port.postMessage({ type: "cancel", jobId });
        }
        cancelFallback?.();
        cancelFallback = undefined;
      };
    },
    dispose() {
      cancelFallback?.();
      worker?.terminate();
      worker = undefined;
      active = undefined;
    },
  };
}

/** The best source for this platform: the worker, or the inline fallback. */
export function defaultMatchSource(): MatchSource {
  if (typeof Worker === "undefined") return inlineMatchSource();
  return workerMatchSource(
    () =>
      new Worker(new URL("./findMatchWorker.ts", import.meta.url), {
        type: "module",
      }) as unknown as FindWorkerPort,
  );
}
