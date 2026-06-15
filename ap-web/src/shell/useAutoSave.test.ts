// Unit tests for useAutoSave — the debounce / single-flight / trailing-save
// engine behind markdown auto-save. Exercised in isolation (no TipTap) with
// fake timers so the timing invariants are deterministic.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";
import { useAutoSave } from "./useAutoSave";

type SaveFn = (content: string) => Promise<void>;

const DELAY = 1000;

// Mutable control object: getContent/isDirty close over it, so mutating its
// fields is visible to the hook without a rerender. `enabled` is read by value
// each render, so changing it requires rerender(ctl).
interface Ctl {
  enabled: boolean;
  dirty: boolean;
  content: string;
  save: Mock<SaveFn>;
}

function makeCtl(overrides: Partial<Ctl> = {}): Ctl {
  return {
    enabled: true,
    dirty: true,
    content: "v1",
    // Resolves immediately by default; individual tests override for
    // single-flight scenarios where the promise must be held open.
    save: vi.fn<SaveFn>(() => Promise.resolve()),
    ...overrides,
  };
}

function renderAutoSave(ctl: Ctl) {
  return renderHook(
    (c: Ctl) =>
      useAutoSave({
        delayMs: DELAY,
        enabled: c.enabled,
        getContent: () => c.content,
        isDirty: () => c.dirty,
        save: c.save,
      }),
    { initialProps: ctl },
  );
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useAutoSave debounce", () => {
  it("saves once after the debounce delay elapses", () => {
    const ctl = makeCtl({ content: "edited" });
    const { result } = renderAutoSave(ctl);

    act(() => {
      result.current.schedule();
    });
    // One tick before the deadline: nothing should have fired yet — proves
    // the write is debounced, not immediate.
    act(() => {
      vi.advanceTimersByTime(DELAY - 1);
    });
    expect(ctl.save).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    // save() is invoked synchronously inside the timer callback (before its
    // first await), so it has been called exactly once with the live content.
    expect(ctl.save).toHaveBeenCalledTimes(1);
    expect(ctl.save).toHaveBeenCalledWith("edited");
  });

  it("coalesces a burst of edits into a single save", () => {
    const ctl = makeCtl();
    const { result } = renderAutoSave(ctl);

    // Three rapid edits, each re-arming the timer well within the window.
    act(() => {
      result.current.schedule();
      vi.advanceTimersByTime(400);
    });
    act(() => {
      result.current.schedule();
      vi.advanceTimersByTime(400);
    });
    act(() => {
      result.current.schedule();
      vi.advanceTimersByTime(400);
    });
    // 1200ms of wall time but only 400ms since the last edit → no save yet.
    expect(ctl.save).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(DELAY);
    });
    // Exactly one write for the whole burst — the debounce reset each time.
    expect(ctl.save).toHaveBeenCalledTimes(1);
  });

  it("does not save when disabled", () => {
    const ctl = makeCtl({ enabled: false });
    const { result } = renderAutoSave(ctl);

    act(() => {
      result.current.schedule();
      vi.advanceTimersByTime(DELAY);
    });
    // enabled=false (offline / conflict / read-only) suppresses the write
    // entirely — a failed assertion here means autosave clobbered a state
    // it was supposed to defer to.
    expect(ctl.save).not.toHaveBeenCalled();
  });

  it("does not save when the editor is clean", () => {
    const ctl = makeCtl({ dirty: false });
    const { result } = renderAutoSave(ctl);

    act(() => {
      result.current.schedule();
      vi.advanceTimersByTime(DELAY);
    });
    // No unsaved edits → no write. Guards against a spurious save on focus
    // churn that would echo back through the file-content query.
    expect(ctl.save).not.toHaveBeenCalled();
  });

  it("cancel() prevents a scheduled save from firing", () => {
    const ctl = makeCtl();
    const { result } = renderAutoSave(ctl);

    act(() => {
      result.current.schedule();
    });
    act(() => {
      result.current.cancel();
    });
    act(() => {
      vi.advanceTimersByTime(DELAY);
    });
    expect(ctl.save).not.toHaveBeenCalled();
  });

  it("clears the pending timer on unmount (no save after teardown)", () => {
    const ctl = makeCtl();
    const { result, unmount } = renderAutoSave(ctl);

    act(() => {
      result.current.schedule();
    });
    unmount();
    act(() => {
      vi.advanceTimersByTime(DELAY);
    });
    // The timer must be cleared on unmount; otherwise a fire-after-unmount
    // would write using a stale closure.
    expect(ctl.save).not.toHaveBeenCalled();
  });
});

describe("useAutoSave flush", () => {
  it("saves immediately and cancels the pending debounce (no double save)", () => {
    const ctl = makeCtl();
    const { result } = renderAutoSave(ctl);

    act(() => {
      result.current.schedule();
    });
    act(() => {
      result.current.flush();
    });
    expect(ctl.save).toHaveBeenCalledTimes(1);

    // The scheduled timer must have been cancelled by flush — advancing past
    // the delay must NOT produce a second write.
    act(() => {
      vi.advanceTimersByTime(DELAY);
    });
    expect(ctl.save).toHaveBeenCalledTimes(1);
  });

  it("is a no-op when not dirty", () => {
    const ctl = makeCtl({ dirty: false });
    const { result } = renderAutoSave(ctl);
    act(() => {
      result.current.flush();
    });
    expect(ctl.save).not.toHaveBeenCalled();
  });
});

describe("useAutoSave single-flight + trailing save", () => {
  it("does not start a second write while one is in flight, then saves the latest", async () => {
    // A save() whose promise is held open so we can observe overlap behaviour.
    let releaseFirst: (() => void) | null = null;
    const saved: string[] = [];
    const save = vi.fn<SaveFn>((content: string) => {
      saved.push(content);
      if (saved.length === 1) {
        return new Promise<void>((res) => {
          releaseFirst = () => res();
        });
      }
      return Promise.resolve();
    });
    const ctl = makeCtl({ content: "a", save });
    const { result } = renderAutoSave(ctl);

    // First flush starts save("a") — held open, in flight.
    act(() => {
      result.current.flush();
    });
    expect(save).toHaveBeenCalledTimes(1);

    // An edit lands and a second flush arrives while the first is in flight.
    // It must NOT start a concurrent write — it queues a trailing save.
    ctl.content = "b";
    act(() => {
      result.current.flush();
    });
    expect(save).toHaveBeenCalledTimes(1);

    // Release the first write → the trailing save runs once with the latest
    // content, and no further writes after that.
    await act(async () => {
      releaseFirst?.();
    });
    expect(save).toHaveBeenCalledTimes(2);
    expect(saved).toEqual(["a", "b"]);
  });

  it("skips the trailing save if the editor became clean meanwhile", async () => {
    let releaseFirst: (() => void) | null = null;
    const save = vi.fn<SaveFn>(
      () =>
        new Promise<void>((res) => {
          releaseFirst = () => res();
        }),
    );
    const ctl = makeCtl({ content: "a", save });
    const { result } = renderAutoSave(ctl);

    act(() => {
      result.current.flush();
    }); // save1 in flight
    act(() => {
      result.current.flush();
    }); // queues a trailing save
    expect(save).toHaveBeenCalledTimes(1);

    // The queued content got persisted by another path → editor is clean now.
    ctl.dirty = false;
    await act(async () => {
      releaseFirst?.();
    });
    // No trailing write: the trailing save re-checks dirtiness before running.
    expect(save).toHaveBeenCalledTimes(1);
  });
});
