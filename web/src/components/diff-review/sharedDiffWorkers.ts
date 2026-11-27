// One pool of syntax-highlighting workers for every diff review this window shows, so opening a
// review doesn't wait for fresh workers to start each time. A review is handed the pool only
// once it has started; until then, or after it fails, a review starts its own as before. Pools
// given up on are shut down only once no review holds them.

import { WorkerPoolManager } from "@pierre/diffs/worker";
import { useEffect, useState } from "react";

import { runInIdleSlices } from "@/lib/idleWork";
import { DIFF_SYNTAX_THEME } from "@/lib/diffTheme";

/** How long a pool may take to start before it's given up on. */
export const SHARED_WORKERS_STARTUP_MS = 10_000;
/** After this many failed starts in a window, reviews render without workers until a reload. */
export const SHARED_WORKERS_MAX_FAILURES = 2;
/** Highlighted files the pool keeps, of each kind; it outlives every review, so fewer than Pierre's 100. */
export const SHARED_WORKERS_CACHE_SIZE = 40;
/** How long a pool given up on lingers, so a review that chose it just now takes hold first. */
export const SHARED_WORKERS_RETIRE_GRACE_MS = 5_000;

export type SharedDiffWorkersStatus = "idle" | "starting" | "ready" | "failed";

/** What a pool is made with: a manager, and a hook for its workers failing before they start. */
export type CreateDiffWorkerPool = (onWorkerError: () => void) => WorkerPoolManager;

export interface SharedDiffWorkers {
  /** Start the pool if it isn't started, starting, or given up on. */
  start: () => void;
  /** The pool, only while it's started and working; null otherwise. Never a starting one. */
  peekReady: () => WorkerPoolManager | null;
  /** A mounted review holds `pool`; it's never shut down while any review does. */
  hold: (pool: WorkerPoolManager) => () => void;
  status: () => SharedDiffWorkersStatus;
  subscribe: (listener: () => void) => () => void;
}

