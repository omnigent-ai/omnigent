// Reading ahead, for a stack's members and a hovered list row: the order a stack's members are
// read in, and one queue per GitHub reader that sends those reads a request at a time, in idle
// moments, slower as the reader's hourly budget runs low, and not at all while it's nearly spent
// or GitHub is limiting the reader. Every view reading as that reader shares the queue, so its
// pace holds across them, and every queue in the window shares one allowance of requests.

import { BUDGET_SLOW_FACTOR, type GitHubBudgetPace } from "@/lib/githubReadBudget";
import type { GitHubReadBudget } from "@/lib/nativeBridge";

import { MAX_STACK_MEMBER_LOOKUPS } from "./stackMembers";
import type { StackParse } from "./stackModel";
import { previousInStack } from "./stackNavigation";

/** At most this many requests ahead at once, while the budget is healthy, across the window. */
export const STACK_WARMUP_IN_FLIGHT = 2;
/** The least time between two requests ahead, while the budget is healthy. */
export const STACK_WARMUP_GAP_MS = 500;
/** The same while the budget runs low: one request at a time, this far apart. */
export const STACK_WARMUP_SLOW_GAP_MS = STACK_WARMUP_GAP_MS * BUDGET_SLOW_FACTOR;

/**
 * The stack's other members to read ahead, nearest first: the one below, then those stacked
 * right on it, then the rest by how far they sit from it. Only members in its repository,
 * up to the stack reads' own limit.
 */
export function stackWarmupOrder(parse: StackParse): string[] {
  const self = parse.selfIndex;
  const readable = (index: number) => {
    const node = parse.nodes[index];
    return index !== self && !!node?.ref && !node.external;
  };
  const below = previousInStack(parse);
  const first = new Set<number>();
  if (below.kind === "go") first.add(below.destination.index);
  parse.nodes.forEach((node, index) => {
    if (node.parentIndex === self) first.add(index);
  });
  const rest = parse.nodes
    .map((_, index) => index)
    .filter((index) => !first.has(index))
    .sort((left, right) => Math.abs(left - self) - Math.abs(right - self) || left - right);
  const urls = [...first, ...rest].filter(readable).map((index) => parse.nodes[index]!.ref!.url);
  return [...new Set(urls)].slice(0, MAX_STACK_MEMBER_LOOKUPS - 1);
}

/** The phase that reads a member's details and stack, which its Overview shows. */
export const STACK_WARMUP_DETAILS_PHASE = 0;
/** The phase that reads what its Changes shows: files, patch and comparison. */
export const STACK_WARMUP_CHANGES_PHASE = 1;

/** One read ahead: at most one request, and the reads that wait on its answer. */
export interface StackWarmupRead {
  /** Earlier phases go first (details and stacks, then changes); nearer ones within each. */
  phase: number;
  rank: number;
  /** What it reads is already fresh, or can't be read here: no request is needed. */
  settled: () => boolean;
  /** Make the request; resolves to the reads its answer lets go ahead, if any. */
  run: () => Promise<StackWarmupRead[]>;
  /** The stack member it reads for, so the user's interest in that member can move it ahead. */
  member?: string;
  /** Whether it may still be sent, asked as it goes; a read it says no to is dropped. */
  admit?: () => boolean;
  /** The GitHub budgets its request spends, so a budget that isn't known holds it back. */
  spends?: readonly GitHubReadBudget["resource"][];
}

/** The requests ahead the window's queues have out, shared so their total stays in bounds. */
export interface SpeculativeSlots {
  available: () => boolean;
  take: () => void;
  /** Give a slot back once its request has really answered, and tell the queues. */
  give: () => void;
  inFlight: () => number;
  /** Hear when a slot comes back; returns the way to stop listening. */
  subscribe: (listener: () => void) => () => void;
}

