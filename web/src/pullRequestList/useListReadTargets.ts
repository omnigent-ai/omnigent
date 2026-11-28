// Which of the Pull Requests list's rows read their pull request from GitHub: only rows in the
// viewport, the first screen at once and rows a scroll brings in once they've stayed a moment,
// plus the few the user is working with. A fast
// scroll past hundreds of rows reads none of them, and new reads are spread out so a slow
// scroll can't spend a reader's hourly budget. Every other row shows what it already has.

import { useEffect, useMemo, useRef, useState } from "react";

/** How long a row a scroll brought into view stays there before it reads. */
export const READ_TARGET_DWELL_MS = 400;
/** The most rows that start a read of their own in any minute. */
export const NEW_ROW_READS_PER_MINUTE = 30;
/** The most rows read for what the user is working with, wherever they are. */
export const MAX_READ_EXCEPTIONS = 3;
const MINUTE_MS = 60_000;

export interface ReadTargetsInput {
  /** The rows inside the viewport, in order. */
  visible: readonly string[];
  /** Rows that read wherever they are: the active row, the one open beside the list, a write's. */
  exceptions: readonly string[];
  /** Whether admitting `url` would read GitHub: nothing cached, or what is cached is stale. */
  needsRead: (url: string) => boolean;
  /** Whether the app may read on its own now; while it can't, only the exceptions read. */
  allowed: boolean;
}

export interface ReadTargets {
  targets: ReadonlySet<string>;
  /** When to work the targets out again, or null when nothing is waiting. */
  wakeAt: number | null;
}

/** The read targets over time: each row's time in view, and the new reads of the last minute. */
export class ReadTargetSchedule {
  private seenAt = new Map<string, number>();
  private admitted = new Set<string>();
  private spent: number[] = [];

  update({ visible, exceptions, needsRead, allowed }: ReadTargetsInput, now: number): ReadTargets {
    const inView = new Set(visible);
    // Rows that fill an empty view, as the list loads or a filter brings them back, came by no
    // scroll: they read at once, so what the list opens on is current before it's acted on.
    const firstScreen = this.seenAt.size === 0;
    for (const url of [...this.seenAt.keys()]) {
      if (!inView.has(url)) {
        // A row that left the view gives up its place, and waits again if it comes back.
        this.seenAt.delete(url);
        this.admitted.delete(url);
      }
    }
    this.spent = this.spent.filter((at) => now - at < MINUTE_MS);
    let wakeAt: number | null = null;
    const wakeBy = (at: number) => {
      wakeAt = wakeAt === null ? at : Math.min(wakeAt, at);
    };
    for (const url of visible) {
      if (!this.seenAt.has(url))
        this.seenAt.set(url, firstScreen ? now - READ_TARGET_DWELL_MS : now);
      if (this.admitted.has(url)) continue;
      const settledAt = this.seenAt.get(url)! + READ_TARGET_DWELL_MS;
      if (now < settledAt) {
        wakeBy(settledAt);
        continue;
      }
      // Nothing is admitted, or spent, while the app may not read on its own.
      if (!allowed) continue;
      if (!needsRead(url)) {
        this.admitted.add(url);
        continue;
      }
      if (this.spent.length >= NEW_ROW_READS_PER_MINUTE) {
        wakeBy(this.spent[0]! + MINUTE_MS);
        continue;
      }
      this.spent.push(now);
      this.admitted.add(url);
    }
    const targets = new Set(exceptions.slice(0, MAX_READ_EXCEPTIONS));
    if (allowed) for (const url of this.admitted) targets.add(url);
    return { targets, wakeAt };
  }
}

const sameSet = (a: ReadonlySet<string>, b: ReadonlySet<string>) =>
  a.size === b.size && [...a].every((url) => b.has(url));

/**
 * {@link ReadTargetSchedule} for a list: the rows that read now, kept the same set while it
 * doesn't change, and worked out again when a waiting row's turn comes.
 */
export function useListReadTargets(
  input: ReadTargetsInput,
  now: () => number = Date.now,
): ReadonlySet<string> {
  const schedule = useRef<ReadTargetSchedule | null>(null);
  schedule.current ??= new ReadTargetSchedule();
  const latest = useRef(input);
  latest.current = input;
  const [tick, setTick] = useState(0);
  const visibleKey = input.visible.join("\n");
  const exceptionsKey = input.exceptions.join("\n");
  const computed = useMemo(
    () => schedule.current!.update(latest.current, now()),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the inputs' content, and each wake.
    [visibleKey, exceptionsKey, input.allowed, tick, now],
  );
  const kept = useRef(computed.targets);
  if (!sameSet(kept.current, computed.targets)) kept.current = computed.targets;
  useEffect(() => {
    if (computed.wakeAt === null) return;
    const timer = setTimeout(
      () => setTick((value) => value + 1),
      Math.max(0, computed.wakeAt - now()),
    );
    return () => clearTimeout(timer);
  }, [computed, now]);
  return kept.current;
}

/**
 * How long a row's first read waits for its first batch: past the stats' coalescing (one batch
 * per 5 s) and its answer, so a batch that never comes doesn't keep the row from reading.
 */
export const FIRST_READ_BATCH_WAIT_MS = 8_000;

export interface FirstReadsInput {
  /** The rows that read now. */
  targets: ReadonlySet<string>;
  /** Whether `url`'s first read should wait: nothing read yet, and its batch not answered. */
  waits: (url: string) => boolean;
}

/**
 * The targets that read now, holding back a row's first read until its first batch answered,
 * so the read records the batch's snapshot instead of reading again to start following it.
 */
export class FirstReadSchedule {
  private since = new Map<string, number>();

  update({ targets, waits }: FirstReadsInput, now: number): ReadTargets {
    for (const url of [...this.since.keys()]) if (!targets.has(url)) this.since.delete(url);
    const reading = new Set<string>();
    let wakeAt: number | null = null;
    for (const url of targets) {
      if (!waits(url)) {
        reading.add(url);
        continue;
      }
      const since = this.since.get(url) ?? now;
      this.since.set(url, since);
      const due = since + FIRST_READ_BATCH_WAIT_MS;
      if (now >= due) reading.add(url);
      else wakeAt = wakeAt === null ? due : Math.min(wakeAt, due);
    }
    return { targets: reading, wakeAt };
  }
}

/** {@link FirstReadSchedule} for a list: the same set while it doesn't change. */
export function useFirstReadsAfterBatch(
  input: FirstReadsInput | null,
  now: () => number = Date.now,
): ReadonlySet<string> | undefined {
  const schedule = useRef<FirstReadSchedule | null>(null);
  schedule.current ??= new FirstReadSchedule();
  const [tick, setTick] = useState(0);
  void tick;
  const computed = input ? schedule.current.update(input, now()) : null;
  const kept = useRef<ReadonlySet<string> | undefined>(undefined);
  if (!computed) kept.current = undefined;
  else if (!kept.current || !sameSet(kept.current, computed.targets))
    kept.current = computed.targets;
  const wakeAt = computed?.wakeAt ?? null;
  useEffect(() => {
    if (wakeAt === null) return;
    const timer = setTimeout(() => setTick((value) => value + 1), Math.max(0, wakeAt - now()));
    return () => clearTimeout(timer);
  }, [wakeAt, now]);
  return kept.current;
}
