import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  PATTERN_TIME_BUDGET_MS,
  searchPatternInWorker,
  WORKER_START_TIMEOUT_MS,
} from "./pullRequestPatternSearch";
import type { PatchMatchTarget } from "./pullRequestReview";

// jsdom has no workers, so the runner is driven through a fake that the test answers by hand.
class FakeWorker {
  static instances: FakeWorker[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessageerror: ((event: Event) => void) | null = null;
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
const NO_MATCHES = { matches: [], truncated: false, invalid: false, tooSlow: false, failed: false };

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
  it("maps the worker's match indices back to the caller's corpus targets", async () => {
    const result = searchPatternInWorker(CORPUS, "needle", OPTIONS);
    const [worker] = FakeWorker.instances;
    expect(worker.posted).toEqual([{ corpus: CORPUS, query: "needle", options: OPTIONS }]);

    worker.reply({ started: true });
    worker.reply({ matches: [{ index: 1, start: 0, end: 6 }], truncated: false, invalid: false });

    const outcome = await result;
    expect(outcome).toEqual({
      matches: [{ target: CORPUS[1], start: 0, end: 6 }],
      truncated: false,
      invalid: false,
      tooSlow: false,
      failed: false,
    });
    expect(outcome?.matches[0].target).toBe(CORPUS[1]);
    expect(worker.terminated).toBe(true);
  });

  it("reports too slow and terminates the worker when matching outlasts the budget", async () => {
    const result = searchPatternInWorker(CORPUS, "(a+)+$", OPTIONS);
    FakeWorker.instances[0].reply({ started: true });
    vi.advanceTimersByTime(PATTERN_TIME_BUDGET_MS - 1);
    expect(FakeWorker.instances[0].terminated).toBe(false);
    vi.advanceTimersByTime(1);

    await expect(result).resolves.toEqual({ ...NO_MATCHES, tooSlow: true });
    expect(FakeWorker.instances[0].terminated).toBe(true);
  });

  it("gives matching its full budget after a delayed start, and keeps the request pending before the start deadline", async () => {
    const result = searchPatternInWorker(CORPUS, "(a+)+$", OPTIONS);
    vi.advanceTimersByTime(WORKER_START_TIMEOUT_MS - 1);
    expect(FakeWorker.instances[0].terminated).toBe(false);
    FakeWorker.instances[0].reply({ started: true });
    vi.advanceTimersByTime(PATTERN_TIME_BUDGET_MS - 1);
    expect(FakeWorker.instances[0].terminated).toBe(false);
    vi.advanceTimersByTime(1);

    await expect(result).resolves.toEqual({ ...NO_MATCHES, tooSlow: true });
  });

  it("reports a failed search when the worker never starts within the start budget", async () => {
    const result = searchPatternInWorker(CORPUS, "(a+)+$", OPTIONS);
    vi.advanceTimersByTime(WORKER_START_TIMEOUT_MS - 1);
    expect(FakeWorker.instances[0].terminated).toBe(false);
    vi.advanceTimersByTime(1);

    await expect(result).resolves.toEqual({ ...NO_MATCHES, failed: true });
    expect(FakeWorker.instances[0].terminated).toBe(true);
  });

  it("stops the worker, and ignores a reply that arrives after the caller aborts", async () => {
    const controller = new AbortController();
    const result = searchPatternInWorker(CORPUS, "needle", OPTIONS, controller.signal);
    const [worker] = FakeWorker.instances;
    controller.abort();
    worker.reply({ started: true });
    worker.reply({ matches: [{ index: 1, start: 0, end: 6 }], truncated: false, invalid: false });

    await expect(result).resolves.toBeNull();
    expect(worker.terminated).toBe(true);
  });

  it("creates no worker when the signal is already aborted", async () => {
    const controller = new AbortController();
    controller.abort();

    await expect(
      searchPatternInWorker(CORPUS, "x", OPTIONS, controller.signal),
    ).resolves.toBeNull();
    expect(FakeWorker.instances).toHaveLength(0);
  });

  it("reports a failed search when the worker raises an error", async () => {
    const result = searchPatternInWorker(CORPUS, "needle", OPTIONS);
    FakeWorker.instances[0].onerror?.(new Event("error"));

    await expect(result).resolves.toEqual({ ...NO_MATCHES, failed: true });
    expect(FakeWorker.instances[0].terminated).toBe(true);
  });

  it("reports a failed search when a reply cannot be deserialized", async () => {
    const result = searchPatternInWorker(CORPUS, "needle", OPTIONS);
    FakeWorker.instances[0].onmessageerror?.(new Event("messageerror"));

    await expect(result).resolves.toEqual({ ...NO_MATCHES, failed: true });
    expect(FakeWorker.instances[0].terminated).toBe(true);
  });

  it("reports a failed search when the corpus cannot be posted to the worker", async () => {
    vi.stubGlobal(
      "Worker",
      class UncloneableWorker extends FakeWorker {
        override postMessage(): never {
          throw new DOMException("could not be cloned", "DataCloneError");
        }
      },
    );

    await expect(searchPatternInWorker(CORPUS, "needle", OPTIONS)).resolves.toEqual({
      ...NO_MATCHES,
      failed: true,
    });
    expect(FakeWorker.instances[0].terminated).toBe(true);
  });

  it("reports a failed search when the browser refuses to start the worker", async () => {
    vi.stubGlobal("Worker", function BlockedWorker(): never {
      throw new DOMException("blocked by worker-src", "SecurityError");
    });

    await expect(searchPatternInWorker(CORPUS, "needle", OPTIONS)).resolves.toEqual({
      ...NO_MATCHES,
      failed: true,
    });
  });

  it("ignores a reply that arrives after the start timeout", async () => {
    const result = searchPatternInWorker(CORPUS, "(a+)+$", OPTIONS);
    const [worker] = FakeWorker.instances;
    vi.advanceTimersByTime(WORKER_START_TIMEOUT_MS);
    worker.reply({ started: true });
    worker.reply({ matches: [], truncated: false, invalid: false });

    await expect(result).resolves.toEqual({ ...NO_MATCHES, failed: true });
    vi.advanceTimersByTime(PATTERN_TIME_BUDGET_MS);
    await expect(result).resolves.toEqual({ ...NO_MATCHES, failed: true });
  });

  it("ignores a reply that arrives after the matching budget ran out", async () => {
    const result = searchPatternInWorker(CORPUS, "(a+)+$", OPTIONS);
    const [worker] = FakeWorker.instances;
    worker.reply({ started: true });
    vi.advanceTimersByTime(PATTERN_TIME_BUDGET_MS);
    worker.reply({ matches: [], truncated: false, invalid: false });

    await expect(result).resolves.toEqual({ ...NO_MATCHES, tooSlow: true });
  });
});
