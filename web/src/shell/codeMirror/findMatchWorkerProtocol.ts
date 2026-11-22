// Messages between the file viewer and its find worker.

import { Text } from "@codemirror/state";
import { type FindQuerySpec, MatchCollector } from "./findMatchCollector";

export type FindWorkerRequest =
  | { type: "document"; documentId: number; text: string }
  | { type: "search"; jobId: number; documentId: number; spec: FindQuerySpec }
  | { type: "cancel"; jobId: number };

export interface FindWorkerBatch {
  jobId: number;
  froms: Float64Array;
  tos: Float64Array;
  imprecise: Float64Array;
  done: boolean;
}

const WORKER_STEP_MILLISECONDS = 16;

/**
 * The worker's side: keeps the latest document and runs one search at a time,
 * yielding between steps so a newer request cancels the current one.
 */
export function createFindWorkerHandler(
  post: (batch: FindWorkerBatch) => void,
  yieldToMessages: (resume: () => void) => void = (resume) => setTimeout(resume, 0),
  now: () => number = () => performance.now(),
): (request: FindWorkerRequest) => void {
  let documentId = -1;
  let doc = Text.empty;
  let currentJob = -1;
  return (request) => {
    if (request.type === "document") {
      documentId = request.documentId;
      doc = Text.of(request.text.split("\n"));
      return;
    }
    if (request.type === "cancel") {
      if (request.jobId === currentJob) currentJob = -1;
      return;
    }
    if (request.documentId !== documentId) {
      post({
        jobId: request.jobId,
        froms: new Float64Array(),
        tos: new Float64Array(),
        imprecise: new Float64Array(),
        done: true,
      });
      return;
    }
    const jobId = request.jobId;
    currentJob = jobId;
    const collector = new MatchCollector(doc, request.spec);
    const run = () => {
      if (currentJob !== jobId) return;
      const batch = collector.step(WORKER_STEP_MILLISECONDS, now);
      post({
        jobId,
        froms: Float64Array.from(batch.froms),
        tos: Float64Array.from(batch.tos),
        imprecise: Float64Array.from(batch.imprecise),
        done: batch.done,
      });
      if (!batch.done) yieldToMessages(run);
    };
    run();
  };
}
