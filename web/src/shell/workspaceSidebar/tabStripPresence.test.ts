import { describe, expect, it } from "vitest";

import { TAB_STRIP_MOTION } from "./tabStripMotion";
import {
  initialPresence,
  nextPresenceDeadline,
  type PresenceState,
  settleAllPresence,
  settlePresence,
  updatePresence,
} from "./tabStripPresence";

const motion = TAB_STRIP_MOTION;
const slack = motion.settleSlackMs;
const payload = (key: string) => `tab ${key}`;
const update = (state: PresenceState<string>, keys: string[], now: number) =>
  updatePresence(state, keys, now, payload);
const shape = (state: PresenceState<string>) =>
  state.slots.map((slot) => `${slot.key}:${slot.phase}`);

describe("tab strip presence", () => {
  it("shows the first tabs as they are, with nothing to animate", () => {
    const state = initialPresence<string>(["a", "b"]);
    expect(shape(state)).toEqual(["a:present", "b:present"]);
    expect(nextPresenceDeadline(state, motion)).toBeNull();
  });

  it("shows the first tabs of an empty strip at once", () => {
    expect(shape(update(initialPresence([]), ["a"], 0))).toEqual(["a:present"]);
  });

  it("grows in a tab added later and settles it once its time is up", () => {
    const added = update(initialPresence(["a"]), ["a", "b"], 1000);
    expect(shape(added)).toEqual(["a:present", "b:entering"]);
    expect(added.slots[1]).toMatchObject({ startedAt: 1000 });
    // It stays marked a little past its animation, which starts after the commit.
    expect(nextPresenceDeadline(added, motion)).toBe(1000 + motion.enterMs + slack);
    expect(settlePresence(added, 1000 + motion.enterMs + slack - 1, motion)).toBe(added);
    expect(shape(settlePresence(added, 1000 + motion.enterMs + slack, motion))).toEqual([
      "a:present",
      "b:present",
    ]);
  });

  it("keeps a closed tab as a ghost at its old place until it has shrunk away", () => {
    const closed = update(initialPresence(["a", "b", "c"]), ["a", "c"], 500);
    expect(shape(closed)).toEqual(["a:present", "b:leaving", "c:present"]);
    expect(closed.slots[1]).toMatchObject({ payload: "tab b" });
    expect(shape(settlePresence(closed, 500 + motion.leaveMs + slack, motion))).toEqual([
      "a:present",
      "c:present",
    ]);
  });

  it("keeps ghosts of the first and of several closed tabs where they were", () => {
    const closed = update(initialPresence(["a", "b", "c", "d"]), ["c"], 0);
    expect(shape(closed)).toEqual(["a:leaving", "b:leaving", "c:present", "d:leaving"]);
  });

  it("puts a ghost after the tab it followed when the remaining tabs reorder", () => {
    const closed = update(initialPresence(["a", "b", "c"]), ["a", "c"], 0);
    const moved = update(closed, ["c", "a"], 10);
    expect(shape(moved)).toEqual(["c:present", "a:present", "b:leaving"]);
  });

  it("swaps a tab replaced at the same place, as when a New tab becomes a tool", () => {
    const swapped = update(initialPresence(["a", "new"]), ["a", "files"], 0);
    expect(shape(swapped)).toEqual(["a:present", "files:present"]);
    expect(nextPresenceDeadline(swapped, motion)).toBeNull();
    // The last tab closing and an empty New tab taking its place is a swap too.
    expect(shape(update(initialPresence(["only"]), ["empty"], 0))).toEqual(["empty:present"]);
  });

  it("turns a tab closed while still growing into a ghost, and a reopened ghost back", () => {
    const added = update(initialPresence(["a"]), ["a", "b"], 0);
    const closed = update(added, ["a"], 40);
    expect(shape(closed)).toEqual(["a:present", "b:leaving"]);
    expect(closed.slots[1]).toMatchObject({ startedAt: 40, payload: "tab b" });
    // Its element is kept, so CSS eases it back from whatever width it had reached.
    const reopened = update(closed, ["a", "b"], 60);
    expect(shape(reopened)).toEqual(["a:present", "b:entering"]);
    expect(reopened.slots[1]).toMatchObject({ startedAt: 60, payload: undefined });
  });

  it("leaves no ghost for a tab with nothing to show", () => {
    expect(shape(updatePresence(initialPresence(["a", "b"]), ["a"], 0, () => undefined))).toEqual([
      "a:present",
    ]);
  });

  it("ends every phase at once, as when Reduce motion turns on", () => {
    const moving = update(update(initialPresence(["a", "b"]), ["a"], 0), ["a", "c"], 10);
    expect(shape(moving)).toEqual(["a:present", "b:leaving", "c:entering"]);
    expect(shape(settleAllPresence(moving))).toEqual(["a:present", "c:present"]);
    const still = initialPresence<string>(["a"]);
    expect(settleAllPresence(still)).toBe(still);
  });

  it("reports the soonest phase end among several", () => {
    const closed = update(initialPresence(["a", "b"]), ["a"], 0);
    const added = update(closed, ["a", "c"], 50);
    expect(nextPresenceDeadline(added, motion)).toBe(
      Math.min(motion.leaveMs, 50 + motion.enterMs) + slack,
    );
  });
});
