// An open PR's checks poll: fast after a change, then slower, never stopping, and only a real
// change of head or checks starts the fast phase again.

import { describe, expect, it } from "vitest";

import {
  CHECKS_FAST_POLL_MS,
  CHECKS_SLOW_POLL_MS,
  checksChangedAt,
  NO_CHECKS_DISCOVERY_POLL_MS,
  pacedChecksPollInterval,
} from "./pullRequestChecksPacing";

describe("pacedChecksPollInterval", () => {
  it("reads fast for 2 minutes after a change, then every 30 s", () => {
    expect(pacedChecksPollInterval(0, false)).toBe(CHECKS_FAST_POLL_MS);
    expect(pacedChecksPollInterval(119_999, false)).toBe(CHECKS_FAST_POLL_MS);
    expect(pacedChecksPollInterval(120_000, false)).toBe(CHECKS_SLOW_POLL_MS);
    expect(pacedChecksPollInterval(60 * 60_000, false)).toBe(CHECKS_SLOW_POLL_MS);
  });

  it("looks for checks every 2 minutes once none have appeared for 10, and never stops", () => {
    expect(pacedChecksPollInterval(599_999, true)).toBe(CHECKS_SLOW_POLL_MS);
    expect(pacedChecksPollInterval(600_000, true)).toBe(NO_CHECKS_DISCOVERY_POLL_MS);
    expect(pacedChecksPollInterval(24 * 60 * 60_000, true)).toBe(NO_CHECKS_DISCOVERY_POLL_MS);
  });
});

describe("checksChangedAt", () => {
  const checks = (pending: number) => ({ pending, total: 1 });

  it("keeps the time of the last change across answers that show the same head and checks", () => {
    const query = {};
    expect(checksChangedAt(query, { head: "a1", checks: checks(1) }, 1_000)).toBe(1_000);
    expect(checksChangedAt(query, { head: "a1", checks: checks(1) }, 9_000)).toBe(1_000);
  });

  it("moves on a push or a check change, and back again (A-B-A)", () => {
    const query = {};
    checksChangedAt(query, { head: "a1", checks: checks(1) }, 1_000);
    expect(checksChangedAt(query, { head: "b2", checks: checks(1) }, 2_000)).toBe(2_000);
    expect(checksChangedAt(query, { head: "b2", checks: checks(0) }, 3_000)).toBe(3_000);
    expect(checksChangedAt(query, { head: "b2", checks: checks(1) }, 4_000)).toBe(4_000);
  });

  it("keeps each query's history apart", () => {
    const first = {};
    const second = {};
    checksChangedAt(first, { head: "a1", checks: checks(1) }, 1_000);
    expect(checksChangedAt(second, { head: "a1", checks: checks(1) }, 5_000)).toBe(5_000);
    expect(checksChangedAt(first, { head: "a1", checks: checks(1) }, 6_000)).toBe(1_000);
  });
});
