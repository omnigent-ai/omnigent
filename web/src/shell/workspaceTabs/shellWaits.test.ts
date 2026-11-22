import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createShellWaits } from "./shellWaits";

describe("createShellWaits", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(1_000);
  });
  afterEach(() => vi.useRealTimers());

  it("times a wait from when it's first seen, and starts it over for something new", () => {
    const waits = createShellWaits();
    waits.track(new Map([["terminal:a", "shell_list"]]));
    vi.setSystemTime(5_000);
    waits.track(new Map([["terminal:a", "shell_list"]]));
    expect(waits.since("terminal:a")).toBe(1_000);
    waits.track(new Map([["terminal:a", "starting"]]));
    expect(waits.since("terminal:a")).toBe(5_000);
  });

  it("reads a wait only for what it waits for, so a new reason hasn't started yet", () => {
    const waits = createShellWaits();
    waits.track(new Map([["terminal:a", "starting"]]));
    expect(waits.since("terminal:a", "starting")).toBe(1_000);
    expect(waits.since("terminal:a", "shell_list")).toBeUndefined();
    expect(waits.since("terminal:b", "shell_list")).toBeUndefined();
  });

  it("forgets a shell that stops waiting, so its next wait is fresh", () => {
    const waits = createShellWaits();
    waits.track(new Map([["terminal:a", "shell_list"]]));
    waits.track(new Map());
    expect(waits.since("terminal:a")).toBeUndefined();
    vi.setSystemTime(9_000);
    waits.track(new Map([["terminal:a", "shell_list"]]));
    expect(waits.since("terminal:a")).toBe(9_000);
  });

  it("restarts every current wait from now", () => {
    const waits = createShellWaits();
    waits.track(
      new Map([
        ["terminal:a", "shell_list"],
        ["terminal:b", "starting"],
      ]),
    );
    vi.setSystemTime(30_000);
    waits.restart();
    expect([waits.since("terminal:a"), waits.since("terminal:b")]).toEqual([30_000, 30_000]);
  });
});
