import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createListReconcileScheduler,
  RECONCILE_DEBOUNCE_MS,
  RECONCILE_MAX_GAP_MS,
  RECONCILE_MIN_GAP_MS,
  RECONCILE_ROUND_TIMEOUT_MS,
  RECONCILE_WHILE_ACTIVE_MS,
  type ReconcileScope,
} from "./listReconcileScheduler";

/** A round whose completion the test controls. */
function controllableRun() {
  const resolvers: (() => void)[] = [];
  const run = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        resolvers.push(resolve);
      }),
  );
  return { run, finish: () => resolvers.shift()?.() };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("createListReconcileScheduler", () => {
  it("collapses a burst of requests into one round after the debounce", async () => {
    const run = vi.fn(async () => {});
    const scheduler = createListReconcileScheduler(run);

    for (let i = 0; i < 10; i += 1) scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS - 1);
    expect(run).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("never overlaps rounds and runs one trailing round for requests made mid-round", async () => {
    const { run, finish } = controllableRun();
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    expect(run).toHaveBeenCalledTimes(1);

    // Frames keep arriving while the first round is still waiting on the server.
    for (let i = 0; i < 5; i += 1) scheduler.schedule();
    await vi.advanceTimersByTimeAsync(500);
    expect(run).toHaveBeenCalledTimes(1);

    finish();
    await vi.advanceTimersByTimeAsync(RECONCILE_MIN_GAP_MS);
    expect(run).toHaveBeenCalledTimes(2);
    finish();
    await vi.advanceTimersByTimeAsync(RECONCILE_MAX_GAP_MS);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("spaces rounds by at least the minimum gap", async () => {
    const run = vi.fn(async () => {});
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_MIN_GAP_MS - 1);
    expect(run).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("waits twice as long as a slow round took before the next one", async () => {
    const { run, finish } = controllableRun();
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    await vi.advanceTimersByTimeAsync(5_000); // the server took 5 s to answer
    finish();
    await vi.advanceTimersByTimeAsync(0);

    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(10_000 - 1);
    expect(run).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("caps the spacing however slow the last round was", async () => {
    const { run, finish } = controllableRun();
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    await vi.advanceTimersByTimeAsync(40_000);
    finish();
    await vi.advanceTimersByTimeAsync(0);

    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_MAX_GAP_MS - 1);
    expect(run).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("stops holding back new rounds behind one that never settles", async () => {
    const run = vi.fn(() => new Promise<void>(() => {}));
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_ROUND_TIMEOUT_MS + RECONCILE_MAX_GAP_MS - 1);
    expect(run).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("keeps scheduling after a round fails", async () => {
    const run = vi.fn(async () => {
      throw new Error("500");
    });
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_MIN_GAP_MS);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("runs a round for activity only once the last round is a minute old", async () => {
    const run = vi.fn(async () => {});
    const scheduler = createListReconcileScheduler(run);

    await vi.advanceTimersByTimeAsync(RECONCILE_WHILE_ACTIVE_MS - 1);
    scheduler.noteActivity();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    expect(run).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1);
    scheduler.noteActivity();
    scheduler.noteActivity();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    expect(run).toHaveBeenCalledTimes(1);

    // That round restarts the minute.
    await vi.advanceTimersByTimeAsync(RECONCILE_WHILE_ACTIVE_MS - RECONCILE_DEBOUNCE_MS - 1);
    scheduler.noteActivity();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("counts a requested round toward the activity minute", async () => {
    const run = vi.fn(async () => {});
    const scheduler = createListReconcileScheduler(run);

    await vi.advanceTimersByTimeAsync(RECONCILE_WHILE_ACTIVE_MS / 2);
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    await vi.advanceTimersByTimeAsync(RECONCILE_WHILE_ACTIVE_MS / 2);
    scheduler.noteActivity();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("never runs a round requested before disposal", async () => {
    const run = vi.fn(async () => {});
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule();
    scheduler.dispose();
    await vi.advanceTimersByTimeAsync(RECONCILE_MAX_GAP_MS);
    expect(run).not.toHaveBeenCalled();
  });

  it("drops pending work once disposed", async () => {
    const { run, finish } = controllableRun();
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    scheduler.schedule(); // trailing round requested mid-round
    scheduler.dispose();
    finish();
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(RECONCILE_MAX_GAP_MS);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("runs one round over the union of the lists requested before it", async () => {
    const run = vi.fn(async (_scope: ReconcileScope) => {});
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule(new Set(["a"]));
    scheduler.schedule(new Set(["b"]));
    scheduler.schedule(new Set());
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenLastCalledWith(new Set(["a", "b"]));
  });

  it("widens a round to every list when any request asks for every list", async () => {
    const run = vi.fn(async (_scope: ReconcileScope) => {});
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule(new Set(["a"]));
    scheduler.schedule();
    scheduler.schedule(new Set(["b"]));
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    expect(run).toHaveBeenLastCalledWith("all");
  });

  it("ignores a request that names no lists", async () => {
    const run = vi.fn(async () => {});
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule(new Set());
    await vi.advanceTimersByTimeAsync(RECONCILE_MAX_GAP_MS);
    expect(run).not.toHaveBeenCalled();
  });

  it("gives the trailing round only the lists requested mid-round", async () => {
    const { run, finish } = controllableRun();
    const scheduler = createListReconcileScheduler(run);

    scheduler.schedule(new Set(["a"]));
    await vi.advanceTimersByTimeAsync(RECONCILE_DEBOUNCE_MS);
    scheduler.schedule(new Set(["b"]));
    scheduler.schedule(new Set(["c"]));
    finish();
    await vi.advanceTimersByTimeAsync(RECONCILE_MIN_GAP_MS);
    expect(run).toHaveBeenCalledTimes(2);
    expect(run).toHaveBeenNthCalledWith(1, new Set(["a"]));
    expect(run).toHaveBeenNthCalledWith(2, new Set(["b", "c"]));
  });

  it("still runs the activity round over every list after rounds over named lists", async () => {
    const run = vi.fn(async (_scope: ReconcileScope) => {});
    const scheduler = createListReconcileScheduler(run);

    await vi.advanceTimersByTimeAsync(RECONCILE_WHILE_ACTIVE_MS / 2);
    scheduler.schedule(new Set(["a"]));
    await vi.advanceTimersByTimeAsync(RECONCILE_WHILE_ACTIVE_MS / 2);
    scheduler.noteActivity();
    await vi.advanceTimersByTimeAsync(RECONCILE_MIN_GAP_MS);
    expect(run).toHaveBeenCalledTimes(2);
    expect(run).toHaveBeenLastCalledWith("all");
  });
});
