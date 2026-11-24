// Runs rich Markdown diffs on a worker so a large document never blocks the
// review. One diff runs at a time; a diff that runs too long stops the worker,
// and a cancelled one is skipped or its result ignored. Without workers the page
// does the work itself under much smaller limits.
// The worker applies the full limits; see markdownDiffWorker.ts.

import {
  computeMarkdownDiff,
  MAIN_THREAD_MARKDOWN_DIFF_LIMITS,
  type MarkdownDiffResult,
} from "./markdownDiff";

export interface MarkdownDiffRequest {
  before: string | null;
  after: string | null;
}

export interface MarkdownDiffWorkerRequest {
  id: number;
  request: MarkdownDiffRequest;
}

export type MarkdownDiffWorkerResponse =
  { id: number; result: MarkdownDiffResult } | { id: number; error: string };

export interface MarkdownDiffComputer {
  /** Rejects with an `AbortError` once `signal` aborts, and with an `Error` when the diff fails. */
  compute: (request: MarkdownDiffRequest, signal?: AbortSignal) => Promise<MarkdownDiffResult>;
  dispose: () => void;
}

/** How long one diff may run on the worker. */
export const MARKDOWN_DIFF_TIMEOUT_MS = 10_000;

export const TIMED_OUT_MESSAGE = "This file took too long to compare as a rich diff.";
export const COMPUTE_FAILED_MESSAGE = "The rich diff couldn't be prepared.";

type WorkerFactory = () => Worker;

function defaultWorkerFactory(): Worker {
  return new Worker(new URL("./markdownDiffWorker.ts", import.meta.url), { type: "module" });
}

function abortError(): DOMException {
  return new DOMException("The rich diff is no longer needed.", "AbortError");
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** Diff on the page, yielding first so a burst of requests doesn't block a frame. */
function computeOnMainThread(request: MarkdownDiffRequest, signal?: AbortSignal) {
  return new Promise<MarkdownDiffResult>((resolve, reject) => {
    setTimeout(() => {
      if (signal?.aborted) return reject(abortError());
      try {
        resolve(
          computeMarkdownDiff(request.before, request.after, MAIN_THREAD_MARKDOWN_DIFF_LIMITS),
        );
      } catch {
        reject(new Error(COMPUTE_FAILED_MESSAGE));
      }
    }, 0);
  });
}

interface Job {
  id: number;
  request: MarkdownDiffRequest;
  signal?: AbortSignal;
  resolve: (result: MarkdownDiffResult) => void;
  reject: (error: Error) => void;
  removeAbortListener: () => void;
}

/** A computer backed by one worker, or the main thread when workers are unavailable. */
export function createMarkdownDiffComputer(
  createWorker: WorkerFactory | null = typeof Worker === "undefined" ? null : defaultWorkerFactory,
  timeoutMs: number = MARKDOWN_DIFF_TIMEOUT_MS,
): MarkdownDiffComputer {
  let workerFactory = createWorker;
  let worker: Worker | null = null;
  let running: { job: Job; timer: ReturnType<typeof setTimeout> } | null = null;
  const queue: Job[] = [];
  let nextId = 0;
  let disposed = false;

  const stopWorker = () => {
    worker?.terminate();
    worker = null;
  };

  const settle = (job: Job) => {
    job.removeAbortListener();
    if (running?.job === job) {
      clearTimeout(running.timer);
      running = null;
    }
  };

  const ensureWorker = (): Worker | null => {
    if (worker || !workerFactory) return worker;
    try {
      worker = workerFactory();
    } catch {
      // A worker that can't start won't start next time either.
      workerFactory = null;
      return null;
    }
    worker.onmessage = (event: MessageEvent<MarkdownDiffWorkerResponse>) => {
      const job = running?.job;
      if (!job || job.id !== event.data.id) return;
      settle(job);
      if ("error" in event.data) job.reject(new Error(COMPUTE_FAILED_MESSAGE));
      else job.resolve(event.data.result);
      pump();
    };
    worker.onerror = () => {
      const job = running?.job;
      stopWorker();
      if (job) {
        settle(job);
        job.reject(new Error(COMPUTE_FAILED_MESSAGE));
      }
      pump();
    };
    return worker;
  };

  function pump() {
    if (disposed) return;
    while (!running && queue.length > 0) {
      const job = queue.shift()!;
      if (job.signal?.aborted) continue;
      const target = ensureWorker();
      if (!target) {
        computeOnMainThread(job.request, job.signal).then(
          (result) => {
            settle(job);
            job.resolve(result);
          },
          (error: Error) => {
            settle(job);
            job.reject(error);
          },
        );
        continue;
      }
      const timer = setTimeout(() => {
        if (running?.job !== job) return;
        settle(job);
        stopWorker();
        job.resolve({ status: "tooLarge", message: TIMED_OUT_MESSAGE });
        pump();
      }, timeoutMs);
      running = { job, timer };
      target.postMessage({ id: job.id, request: job.request } satisfies MarkdownDiffWorkerRequest);
    }
  }

  return {
    compute(request, signal) {
      if (disposed) return Promise.reject(abortError());
      if (signal?.aborted) return Promise.reject(abortError());
      return new Promise<MarkdownDiffResult>((resolve, reject) => {
        const job: Job = {
          id: nextId++,
          request,
          signal,
          resolve,
          reject,
          removeAbortListener: () => {},
        };
        if (signal) {
          const onAbort = () => {
            const queued = queue.indexOf(job);
            if (queued >= 0) queue.splice(queued, 1);
            // Stop a running diff rather than let it hold up the next one.
            const wasRunning = running?.job === job;
            settle(job);
            reject(abortError());
            if (wasRunning) {
              stopWorker();
              pump();
            }
          };
          signal.addEventListener("abort", onAbort, { once: true });
          job.removeAbortListener = () => signal.removeEventListener("abort", onAbort);
        }
        queue.push(job);
        pump();
      });
    },
    dispose() {
      disposed = true;
      const pending = [...queue, ...(running ? [running.job] : [])];
      queue.length = 0;
      for (const job of pending) {
        settle(job);
        job.reject(abortError());
      }
      stopWorker();
    },
  };
}
