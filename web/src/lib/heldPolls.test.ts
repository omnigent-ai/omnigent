import { describe, expect, it, vi } from "vitest";

import { createHeldPolls } from "./heldPolls";

const poll = () => ({ observers: [{ options: { queryKey: ["q"] }, setOptions: vi.fn() }] });

describe("createHeldPolls", () => {
  it("lets a released poll go, and counts it once when it's held again", () => {
    const held = createHeldPolls();
    const first = poll();
    held.hold(first);
    held.release(first);
    expect(held.size()).toBe(0);
    held.hold(first);
    held.hold(first);
    expect(held.size()).toBe(1);
    held.reschedule();
    expect(first.observers[0]!.setOptions).toHaveBeenCalledTimes(1);
  });

  it("lets go of a poll whose observers all left", () => {
    const held = createHeldPolls();
    const left = { observers: [] };
    held.hold(left);
    expect(held.size()).toBe(0);
  });
});
