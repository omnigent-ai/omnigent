import { afterEach, describe, expect, it, vi } from "vitest";
import { computeMarkdownDiff, TOO_LARGE_MESSAGE } from "./markdownDiff";
import {
  COMPUTE_FAILED_MESSAGE,
  createMarkdownDiffComputer,
  isAbortError,
  TIMED_OUT_MESSAGE,
  type MarkdownDiffWorkerRequest,
  type MarkdownDiffWorkerResponse,
} from "./markdownDiffComputation";

class FakeWorker {
  onmessage: ((event: MessageEvent<MarkdownDiffWorkerResponse>) => void) | null = null;
  onerror: (() => void) | null = null;
  terminate = vi.fn();
  posted: MarkdownDiffWorkerRequest[] = [];
  postMessage(message: MarkdownDiffWorkerRequest) {
    this.posted.push(message);
  }
  reply(response: MarkdownDiffWorkerResponse) {
    this.onmessage?.({ data: response } as MessageEvent<MarkdownDiffWorkerResponse>);
  }
  answer(index = this.posted.length - 1) {
    const { id, request } = this.posted[index]!;
    this.reply({ id, result: computeMarkdownDiff(request.before, request.after) });
  }
}

function workerPool() {
  const workers: FakeWorker[] = [];
  const factory = () => {
    const worker = new FakeWorker();
    workers.push(worker);
    return worker as unknown as Worker;
  };
  return { workers, factory };
}

const edit = { before: "Old text.", after: "New text." };

afterEach(() => {
  vi.useRealTimers();
});

describe("createMarkdownDiffComputer", () => {
  it("runs one diff at a time and resolves each with its own result", async () => {
    const { workers, factory } = workerPool();
    const computer = createMarkdownDiffComputer(factory);
    const first = computer.compute(edit);
    const second = computer.compute({ before: null, after: "Added." });
    expect(workers[0]!.posted).toHaveLength(1);
    workers[0]!.answer();
    await expect(first).resolves.toMatchObject({ status: "ready" });
    expect(workers[0]!.posted).toHaveLength(2);
    workers[0]!.answer();
    await expect(second).resolves.toMatchObject({
      status: "ready",
      sections: [{ kind: "added", text: "Added." }],
    });
  });

  it("ignores a reply for a diff that is no longer running", async () => {
    const { workers, factory } = workerPool();
    const computer = createMarkdownDiffComputer(factory);
    const pending = computer.compute(edit);
    workers[0]!.reply({ id: 99, error: "stale" });
    workers[0]!.answer();
    await expect(pending).resolves.toMatchObject({ status: "ready" });
  });

  it("skips a diff cancelled while it waits", async () => {
    const { workers, factory } = workerPool();
    const computer = createMarkdownDiffComputer(factory);
    const running = computer.compute(edit);
    const controller = new AbortController();
    const cancelled = computer.compute(edit, controller.signal);
    controller.abort();
    await expect(cancelled).rejects.toSatisfy(isAbortError);
    workers[0]!.answer();
    await running;
    expect(workers[0]!.posted).toHaveLength(1);
  });

  it("stops the worker for a cancelled running diff and starts the next on a new one", async () => {
    const { workers, factory } = workerPool();
    const computer = createMarkdownDiffComputer(factory);
    const controller = new AbortController();
    const cancelled = computer.compute(edit, controller.signal);
    const next = computer.compute(edit);
    controller.abort();
    await expect(cancelled).rejects.toSatisfy(isAbortError);
    expect(workers[0]!.terminate).toHaveBeenCalled();
    workers[1]!.answer();
    await expect(next).resolves.toMatchObject({ status: "ready" });
  });

  it("gives up on a diff that runs too long, stopping its worker", async () => {
    vi.useFakeTimers();
    const { workers, factory } = workerPool();
    const computer = createMarkdownDiffComputer(factory, 1_000);
    const slow = computer.compute(edit);
    const next = computer.compute(edit);
    await vi.advanceTimersByTimeAsync(1_000);
    await expect(slow).resolves.toEqual({ status: "tooLarge", message: TIMED_OUT_MESSAGE });
    expect(workers[0]!.terminate).toHaveBeenCalled();
    workers[1]!.answer();
    await expect(next).resolves.toMatchObject({ status: "ready" });
  });

  it("fails the running diff when the worker crashes or reports an error", async () => {
    const { workers, factory } = workerPool();
    const computer = createMarkdownDiffComputer(factory);
    const crashed = computer.compute(edit);
    const reported = computer.compute(edit);
    workers[0]!.onerror?.();
    await expect(crashed).rejects.toThrow(COMPUTE_FAILED_MESSAGE);
    const { id } = workers[1]!.posted[0]!;
    workers[1]!.reply({ id, error: "parse failure" });
    await expect(reported).rejects.toThrow(COMPUTE_FAILED_MESSAGE);
  });

  it("falls back to the page, with its smaller limits, when a worker can't start", async () => {
    const computer = createMarkdownDiffComputer(() => {
      throw new Error("Workers are blocked");
    });
    await expect(computer.compute(edit)).resolves.toMatchObject({ status: "ready" });
    await expect(computer.compute({ before: null, after: "x".repeat(100_001) })).resolves.toEqual({
      status: "tooLarge",
      message: TOO_LARGE_MESSAGE,
    });
  });

  it("computes on the page without workers", async () => {
    const computer = createMarkdownDiffComputer(null);
    await expect(computer.compute(edit)).resolves.toMatchObject({ status: "ready" });
  });

  it("cancels everything when disposed", async () => {
    const { workers, factory } = workerPool();
    const computer = createMarkdownDiffComputer(factory);
    const running = computer.compute(edit);
    const waiting = computer.compute(edit);
    computer.dispose();
    await expect(running).rejects.toSatisfy(isAbortError);
    await expect(waiting).rejects.toSatisfy(isAbortError);
    expect(workers[0]!.terminate).toHaveBeenCalled();
    await expect(computer.compute(edit)).rejects.toSatisfy(isAbortError);
  });
});
