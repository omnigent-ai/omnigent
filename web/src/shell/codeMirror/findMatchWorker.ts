// Web Worker entry for find-in-file: matching runs here, off the UI thread.

import { createFindWorkerHandler, type FindWorkerRequest } from "./findMatchWorkerProtocol";

// The app's TypeScript lib is the DOM's, so describe the worker scope it uses.
const scope = self as unknown as {
  postMessage: (message: unknown, transfer: Transferable[]) => void;
  onmessage: ((event: MessageEvent<FindWorkerRequest>) => void) | null;
};

const handle = createFindWorkerHandler((batch) =>
  scope.postMessage(batch, [batch.froms.buffer, batch.tos.buffer]),
);
scope.onmessage = (event) => handle(event.data);
