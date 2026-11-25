// Worker entry: one rich Markdown diff per message, under the worker limits.

import { computeMarkdownDiff, WORKER_MARKDOWN_DIFF_LIMITS } from "./markdownDiff";
import type {
  MarkdownDiffWorkerRequest,
  MarkdownDiffWorkerResponse,
} from "./markdownDiffComputation";

const scope = self as unknown as {
  onmessage: ((event: MessageEvent<MarkdownDiffWorkerRequest>) => void) | null;
  postMessage: (message: MarkdownDiffWorkerResponse) => void;
};

scope.onmessage = ({ data }) => {
  try {
    const { before, after } = data.request;
    scope.postMessage({
      id: data.id,
      result: computeMarkdownDiff(before, after, WORKER_MARKDOWN_DIFF_LIMITS),
    });
  } catch (error) {
    scope.postMessage({
      id: data.id,
      error: error instanceof Error ? error.message : "The rich diff couldn't be prepared.",
    });
  }
};