export function createSharedDiffWorkers({
  createPool,
  startupMs = SHARED_WORKERS_STARTUP_MS,
  maxFailures = SHARED_WORKERS_MAX_FAILURES,
  retireGraceMs = SHARED_WORKERS_RETIRE_GRACE_MS,
  setTimer = (work, ms) => {
    const handle = setTimeout(work, ms);
    return () => clearTimeout(handle);
  },
}: {
  createPool: CreateDiffWorkerPool;
  startupMs?: number;
  maxFailures?: number;
  retireGraceMs?: number;
  setTimer?: (work: () => void, ms: number) => () => void;
}): SharedDiffWorkers {
  let status: SharedDiffWorkersStatus = "idle";
  let current: WorkerPoolManager | null = null;
  let failures = 0;
  const holders = new Map<WorkerPoolManager, number>();
  // Pools given up on that a review still holds: shut down once the last one lets go.
  const retired = new Set<WorkerPoolManager>();
  const listeners = new Set<() => void>();

  const set = (next: SharedDiffWorkersStatus) => {
    status = next;
    for (const listener of [...listeners]) listener();
  };

  const shutDown = (pool: WorkerPoolManager) => {
    retired.delete(pool);
    try {
      pool.terminate();
    } catch {
      // A pool that can't shut down cleanly is dropped all the same.
    }
  };

  /**
   * Give up on `pool`: shut it down once no review holds it, after a grace period, so one that
   * chose it while rendering takes hold before it goes.
   */
  const retire = (pool: WorkerPoolManager) => {
    if (current === pool) current = null;
    retired.add(pool);
    setTimer(() => {
      if (retired.has(pool) && (holders.get(pool) ?? 0) === 0) shutDown(pool);
    }, retireGraceMs);
  };

  const fail = (pool: WorkerPoolManager | null) => {
    failures += 1;
    if (pool) retire(pool);
    set("failed");
  };

  function start() {
    if (status === "starting" || status === "ready") return;
    if (status === "failed" && failures >= maxFailures) return;
    let pool: WorkerPoolManager;
    let settled = false;
    const startupFailed = () => {
      if (settled) return;
      settled = true;
      stopTimer();
      fail(pool ?? null);
    };
    let stopTimer = () => {};
    try {
      pool = createPool(() => {
        // Pierre only logs a worker's error, so the pool is given up on here: before it started,
        // as a failed start; after, no review is handed it again and it goes once let go.
        if (current !== pool) return;
        if (status === "starting") startupFailed();
        else if (status === "ready") fail(pool);
      });
    } catch {
      fail(null);
      return;
    }
    current = pool;
    set("starting");
    stopTimer = setTimer(startupFailed, startupMs);
    pool.initialize().then(() => {
      if (settled) return;
      settled = true;
      stopTimer();
      if (!pool.isWorkingPool()) fail(pool);
      else set("ready");
    }, startupFailed);
  }

  function peekReady(): WorkerPoolManager | null {
    if (status !== "ready" || !current) return null;
    // A pool Pierre itself marked failed after starting is given up on too.
    if (!current.isWorkingPool()) {
      fail(current);
      return null;
    }
    return current;
  }

  function hold(pool: WorkerPoolManager): () => void {
    holders.set(pool, (holders.get(pool) ?? 0) + 1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const left = (holders.get(pool) ?? 1) - 1;
      if (left > 0) holders.set(pool, left);
      else {
        holders.delete(pool);
        if (retired.has(pool)) shutDown(pool);
      }
    };
  }

  return {
    start,
    peekReady,
    hold,
    status: () => status,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** Today's pool: two to four workers, as many as the machine allows. */
export const createDiffWorkerPool: CreateDiffWorkerPool = (onWorkerError) =>
  new WorkerPoolManager(
    {
      poolSize: Math.max(2, Math.min(4, navigator.hardwareConcurrency || 2)),
      totalASTLRUCacheSize: SHARED_WORKERS_CACHE_SIZE,
      workerFactory: () => {
        const worker = new Worker(new URL("@pierre/diffs/worker/worker.js", import.meta.url), {
          type: "module",
        });
        worker.addEventListener("error", onWorkerError);
        return worker;
      },
    },
    { langs: ["text"], theme: DIFF_SYNTAX_THEME },
  );

/** This window's shared pool; none where workers can't run. */
export const sharedDiffWorkers: SharedDiffWorkers | null =
  typeof Worker === "undefined"
    ? null
    : createSharedDiffWorkers({ createPool: createDiffWorkerPool });

/** Start the shared pool in the next idle moment, so a review opened soon after finds it ready. */
export function warmSharedDiffWorkers(): () => void {
  if (!sharedDiffWorkers || sharedDiffWorkers.status() !== "idle") return () => {};
  return runInIdleSlices(() => {
    sharedDiffWorkers.start();
    return true;
  });
}

/** Which workers a review renders with, chosen once when it mounts. */
export type DiffWorkersChoice =
  /** The shared pool, started and working; the review holds it while mounted. */
  | { kind: "shared"; pool: WorkerPoolManager }
  /**
   * Its own pool, as before sharing: the shared one isn't ready yet, or has failed.
   */
  | { kind: "own" }
  /** No workers, where workers can't run. */
  | { kind: "none" };

/**
 * The workers a review mounting now renders with. The choice holds while it's mounted, so the
 * shared pool becoming ready later never remounts it; a review that started its own starts the
 * shared one for the next.
 */
export function useDiffWorkers(): DiffWorkersChoice {
  const [choice] = useState<DiffWorkersChoice>(() => {
    if (!sharedDiffWorkers) return { kind: "none" };
    const pool = sharedDiffWorkers.peekReady();
    return pool ? { kind: "shared", pool } : { kind: "own" };
  });
  // The hold lasts exactly as long as the review is mounted.
  useEffect(() => {
    if (!sharedDiffWorkers || choice.kind !== "shared") return undefined;
    return sharedDiffWorkers.hold(choice.pool);
  }, [choice]);
  useEffect(() => {
    if (sharedDiffWorkers && choice.kind === "own") sharedDiffWorkers.start();
  }, [choice]);
  return choice;
}

/**
 * Start the shared pool in an idle moment while this mounts: PR lists do, so a PR
 * opened from one finds it ready, and a PR's details do, for routes that open one directly.
 */
export function useWarmSharedDiffWorkers(): void {
  useEffect(() => warmSharedDiffWorkers(), []);
}
