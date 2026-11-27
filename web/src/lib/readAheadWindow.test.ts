import { describe, expect, it } from "vitest";

import { createSpeculativeSlots } from "@/lib/pullRequestStacks/stackWarmup";
import { createReadAheadWindow } from "@/lib/readAheadWindow";

/** Lets settled reads give back their slots. */
const settle = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

/** A window whose reads wait until the test answers them. */
function windowOver(limit = 2) {
  const slots = createSpeculativeSlots(limit);
  const reads: { key: string; signal: AbortSignal; answer: () => void; fail: () => void }[] = [];
  const done = new Set<string>();
  const readAhead = createReadAheadWindow({
    slots,
    done: (key) => done.has(key),
    read: (key, signal) =>
      new Promise<void>((resolve, reject) => {
        reads.push({
          key,
          signal,
          answer: () => {
            done.add(key);
            resolve();
          },
          fail: () => reject(new Error("refused")),
        });
      }),
  });
  return { readAhead, reads, slots, done };
}

describe("a read-ahead window", () => {
  it("reads what it wants in order, a few at a time, and the rest once a slot frees", async () => {
    const { readAhead, reads, slots } = windowOver();
    readAhead.want(["b", "c", "d"]);
    expect(reads.map((read) => read.key)).toEqual(["b", "c"]);
    expect(slots.inFlight()).toBe(2);
    reads[0]!.answer();
    await settle();
    expect(reads.map((read) => read.key)).toEqual(["b", "c", "d"]);
  });

  it("drops a queued key it no longer wants, and aborts a started one's signal", async () => {
    const { readAhead, reads } = windowOver();
    readAhead.want(["b", "c", "d"]);
    readAhead.want(["c"]);
    expect(reads[0]!.signal.aborted).toBe(true);
    expect(reads[1]!.signal.aborted).toBe(false);
    // A read let go of still holds its slot until it settles.
    reads[1]!.answer();
    await settle();
    expect(reads.map((read) => read.key)).toEqual(["b", "c"]);
  });

  it("never reads what's already done, and keeps a failure quiet", async () => {
    const { readAhead, reads, done, slots } = windowOver();
    done.add("b");
    readAhead.want(["b", "c"]);
    expect(reads.map((read) => read.key)).toEqual(["c"]);
    reads[0]!.fail();
    await settle();
    expect(slots.inFlight()).toBe(0);
    // A key that failed can be wanted again.
    readAhead.want(["c"]);
    expect(reads.map((read) => read.key)).toEqual(["c", "c"]);
  });

  it("shares slots across windows, and lets go of everything on stop", async () => {
    const slots = createSpeculativeSlots(1);
    const signals: AbortSignal[] = [];
    let answer = () => {};
    const make = () =>
      createReadAheadWindow({
        slots,
        done: () => false,
        read: (_key, signal) => {
          signals.push(signal);
          return new Promise<void>((resolve) => {
            answer = resolve;
          });
        },
      });
    const first = make();
    const second = make();
    first.want(["a"]);
    second.want(["b"]);
    expect(signals).toHaveLength(1);
    first.stop();
    expect(signals[0]!.aborted).toBe(true);
    answer();
    await settle();
    expect(signals).toHaveLength(2);
  });
});
