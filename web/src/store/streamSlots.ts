// Origin-wide budget for how many conversation streams stay open at once.
//
// The cap this enforces is shared across every same-origin tab, because the
// resource it protects — the browser's connection pool (~6 per origin on
// HTTP/1.1, one multiplexed connection on HTTP/2) — is shared across tabs. A
// per-tab cap can't see the other tabs, so two tabs at cap 3 each open 6 SSE
// streams and deadlock every other fetch. `navigator.locks` is the coordination
// primitive: a lock's holders are visible across all same-origin agents, and a
// lock auto-releases when its tab closes or crashes, so a dead tab never strands
// a slot.
//
// Modelled as N named slot-locks (`omnigent:stream-slot:0..N-1`) acquired with
// `{ ifAvailable: true }`. That grants a free slot atomically or hands back
// `null` — unlike querying the held count and then acquiring, which races (two
// tabs both read "one free" and both take it). N is the same transport-derived
// number as before (see `maxLiveConversations`), only now shared.

import { maxLiveConversations } from "./conversationRegistry";

/**
 * A held origin-wide slot.
 *
 * `release` is idempotent and, crucially, resolves only once the underlying lock
 * is actually released — so a caller that evicts a stream to reclaim its slot
 * can `await` this before re-checking availability, instead of racing a release
 * that the browser hasn't processed yet.
 */
export interface StreamSlot {
  release: () => Promise<void>;
}

export interface StreamSlotManager {
  /**
   * Take one free slot without waiting: resolves to a `StreamSlot` when a slot
   * was granted, or `null` when every slot is held (origin-wide, across tabs).
   */
  tryAcquire: () => Promise<StreamSlot | null>;
}

const SLOT_LOCK_PREFIX = "omnigent:stream-slot:";

/**
 * Hold `name` until the returned slot's `release` runs, or resolve `null` when
 * it is already held. `ifAvailable` is what makes this race-free: the callback
 * either gets the lock or gets `null`, atomically.
 */
function holdLockIfFree(name: string): Promise<StreamSlot | null> {
  return new Promise((settle) => {
    let releaseHeld: () => void = () => {};
    let released = false;
    // `requestDone` resolves when the callback's returned promise settles —
    // i.e. after `releaseHeld()` runs AND the browser has released the lock.
    // `release` awaits it so the freed slot is observable to the next acquire.
    const requestDone = navigator.locks
      .request(name, { ifAvailable: true }, (lock) => {
        if (lock === null) {
          settle(null);
          return;
        }
        return new Promise<void>((r) => {
          releaseHeld = r;
          settle({
            release: async () => {
              if (!released) {
                released = true;
                releaseHeld();
              }
              await requestDone;
            },
          });
        });
      })
      .catch(() => settle(null));
  });
}

/** Web Locks manager: coordinated across every same-origin tab. */
function webLocksSlotManager(count: () => number): StreamSlotManager {
  return {
    async tryAcquire() {
      const n = Math.max(0, count());
      // Sequential, not parallel: `ifAvailable` in parallel could grant two
      // slots to one caller. Take the first free one.
      for (let i = 0; i < n; i += 1) {
        // eslint-disable-next-line no-await-in-loop
        const slot = await holdLockIfFree(`${SLOT_LOCK_PREFIX}${i}`);
        if (slot !== null) return slot;
      }
      return null;
    },
  };
}

/**
 * Per-tab counting semaphore, for environments without Web Locks (jsdom, an
 * insecure context, an old browser). No cross-tab coordination — this simply
 * degrades to the pre-existing per-tab cap rather than failing to open streams.
 */
function inMemorySlotManager(count: () => number): StreamSlotManager {
  let held = 0;
  return {
    tryAcquire() {
      if (held >= Math.max(0, count())) return Promise.resolve(null);
      held += 1;
      let released = false;
      return Promise.resolve({
        release: () => {
          if (!released) {
            released = true;
            held -= 1;
          }
          return Promise.resolve();
        },
      });
    },
  };
}

function webLocksAvailable(): boolean {
  return (
    typeof navigator !== "undefined" &&
    typeof navigator.locks !== "undefined" &&
    typeof navigator.locks.request === "function"
  );
}

function createDefaultStreamSlotManager(): StreamSlotManager {
  return webLocksAvailable()
    ? webLocksSlotManager(maxLiveConversations)
    : inMemorySlotManager(maxLiveConversations);
}

let activeManager: StreamSlotManager = createDefaultStreamSlotManager();

/** The process-wide slot manager (Web Locks in a real browser). */
export function getStreamSlotManager(): StreamSlotManager {
  return activeManager;
}

/** Swap in a fake manager (tests drive slot availability through this). */
export function setStreamSlotManagerForTest(manager: StreamSlotManager): void {
  activeManager = manager;
}

/** Restore the real manager (test teardown). */
export function resetStreamSlotManager(): void {
  activeManager = createDefaultStreamSlotManager();
}
