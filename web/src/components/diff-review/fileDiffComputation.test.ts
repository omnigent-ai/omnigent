import { describe, expect, it, vi } from "vitest";
import {
  computeFileDiff,
  createFileDiffComputer,
  type FileDiffWorkerRequest,
  type FileDiffWorkerResponse,
} from "./fileDiffComputation";

const request = (before: string | null, after: string | null, ignoreWhitespace = false) => ({
  path: "src/app.ts",
  before,
  after,
  ignoreWhitespace,
  revision: "r1",
});

describe("computeFileDiff", () => {
  it("diffs added, deleted, and modified files with full contents", () => {
    expect(computeFileDiff(request(null, "a\nb\n")).type).toBe("new");
    expect(computeFileDiff(request("a\n", null)).type).toBe("deleted");
    const modified = computeFileDiff(request("a\nb\n", "a\nB\n"));
    expect(modified.type).toBe("change");
    expect(modified.isPartial).toBe(false);
    expect(modified.additionLines).toEqual(["a\n", "B\n"]);
    expect(modified.lang).toBe("typescript");
  });

  it("keys the renderer's caches by the text, not only the revision it was given", () => {
    const key = (before: string | null, after: string | null) =>
      computeFileDiff(request(before, after)).cacheKey;
    // The same revision with different text, as two sessions deleting a same-named file give.
    expect(key("const one = 1;\n", null)).not.toBe(key("const two = 2;\n", null));
    expect(key("a\n", "b\n")).not.toBe(key("a\n", "c\n"));
    expect(key("a\n", "b\n")).not.toBe(key("x\n", "b\n"));
    // The same text keeps its key, so a review reopened on it reuses what was drawn.
    expect(key("a\n", "b\n")).toBe(key("a\n", "b\n"));
    expect(key("a\n", "b\n")).toMatch(/^r1:before:/);
  });

  it("drops leading and trailing whitespace changes when asked, like Monaco", () => {
    expect(computeFileDiff(request("  a\nb \n", "a\nb\n")).hunks).not.toHaveLength(0);
    expect(computeFileDiff(request("  a\nb \n", "a\nb\n", true)).hunks).toHaveLength(0);
  });

  it("diffs a million-line file", () => {
    const after = Array.from({ length: 1_000_000 }, (_, line) => `export const value${line} = 1;`)
      .join("\n")
      .concat("\n");
    const fileDiff = computeFileDiff(request(null, after));
    expect(fileDiff.additionLines).toHaveLength(1_000_000);
    expect(fileDiff.unifiedLineCount).toBeGreaterThanOrEqual(1_000_000);
    // Seconds on a fast machine, several times that under coverage.
  }, 60_000);
});

class FakeWorker {
  onmessage: ((event: MessageEvent<FileDiffWorkerResponse>) => void) | null = null;
  onerror: (() => void) | null = null;
  terminate = vi.fn();
  posted: FileDiffWorkerRequest[] = [];
  postMessage(message: FileDiffWorkerRequest) {
    this.posted.push(message);
  }
  reply(response: FileDiffWorkerResponse) {
    this.onmessage?.({ data: response } as MessageEvent<FileDiffWorkerResponse>);
  }
}

describe("createFileDiffComputer", () => {
  it("computes in place without workers", async () => {
    const computer = createFileDiffComputer(null);
    await expect(computer.compute(request("a\n", "b\n"))).resolves.toMatchObject({
      type: "change",
    });
  });

  it("matches worker replies to their requests", async () => {
    const worker = new FakeWorker();
    const computer = createFileDiffComputer(() => worker as unknown as Worker);
    const first = computer.compute(request("a\n", "b\n"));
    const second = computer.compute(request("c\n", "d\n"));
    const [one, two] = worker.posted;
    worker.reply({ id: two!.id, error: "too big" });
    worker.reply({ id: one!.id, fileDiff: computeFileDiff(one!.request) });
    await expect(first).resolves.toMatchObject({ type: "change" });
    await expect(second).rejects.toThrow("too big");
  });

  it("rejects work whose worker can't start, and starts one for the next request", async () => {
    const workers: FakeWorker[] = [];
    let starts = 0;
    const computer = createFileDiffComputer(() => {
      starts += 1;
      if (starts === 1) throw new Error("Worker blocked");
      const worker = new FakeWorker();
      workers.push(worker);
      return worker as unknown as Worker;
    });
    await expect(computer.compute(request("a\n", "b\n"))).rejects.toThrow("Worker blocked");
    const next = computer.compute(request("a\n", "b\n"));
    expect(workers).toHaveLength(1);
    computer.dispose();
    await expect(next).rejects.toThrow("closed");
  });

  it("rejects pending work when the worker crashes, then starts a new one", async () => {
    const workers: FakeWorker[] = [];
    const computer = createFileDiffComputer(() => {
      const worker = new FakeWorker();
      workers.push(worker);
      return worker as unknown as Worker;
    });
    const pending = computer.compute(request("a\n", "b\n"));
    workers[0]!.onerror?.();
    await expect(pending).rejects.toThrow("couldn't be computed");
    const discarded = computer.compute(request("a\n", "b\n"));
    expect(workers).toHaveLength(2);
    computer.dispose();
    await expect(discarded).rejects.toThrow("closed");
    expect(workers[1]!.terminate).toHaveBeenCalled();
  });
});
