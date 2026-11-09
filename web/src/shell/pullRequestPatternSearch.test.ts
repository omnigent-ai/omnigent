import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PATTERN_TIME_BUDGET_MS, searchPatternInWorker } from "./pullRequestPatternSearch";
import type { PatchMatchTarget } from "./pullRequestReview";

// jsdom has no workers, so the runner is driven through a fake that the test answers by hand.
class FakeWorker {
  static instances: FakeWorker[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  posted: unknown[] = [];
  terminated = false;

  constructor() {
    FakeWorker.instances.push(this);
  }
  postMessage(message: unknown) {
    this.posted.push(message);
  }
  terminate() {
    this.terminated = true;
  }
  reply(data: unknown) {
    this.onmessage?.({ data } as MessageEvent);
  }
}

const CORPUS: PatchMatchTarget[] = [
  { path: "a.ts", kind: "path", text: "a.ts" },
  { path: "a.ts", kind: "line", side: "additions", line: 2, text: "needle here" },
];
const OPTIONS = { matchCase: false, regex: true, limit: 10 };

beforeEach(() => {
  FakeWorker.instances = [];
  vi.stubGlobal("Worker", FakeWorker);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("searchPatternInWorker", () => {
  it("reports too slow and terminates the worker when the budget runs out", async () => {
    const result = searchPatternInWorker(CORPUS, "(a+)+$", OPTIONS);
    vi.advanceTimersByTime(PATTERN_TIME_BUDGET_MS);

    await expect(result).resolves.toEqual({
      matches: [],
      truncated: false,
      invalid: false,
      tooSlow: true,
    });
    expect(FakeWorker.instances[0].terminated).toBe(true);
  });

  it("maps the worker's match indices back to the caller's corpus targets", async () => {
    const result = searchPatternInWorker(CORPUS, "needle", OPTIONS);
    const [worker] = FakeWorker.instances;
    expect(worker.posted).toEqual([{ corpus: CORPUS, query: "needle", options: OPTIONS }]);

    worker.reply({ matches: [{ index: 1, start: 0, end: 6 }], truncated: false, invalid: false });

    const outcome = await result;
    expect(outcome).toEqual({
      matches: [{ target: CORPUS[1], start: 0, end: 6 }],
      truncated: false,
      invalid: false,
      tooSlow: false,
    });
    expect(outcome?.matches[0].target).toBe(CORPUS[1]);
    expect(worker.terminated).toBe(true);
  });

  it("stops the worker and resolves null when the caller aborts", async () => {
    const controller = new AbortController();
    const result = searchPatternInWorker(CORPUS, "(a+)+$", OPTIONS, controller.signal);
    controller.abort();

    await expect(result).resolves.toBeNull();
    expect(FakeWorker.instances[0].terminated).toBe(true);
  });
});