export function createSpeculativeSlots(limit: number = STACK_WARMUP_IN_FLIGHT): SpeculativeSlots {
  let used = 0;
  const listeners = new Set<() => void>();
  return {
    available: () => used < limit,
    take: () => {
      used += 1;
    },
    give: () => {
      used -= 1;
      for (const listener of [...listeners]) listener();
    },
    inFlight: () => used,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** What paces a reader's queue: its hourly budget and cooldown, idle time and the clock. */
export interface StackWarmupPacing {
  budget: () => GitHubBudgetPace;
  /** When a rate limit holds the reader until, or null. */
  cooldownUntil: () => number | null;
  /** Hear when the budget may have eased; returns the way to stop listening. */
  onBudgetEased: (listener: () => void) => () => void;
  now: () => number;
  /** Run `work` when the browser is idle; returns the way to cancel it. */
  whenIdle: (work: () => void) => () => void;
  setTimer: (work: () => void, ms: number) => () => void;
}

/**
 * Move a member's reads ahead while the user shows interest in it; null where nothing reads it
 * ahead. The returned function ends that interest.
 */
export type StackReadAhead = (url: string) => (() => void) | null;

/** Who asked for a set of reads: a view of a stack, until it lets them go. */
export type StackWarmupOwner = object;

export interface StackWarmupQueue {
  /** Read `reads` for `owner`, in place of whatever it asked for before and hasn't started. */
  submit: (owner: StackWarmupOwner, reads: StackWarmupRead[]) => void;
  /** Forget what `owner` asked for that hasn't started; a request under way finishes. */
  release: (owner: StackWarmupOwner) => void;
  /**
   * Send `owner`'s reads for `member` up to `lastPhase` before any other, at the same pace,
   * until the returned function ends it or newer interest replaces it.
   */
  prioritize: (owner: StackWarmupOwner, member: string, lastPhase: number) => () => void;
  /** Requests sent and not answered yet, for tests and diagnostics. */
  inFlight: () => number;
}

interface Entry {
  owner: StackWarmupOwner;
  read: StackWarmupRead;
  order: number;
}

/**
 * A queue for one reader. Its pace counts every request it sent, whoever asked for it, and its
 * requests take from `slots`, which the window's other queues share.
 */
export function createStackWarmupQueue(
  pacing: StackWarmupPacing,
  slots: SpeculativeSlots = createSpeculativeSlots(),
): StackWarmupQueue {
  let entries: Entry[] = [];
  let order = 0;
  let inFlight = 0;
  let lastStart = -Infinity;
  const owners = new Set<StackWarmupOwner>();
  // The member the user shows interest in, if any: its reads go first while it lasts.
  let priority: { owner: StackWarmupOwner; member: string; lastPhase: number } | null = null;
  // An idle moment asked for, and a wait for the pace to allow the next request.
  let cancelIdle: (() => void) | null = null;
  let cancelWait: (() => void) | null = null;
  let stopListening: (() => void) | null = null;

  const add = (owner: StackWarmupOwner, reads: StackWarmupRead[]) => {
    for (const read of reads) entries.push({ owner, read, order: order++ });
    entries.sort(
      (left, right) =>
        left.read.phase - right.read.phase ||
        left.read.rank - right.read.rank ||
        left.order - right.order,
    );
  };

  /** The next entry to send: the member of interest's first, else the queue's order. */
  const nextEntry = (): Entry | undefined => {
    const wanted = priority;
    const promoted =
      wanted &&
      entries.find(
        ({ owner, read }) =>
          owner === wanted.owner && read.member === wanted.member && read.phase <= wanted.lastPhase,
      );
    return promoted || entries[0];
  };

  /** Null when a request may go now; else when to look again, or "full" to wait for one. */
  const blocked = (): number | "full" | null => {
    const now = pacing.now();
    const held = pacing.cooldownUntil();
    if (held !== null && held > now) return held;
    const budget = pacing.budget();
    if (budget.pace === "stopped") return budget.until;
    const slow = budget.pace === "slow";
    if (inFlight >= (slow ? 1 : STACK_WARMUP_IN_FLIGHT) || !slots.available()) return "full";
    const gap = slow ? STACK_WARMUP_SLOW_GAP_MS : STACK_WARMUP_GAP_MS;
    return now - lastStart < gap ? lastStart + gap : null;
  };

  const waitUntil = (until: number) => {
    if (cancelWait) return;
    cancelWait = pacing.setTimer(
      () => {
        cancelWait = null;
        pump();
      },
      Math.max(0, until - pacing.now()),
    );
  };

  /** Run `entry`'s read; its answer's reads join the queue while their owner still wants them. */
  const run = (entry: Entry, counted: boolean) => {
    if (counted) {
      inFlight += 1;
      slots.take();
      lastStart = pacing.now();
    }
    void entry.read
      .run()
      .then(
        (next) => {
          if (owners.has(entry.owner)) add(entry.owner, next);
        },
        () => {},
      )
      .finally(() => {
        if (counted) {
          inFlight -= 1;
          slots.give();
        }
        pump();
      });
  };

  function pump() {
    // Reads that need no request go at once, whatever the pace.
    for (const entry of entries.filter((each) => each.read.settled())) {
      entries.splice(entries.indexOf(entry), 1);
      run(entry, false);
    }
    if (entries.length === 0) {
      if (inFlight === 0) {
        stopListening?.();
        stopListening = null;
      }
      return;
    }
    stopListening ??= pacing.onBudgetEased(() => {
      cancelWait?.();
      cancelWait = null;
      pump();
    });
    if (cancelIdle) return;
    const wait = blocked();
    if (wait === "full") return;
    if (wait !== null) return waitUntil(wait);
    cancelIdle = pacing.whenIdle(() => {
      cancelIdle = null;
      // The pace may have changed while it waited: ask again as the request goes.
      const again = blocked();
      const entry = nextEntry();
      if (again !== null || !entry) {
        pump();
        return;
      }
      entries.splice(entries.indexOf(entry), 1);
      // One no longer admitted, e.g. its hover ended or the budget ran low, is dropped unsent.
      if (entry.read.settled()) run(entry, false);
      else if (entry.read.admit?.() !== false) run(entry, true);
      pump();
    });
  }

  // A slot another queue gave back may let this one send.
  slots.subscribe(() => {
    if (entries.length > 0) pump();
  });

  return {
    submit: (owner, reads) => {
      owners.add(owner);
      entries = entries.filter((entry) => entry.owner !== owner);
      add(owner, reads);
      pump();
    },
    release: (owner) => {
      owners.delete(owner);
      if (priority?.owner === owner) priority = null;
      entries = entries.filter((entry) => entry.owner !== owner);
      if (entries.length === 0) {
        cancelIdle?.();
        cancelIdle = null;
        cancelWait?.();
        cancelWait = null;
        if (inFlight === 0) {
          stopListening?.();
          stopListening = null;
        }
      }
    },
    prioritize: (owner, member, lastPhase) => {
      if (!owners.has(owner)) return () => {};
      const interest = { owner, member, lastPhase };
      priority = interest;
      pump();
      // Only this interest ends here, not a newer one that replaced it.
      return () => {
        if (priority === interest) priority = null;
      };
    },
    inFlight: () => inFlight,
  };
}
