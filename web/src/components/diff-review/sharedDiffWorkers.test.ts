import type { WorkerPoolManager } from "@pierre/diffs/worker";
import { describe, expect, it, vi } from "vitest";

import {
  createSharedDiffWorkers,
  SHARED_WORKERS_RETIRE_GRACE_MS,
  SHARED_WORKERS_STARTUP_MS,
} from "./sharedDiffWorkers";

/** A pool the test starts, fails or kills by hand. */
function fakePool() {
  let resolve: () => void = () => {};
  let reject: (error: Error) => void = () => {};
  const started = new Promise<void>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  started.catch(() => {});
  const state = { working: true, terminated: 0 };
  const pool = {
    initialize: vi.fn(() => started),
    isWorkingPool: () => state.working,
    terminate: vi.fn(() => {
      state.terminated += 1;
    }),
  } as unknown as WorkerPoolManager;
  return { pool, state, start: resolve, fail: reject };
}

/** A store whose pools come from `pools`, in order, on a clock the test moves. */
function store(...pools: ReturnType<typeof fakePool>[]) {
  let now = 0;
  const timers: { at: number; work: () => void }[] = [];
  const workerErrors: (() => void)[] = [];
  const queue = [...pools];
  const created: WorkerPoolManager[] = [];
  const shared = createSharedDiffWorkers({
    createPool: (onWorkerError) => {
      workerErrors.push(onWorkerError);
      const next = queue.shift();
      if (!next) throw new Error("no pool left to make");
      created.push(next.pool);
      return next.pool;
    },
    setTimer: (work, ms) => {
      const timer = { at: now + ms, work };
      timers.push(timer);
      return () => {
        const index = timers.indexOf(timer);
        if (index >= 0) timers.splice(index, 1);
      };
    },
  });
  const advance = (ms: number) => {
    now += ms;
    for (const timer of timers.filter((each) => each.at <= now)) {
      timers.splice(timers.indexOf(timer), 1);
      timer.work();
    }
  };
  return { shared, advance, created, workerErrors };
}

const flush = () => Promise.resolve().then(() => undefined);

describe("the shared diff workers", () => {
  it("starts one pool, hands it out only once it's started, and starts no second", async () => {
    const first = fakePool();
    const { shared, created } = store(first);
    expect(shared.status()).toBe("idle");
    shared.start();
    shared.start();
    expect(shared.status()).toBe("starting");
    expect(shared.peekReady()).toBeNull();
    first.start();
    await flush();
    expect(shared.status()).toBe("ready");
    expect(shared.peekReady()).toBe(first.pool);
    shared.start();
    expect(created).toEqual([first.pool]);
  });

  it("tells its listeners each change", async () => {
    const first = fakePool();
    const { shared } = store(first);
    const heard: string[] = [];
    shared.subscribe(() => heard.push(shared.status()));
    shared.start();
    first.start();
    await flush();
    expect(heard).toEqual(["starting", "ready"]);
  });

  it.each([
    ["rejects", (pool: ReturnType<typeof fakePool>) => pool.fail(new Error("no wasm"))],
    ["never answers", () => {}],
  ])("gives up on a start that %s, and the next review starts a fresh one", async (_case, act) => {
    const first = fakePool();
    const second = fakePool();
    const { shared, advance, created } = store(first, second);
    shared.start();
    act(first);
    await flush();
    advance(SHARED_WORKERS_STARTUP_MS);
    await flush();
    expect(shared.status()).toBe("failed");
    expect(shared.peekReady()).toBeNull();
    // It was never handed out, so it goes as soon as the grace period ends.
    advance(SHARED_WORKERS_RETIRE_GRACE_MS);
    expect(first.state.terminated).toBe(1);
    shared.start();
    second.start();
    await flush();
    expect(shared.peekReady()).toBe(second.pool);
    expect(created).toEqual([first.pool, second.pool]);
  });

  it("gives up on a start when a worker fails before it, as Pierre would only log", async () => {
    const first = fakePool();
    const { shared, workerErrors } = store(first);
    shared.start();
    workerErrors[0]!();
    expect(shared.status()).toBe("failed");
    // An answer arriving after it gave up changes nothing.
    first.start();
    await flush();
    expect(shared.status()).toBe("failed");
  });

  it("gives up on a pool Pierre marks failed as it starts", async () => {
    const first = fakePool();
    const { shared } = store(first);
    shared.start();
    first.state.working = false;
    first.start();
    await flush();
    expect(shared.status()).toBe("failed");
  });

  it("stops starting after two failures, until a reload", async () => {
    const pools = [fakePool(), fakePool(), fakePool()];
    const { shared, created } = store(...pools);
    shared.start();
    pools[0]!.fail(new Error("first"));
    await flush();
    shared.start();
    pools[1]!.fail(new Error("second"));
    await flush();
    shared.start();
    expect(created).toHaveLength(2);
    expect(shared.status()).toBe("failed");
  });

  it("counts a pool that can't even be made as a failure", () => {
    const { shared } = store();
    shared.start();
    expect(shared.status()).toBe("failed");
    expect(shared.peekReady()).toBeNull();
  });

  it("gives up on a ready pool when one of its workers fails, keeping it for its holders", async () => {
    const first = fakePool();
    const second = fakePool();
    const { shared, advance, workerErrors } = store(first, second);
    shared.start();
    first.start();
    await flush();
    const release = shared.hold(first.pool);
    workerErrors[0]!();
    expect(shared.status()).toBe("failed");
    expect(shared.peekReady()).toBeNull();
    advance(SHARED_WORKERS_RETIRE_GRACE_MS);
    expect(first.state.terminated).toBe(0);
    release();
    expect(first.state.terminated).toBe(1);
    // A later error from the retired pool changes nothing about the fresh one.
    shared.start();
    second.start();
    await flush();
    workerErrors[0]!();
    expect(shared.peekReady()).toBe(second.pool);
  });

  it("keeps a pool that died after starting until the last review holding it lets go", async () => {
    const first = fakePool();
    const second = fakePool();
    const { shared, advance } = store(first, second);
    shared.start();
    first.start();
    await flush();
    const releaseOne = shared.hold(first.pool);
    const releaseTwo = shared.hold(first.pool);
    // Its workers die: the next review doesn't get it, and a fresh one starts.
    first.state.working = false;
    expect(shared.peekReady()).toBeNull();
    advance(SHARED_WORKERS_RETIRE_GRACE_MS);
    expect(first.state.terminated).toBe(0);
    releaseOne();
    releaseOne();
    expect(first.state.terminated).toBe(0);
    releaseTwo();
    expect(first.state.terminated).toBe(1);
    shared.start();
    second.start();
    await flush();
    expect(shared.peekReady()).toBe(second.pool);
  });

  it("lets a review that chose a pool just before it was given up take hold of it", async () => {
    const first = fakePool();
    const { shared, advance } = store(first, fakePool());
    shared.start();
    first.start();
    await flush();
    const chosen = shared.peekReady()!;
    first.state.working = false;
    shared.peekReady();
    // The review mounts and holds it within the grace period.
    advance(SHARED_WORKERS_RETIRE_GRACE_MS - 1);
    const release = shared.hold(chosen);
    advance(1);
    expect(first.state.terminated).toBe(0);
    release();
    expect(first.state.terminated).toBe(1);
  });
});
