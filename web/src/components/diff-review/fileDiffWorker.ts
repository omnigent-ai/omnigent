// Worker entry: diff one file's before and after text per message.

import {
  computeFileDiff,
  type FileDiffWorkerRequest,
  type FileDiffWorkerResponse,
} from "./fileDiffComputation";

const scope = self as unknown as {
  onmessage: ((event: MessageEvent<FileDiffWorkerRequest>) => void) | null;
  postMessage: (message: FileDiffWorkerResponse) => void;
};

scope.onmessage = ({ data }) => {
  try {
    scope.postMessage({ id: data.id, fileDiff: computeFileDiff(data.request) });
  } catch (error) {
    scope.postMessage({
      id: data.id,
      error: error instanceof Error ? error.message : "The diff couldn't be computed.",
    });
  }
};
