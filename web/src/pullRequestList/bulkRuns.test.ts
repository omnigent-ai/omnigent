import { beforeEach, describe, expect, it, vi } from "vitest";

const { toastMock } = vi.hoisted(() => ({ toastMock: vi.fn() }));
vi.mock("sonner", () => ({ toast: toastMock }));

import {
  bulkRunSnapshot,
  resetBulkRunsForTests,
  runningBulkRuns,
  startBulkRun,
  stopBulkRun,
  subscribeBulkRuns,
} from "./bulkRuns";

interface ToastOptions {
  id: string;
  description?: string;
  duration?: number;
  closeButton?: boolean;
  action?: { label: string; onClick: (event: { preventDefault: () => void }) => void };
}
const lastToast = () => {
  const [title, options] = toastMock.mock.calls.at(-1) as [string, ToastOptions];
  return { title, ...options };
};

beforeEach(() => {
  toastMock.mockReset();
  resetBulkRunsForTests();
});

describe("bulk runs", () => {
  it("updates one toast in place, then turns it into the summary", () => {
    const run = startBulkRun({ label: "Removing pull requests", total: 3 });
    expect(lastToast()).toMatchObject({
      title: "Removing pull requests",
      description: "0 of 3 done",
      action: { label: "Stop" },
    });
    run.progress({ done: 2, failed: 1 });
    expect(lastToast()).toMatchObject({ description: "2 of 3 done · 1 failed" });

    run.finish({ title: "Removed 2 pull requests", detail: "1 couldn't be removed." });
    expect(lastToast()).toMatchObject({
      title: "Removed 2 pull requests",
      description: "1 couldn't be removed.",
      action: undefined,
    });
    expect(new Set(toastMock.mock.calls.map(([, options]) => options.id))).toEqual(
      new Set([`bulk-run:${run.id}`]),
    );
    expect(runningBulkRuns()).toEqual([]);
  });

  it("keeps a summary that needs a look open until it's closed", () => {
    const run = startBulkRun({ label: "Approving", total: 1 });
    run.finish({ title: "GitHub refused 1", needsLook: true });
    expect(lastToast()).toMatchObject({ duration: Number.POSITIVE_INFINITY, closeButton: true });
  });

  it("stops from the toast, keeps the toast, and ignores progress after the end", () => {
    const run = startBulkRun({ label: "Removing", total: 5 });
    const prevented = vi.fn();
    lastToast().action!.onClick({ preventDefault: prevented });
    expect(prevented).toHaveBeenCalled();
    expect(run.stopRequested()).toBe(true);
    expect(lastToast().action).toBeUndefined();

    run.finish({ title: "Stopped after 2" });
    const calls = toastMock.mock.calls.length;
    run.progress({ done: 4 });
    run.finish({ title: "Again" });
    stopBulkRun(run.id);
    expect(toastMock.mock.calls).toHaveLength(calls);
  });

  it("tells subscribers about each run and its progress", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeBulkRuns(listener);
    const first = startBulkRun({ label: "First", total: 2 });
    startBulkRun({ label: "Second", total: 1 });
    first.progress({ done: 1 });
    expect(runningBulkRuns().map((each) => [each.label, each.done])).toEqual([
      ["First", 1],
      ["Second", 0],
    ]);
    expect(listener).toHaveBeenCalledTimes(3);
    unsubscribe();
    first.finish({ title: "Done" });
    expect(listener).toHaveBeenCalledTimes(3);
  });

  it("keeps a run's snapshot the same object until it changes, and says when Stop was asked", () => {
    const run = startBulkRun({ label: "Removing", total: 3 });
    const first = bulkRunSnapshot(run.id);
    expect(first).toMatchObject({ done: 0, total: 3, stopRequested: false });
    expect(bulkRunSnapshot(run.id)).toBe(first);
    run.progress({ done: 1 });
    const second = bulkRunSnapshot(run.id);
    expect(second).not.toBe(first);
    expect(second).toMatchObject({ done: 1 });
    stopBulkRun(run.id);
    expect(bulkRunSnapshot(run.id)).toMatchObject({ done: 1, stopRequested: true });
  });

  it("tells subscribers once more after a run is gone, so a reader by id sees it ended", () => {
    const run = startBulkRun({ label: "Removing", total: 1 });
    const seen: (string | null)[] = [];
    const unsubscribe = subscribeBulkRuns(() => seen.push(bulkRunSnapshot(run.id)?.state ?? null));
    run.finish({ title: "Removed 1 pull request" });
    unsubscribe();
    expect(seen).toEqual(["finished", null]);
    expect(bulkRunSnapshot(run.id)).toBeNull();
  });
});
