// Polls a pacer slowed or paused, kept so they can pace again the moment what held them lets go.
// TanStack works out a poll's interval only after a read or an options change, so a poll held
// for an hour would otherwise wait out its old timer.

/** A TanStack query, by the observers that schedule its poll. */
export interface HeldPoll {
  observers?: readonly {
    options: unknown;
    setOptions: (options: never) => void;
    refetch?: (options?: { cancelRefetch?: boolean }) => Promise<unknown>;
  }[];
}

// Below this many, holding never prunes: a few refs to let go cost less than walking them.
const PRUNE_AT_LEAST = 32;

const observed = (poll: HeldPoll) => (poll.observers?.length ?? 0) > 0;

/** Holding never keeps a query alive: one removed, or whose observers all left, is let go. */
export function createHeldPolls() {
  let refs: WeakRef<HeldPoll>[] = [];
  const holding = new WeakSet<HeldPoll>();
  // How many refs the last pass kept, so holding prunes only once they've doubled since.
  let keptAtPrune = 0;

  /** The held polls still alive and observed; the rest are let go. */
  function live(): HeldPoll[] {
    const polls: HeldPoll[] = [];
    const kept: WeakRef<HeldPoll>[] = [];
    for (const ref of refs) {
      const poll = ref.deref();
      if (!poll) continue;
      if (observed(poll)) {
        polls.push(poll);
        kept.push(ref);
      } else holding.delete(poll);
    }
    refs = kept;
    keptAtPrune = kept.length;
    return polls;
  }

  function take(): HeldPoll[] {
    const polls = live();
    refs = [];
    for (const poll of polls) holding.delete(poll);
    return polls;
  }

  return {
    hold(poll: HeldPoll) {
      if (holding.has(poll)) return;
      if (refs.length >= Math.max(2 * keptAtPrune, PRUNE_AT_LEAST)) live();
      holding.add(poll);
      refs.push(new WeakRef(poll));
    },
    /** Work out each held poll's interval again; its next read follows the new one. */
    reschedule() {
      for (const poll of take()) {
        for (const observer of poll.observers ?? []) {
          try {
            observer.setOptions(observer.options as never);
          } catch {
            // A poll that can't reschedule keeps its timer.
          }
        }
      }
    },
    /** Hand over every held poll still alive and observed, holding none after. */
    drain: take,
    /** How many polls are held and still observed. */
    size: () => live().length,
    clear() {
      take();
    },
  };
}

/** The observer parts a wake reads through: whether it's on, and still polls. */
interface PollingObserver {
  options: {
    enabled?: boolean | ((query: HeldPoll) => boolean);
    refetchInterval?: number | false | ((query: HeldPoll) => number | false | undefined);
  };
  refetch?: (options?: { cancelRefetch?: boolean }) => Promise<unknown>;
}

/** An observer of `poll` that's enabled and still polls it, or null. */
export function pollingObserver(poll: HeldPoll): PollingObserver | null {
  for (const each of (poll.observers ?? []) as readonly PollingObserver[]) {
    try {
      const { enabled, refetchInterval } = each.options;
      if ((typeof enabled === "function" ? enabled(poll) : enabled) === false) continue;
      const interval =
        typeof refetchInterval === "function" ? refetchInterval(poll) : refetchInterval;
      if (typeof interval === "number" && interval > 0) return each;
    } catch {
      // An observer whose options can't be read isn't one to read through.
    }
  }
  return null;
}
