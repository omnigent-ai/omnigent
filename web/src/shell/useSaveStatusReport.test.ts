import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SAVED_BADGE_MS, saveStatusOf, useSaveStatusReport } from "./useSaveStatusReport";

const quiet = {
  writePending: false,
  writeError: false,
  writeSuccess: false,
  saveDisabled: false,
  isDirty: false,
};

describe("saveStatusOf", () => {
  it.each([
    ["a clean editor", {}, "idle"],
    ["unsaved edits", { isDirty: true }, "unsaved"],
    ["a save in flight", { isDirty: true, writePending: true }, "saving"],
    ["a failed save with edits left", { isDirty: true, writeError: true }, "error"],
    ["a failed save reverted to clean", { writeError: true }, "idle"],
    ["edits while saving is unavailable", { isDirty: true, saveDisabled: true }, "offline"],
    ["a finished save", { writeSuccess: true }, "saved"],
    ["edits after a finished save", { writeSuccess: true, isDirty: true }, "unsaved"],
  ])("reports %s", (_label, inputs, expected) => {
    expect(saveStatusOf({ ...quiet, ...inputs })).toBe(expected);
  });
});

describe("useSaveStatusReport", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  type Inputs = typeof quiet;
  function report(onChange: (status: string) => void, inputs: Inputs = quiet) {
    return renderHook(({ current, callback }) => useSaveStatusReport(current, callback), {
      initialProps: { current: inputs, callback: onChange },
    });
  }

  it("clears Saved after a moment, and a second save shows it again for its full time", () => {
    const onChange = vi.fn();
    const hook = report(onChange, { ...quiet, writeSuccess: true });
    expect(onChange).toHaveBeenLastCalledWith("saved");

    vi.advanceTimersByTime(SAVED_BADGE_MS - 1);
    // A second save: the inputs move through a write and back to a finished one.
    hook.rerender({ current: { ...quiet, writePending: true }, callback: onChange });
    hook.rerender({ current: { ...quiet, writeSuccess: true }, callback: onChange });
    vi.advanceTimersByTime(SAVED_BADGE_MS - 1);
    expect(onChange).toHaveBeenLastCalledWith("saved");
    vi.advanceTimersByTime(1);
    expect(onChange).toHaveBeenLastCalledWith("idle");
  });

  it("reports to the latest callback, and clears the chip when the editor goes", () => {
    const first = vi.fn();
    const second = vi.fn();
    const hook = report(first, { ...quiet, writeSuccess: true });
    hook.rerender({ current: { ...quiet, writeSuccess: true }, callback: second });
    vi.advanceTimersByTime(SAVED_BADGE_MS);
    expect(second).toHaveBeenLastCalledWith("idle");
    expect(first).not.toHaveBeenCalledWith("idle");

    hook.rerender({ current: { ...quiet, isDirty: true, writePending: true }, callback: second });
    expect(second).toHaveBeenLastCalledWith("saving");
    hook.unmount();
    expect(second).toHaveBeenLastCalledWith("idle");
    // The unmounted editor's timer doesn't report afterwards.
    const calls = second.mock.calls.length;
    vi.advanceTimersByTime(SAVED_BADGE_MS);
    expect(second.mock.calls.length).toBe(calls);
  });
});
