// The window's own shared pool, started ahead of a review by whatever mounts the warm-up.

import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as SharedDiffWorkersModule from "./sharedDiffWorkers";

const pools = vi.hoisted(() => ({ made: 0 }));

// Pierre's manager, as a pool whose start never answers: what matters is how many are made.
vi.mock("@pierre/diffs/worker", () => ({
  WorkerPoolManager: class {
    constructor() {
      pools.made += 1;
    }
    initialize = () => new Promise<void>(() => {});
    isWorkingPool = () => true;
    terminate = () => {};
  },
}));

let shared: typeof SharedDiffWorkersModule;

function Warming() {
  shared.useWarmSharedDiffWorkers();
  return null;
}

/** Let the idle slice the warm-up waits for run, well inside the pool's startup time. */
async function idle() {
  await act(async () => {
    vi.advanceTimersByTime(10);
  });
}

beforeEach(async () => {
  vi.useFakeTimers();
  // jsdom has no workers; the window's pool exists only where they do.
  vi.stubGlobal("Worker", function Worker() {});
  vi.stubGlobal("requestIdleCallback", undefined);
  vi.resetModules();
  pools.made = 0;
  shared = await import("./sharedDiffWorkers");
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("warming the shared diff workers", () => {
  it("starts one pool in an idle moment, however many lists and details mount", async () => {
    const first = render(<Warming />);
    expect(pools.made).toBe(0);
    await idle();
    expect(pools.made).toBe(1);
    expect(shared.sharedDiffWorkers?.status()).toBe("starting");

    // Another list, a PR's details, and the first list again: the same pool.
    render(
      <>
        <Warming />
        <Warming />
      </>,
    );
    first.unmount();
    render(<Warming />);
    await idle();
    expect(pools.made).toBe(1);
  });

  it("starts none for a list that leaves before the idle moment", async () => {
    render(<Warming />).unmount();
    await idle();
    expect(pools.made).toBe(0);
  });
});
