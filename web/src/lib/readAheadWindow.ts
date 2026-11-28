// Reads ahead of a step: a view says which keys it wants now, in order, and the window reads
// them a few at a time across the views that share its slots, letting go of what leaves it.

import type { SpeculativeSlots } from "@/lib/pullRequestStacks/stackWarmup";

/** One view's reads ahead: what it wants now, in order, until it stops. */
export interface ReadAheadWindow {
  /** Read `keys` ahead, in this order, and let go of any it wanted before and no longer does. */
  want: (keys: readonly string[]) => void;
  /** Let go of everything; a read under way still holds its slot until it answers. */
  stop: () => void;
}

/**
 * A window over `read`. A read waits for one of `slots`, and holds it until it settles, even
 * after it was let go of. Letting go drops a read that hasn't started, and aborts the signal
 * a started read was given; whether that cancels it is the reader's choice. A key `done`
 * reports already read is never read again.
 */
export function createReadAheadWindow({
  slots,
  done,
  read,
}: {
  slots: SpeculativeSlots;
  done: (key: string) => boolean;
  read: (key: string, signal: AbortSignal) => Promise<unknown>;
}): ReadAheadWindow {
  const interests = new Map<string, AbortController>();
  let queue: string[] = [];
  const start = (key: string, interest: AbortController) => {
    slots.take();
    let reading: Promise<unknown>;
    try {
      reading = read(key, interest.signal);
    } catch (error) {
      reading = Promise.reject(error);
    }
    // A failed read ahead stays quiet: the view that shows the key reads it again and says why.
    void reading
      .catch(() => {})
      .finally(() => {
        if (interests.get(key) === interest) interests.delete(key);
        slots.give();
      });
  };
  const pump = () => {
    while (queue.length > 0 && slots.available()) {
      const key = queue.shift()!;
      const interest = interests.get(key);
      if (interest && !done(key)) start(key, interest);
      else interests.delete(key);
    }
  };
  const unsubscribe = slots.subscribe(pump);
  const want = (keys: readonly string[]) => {
    const next = new Set(keys);
    for (const [key, interest] of interests) {
      if (next.has(key)) continue;
      interest.abort();
      interests.delete(key);
    }
    // A read queued or under way keeps its place; a new one joins the queue.
    for (const key of keys) {
      if (interests.has(key) || done(key)) continue;
      interests.set(key, new AbortController());
      queue.push(key);
    }
    queue = queue
      .filter((key) => interests.has(key))
      .sort((left, right) => keys.indexOf(left) - keys.indexOf(right));
    pump();
  };
  return {
    want,
    stop: () => {
      want([]);
      unsubscribe();
    },
  };
}
