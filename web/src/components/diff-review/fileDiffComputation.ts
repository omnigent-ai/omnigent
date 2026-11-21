// Turns a file's before and after text into the diff the review renders. Large
// files take a while to diff, so the work runs on a worker and the page stays
// responsive; where workers aren't available it runs in place.

import { parseDiffFromFile, setLanguageOverride, type FileDiffMetadata } from "@pierre/diffs";
import { hashText } from "@/lib/textHash";
import { syntaxLanguage } from "./diffReviewModel";

export interface FileDiffRequest {
  path: string;
  /** Content before the change; null for an added file. */
  before: string | null;
  /** Content after the change; null for a deleted file. */
  after: string | null;
  ignoreWhitespace: boolean;
  /** Distinguishes revisions of the same file for the renderer's caches. */
  revision: string;
}

/**
 * The renderer's cache key for one side. The highlighting workers outlive a review and serve every
 * review in the window, so the key names the text itself, not just where it came from.
 */
function sideCacheKey(revision: string, side: "before" | "after", contents: string): string {
  return `${revision}:${side}:${contents.length}:${hashText(contents)}`;
}

/** Diff `request` synchronously. Throws when the diff library rejects the input. */
export function computeFileDiff(request: FileDiffRequest): FileDiffMetadata {
  const { path, before, after, revision } = request;
  const fileDiff = parseDiffFromFile(
    before === null
      ? null
      : { name: path, contents: before, cacheKey: sideCacheKey(revision, "before", before) },
    after === null
      ? null
      : { name: path, contents: after, cacheKey: sideCacheKey(revision, "after", after) },
    request.ignoreWhitespace ? { ignoreWhitespace: true } : undefined,
    true,
  );
  return setLanguageOverride(fileDiff, syntaxLanguage(path));
}

export interface FileDiffWorkerRequest {
  id: number;
  request: FileDiffRequest;
}

export type FileDiffWorkerResponse =
  { id: number; fileDiff: FileDiffMetadata } | { id: number; error: string };

/** Computes diffs off the main thread, one request at a time per worker. */
export interface FileDiffComputer {
  compute: (request: FileDiffRequest) => Promise<FileDiffMetadata>;
  dispose: () => void;
}

type WorkerFactory = () => Worker;

function defaultWorkerFactory(): Worker {
  return new Worker(new URL("./fileDiffWorker.ts", import.meta.url), { type: "module" });
}

/** A computer backed by one worker, or the main thread when `Worker` is missing. */
export function createFileDiffComputer(
  createWorker: WorkerFactory | null = typeof Worker === "undefined" ? null : defaultWorkerFactory,
): FileDiffComputer {
  if (!createWorker) {
    return {
      compute: async (request) => computeFileDiff(request),
      dispose: () => {},
    };
  }
  let worker: Worker | null = null;
  let nextId = 0;
  const pending = new Map<
    number,
    { resolve: (fileDiff: FileDiffMetadata) => void; reject: (error: Error) => void }
  >();
  const rejectAll = (message: string) => {
    for (const { reject } of pending.values()) reject(new Error(message));
    pending.clear();
  };
  const ensureWorker = () => {
    if (worker) return worker;
    worker = createWorker();
    worker.onmessage = (event: MessageEvent<FileDiffWorkerResponse>) => {
      const entry = pending.get(event.data.id);
      if (!entry) return;
      pending.delete(event.data.id);
      if ("error" in event.data) entry.reject(new Error(event.data.error));
      else entry.resolve(event.data.fileDiff);
    };
    worker.onerror = () => {
      worker?.terminate();
      worker = null;
      rejectAll("The diff couldn't be computed.");
    };
    return worker;
  };
  return {
    compute(request) {
      const id = nextId++;
      return new Promise<FileDiffMetadata>((resolve, reject) => {
        pending.set(id, { resolve, reject });
        try {
          ensureWorker().postMessage({ id, request } satisfies FileDiffWorkerRequest);
        } catch (error) {
          // A worker that can't start or take the request leaves nothing waiting on it.
          pending.delete(id);
          reject(error instanceof Error ? error : new Error(String(error)));
        }
      });
    },
    dispose() {
      worker?.terminate();
      worker = null;
      rejectAll("The diff review closed.");
    },
  };
}
