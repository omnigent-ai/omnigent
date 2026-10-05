// Worker stand-ins for brand-rule scans: jsdom has no Worker. `BrandScanThread`
// runs the real scanner on a Node thread, so a stuck regex really is off the
// main thread; `FakeBrandScanWorker` answers in-process for component tests.

import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Worker as NodeWorker } from "node:worker_threads";
import { handleBrandScan, type BrandScanInput } from "@/lib/brandRules";

const SCANNER = pathToFileURL(
  path.join(path.dirname(fileURLToPath(import.meta.url)), "../lib/brandRules.ts"),
).href;
const THREAD = `
const { parentPort, workerData } = require("node:worker_threads");
import(workerData).then((m) =>
  parentPort.on("message", (input) => parentPort.postMessage(m.handleBrandScan(input))),
);`;

interface WorkerLike {
  onmessage: ((e: MessageEvent) => void) | null;
  onerror: ((e: ErrorEvent) => void) | null;
}

export class BrandScanThread implements WorkerLike {
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: ErrorEvent) => void) | null = null;
  terminated = false;
  private thread = new NodeWorker(THREAD, { eval: true, workerData: SCANNER });

  constructor() {
    this.thread.on("message", (data) => this.onmessage?.({ data } as MessageEvent));
    this.thread.on("error", () => this.onerror?.({} as ErrorEvent));
  }

  postMessage(input: BrandScanInput) {
    this.thread.postMessage(input);
  }

  terminate() {
    this.terminated = true;
    void this.thread.terminate();
  }
}

export class FakeBrandScanWorker implements WorkerLike {
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: ErrorEvent) => void) | null = null;

  postMessage(input: BrandScanInput) {
    setTimeout(() => this.onmessage?.({ data: handleBrandScan(input) } as MessageEvent), 0);
  }

  terminate() {}
}
