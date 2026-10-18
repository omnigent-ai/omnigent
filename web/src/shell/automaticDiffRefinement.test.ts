import { afterEach, describe, expect, it, vi } from "vitest";
import { attachAutomaticDiffRefinement } from "./automaticDiffRefinement";

function setup(initial: boolean | null = null) {
  vi.useFakeTimers();
  let result = initial === null ? null : { quitEarly: initial };
  let updated = () => {};
  let changed = () => {};
  const dispose = vi.fn();
  const editor = {
    getDiffComputationResult: () => result,
    updateOptions: vi.fn(),
    onDidUpdateDiff: (fn: () => void) => {
      updated = fn;
      return { dispose };
    },
    getModel: () => ({
      original: { onDidChangeContent: () => ({ dispose }) },
      modified: {
        onDidChangeContent: (fn: () => void) => {
          changed = fn;
          return { dispose };
        },
      },
    }),
  };
  const status = vi.fn();
  const stop = attachAutomaticDiffRefinement(
    editor as unknown as Parameters<typeof attachAutomaticDiffRefinement>[0],
    status,
  );
  return {
    editor,
    status,
    stop,
    changed: () => changed(),
    update: (quitEarly: boolean) => {
      result = { quitEarly };
      updated();
    },
  };
}

afterEach(() => vi.useRealTimers());

describe("automatic diff refinement", () => {
  it("leaves completed diffs alone", () => {
    const h = setup();
    h.update(false);
    vi.runAllTimers();
    expect(h.editor.updateOptions).not.toHaveBeenCalled();
    expect(h.status).toHaveBeenLastCalledWith("idle");
    h.stop();
  });

  it.each([false, true])("retries once and reports the final timeout=%s", (timeout) => {
    const h = setup(true);
    expect(h.status).toHaveBeenLastCalledWith("refining");
    expect(h.editor.updateOptions).not.toHaveBeenCalled();
    vi.runAllTimers();
    expect(h.editor.updateOptions).toHaveBeenCalledExactlyOnceWith({ maxComputationTime: 10000 });
    h.update(timeout);
    vi.runAllTimers();
    expect(h.editor.updateOptions).toHaveBeenCalledTimes(1);
    expect(h.status).toHaveBeenLastCalledWith(timeout ? "limited" : "idle");
    h.stop();
  });

  it("cancels pending refinement on disposal and ignores late results", () => {
    const h = setup();
    h.update(true);
    h.stop();
    h.status.mockClear();
    vi.runAllTimers();
    h.update(false);
    expect(h.editor.updateOptions).not.toHaveBeenCalled();
    expect(h.status).not.toHaveBeenCalled();
  });

  it("starts a fresh budget after content changes", () => {
    const h = setup(true);
    h.changed();
    vi.runAllTimers();
    expect(h.editor.updateOptions).toHaveBeenCalledExactlyOnceWith({ maxComputationTime: 1000 });
    h.update(true);
    vi.runAllTimers();
    expect(h.editor.updateOptions).toHaveBeenLastCalledWith({ maxComputationTime: 10000 });
    h.stop();
  });
});
