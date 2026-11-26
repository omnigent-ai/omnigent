import { describe, expect, it } from "vitest";
import { PullRequestTiming } from "@/lib/pullRequestTiming";

const timing = (cached = false) => new PullRequestTiming("conv_1", "host", cached, 1000);

describe("PullRequestTiming", () => {
  it("reports each milestone's first time, relative to the info request", () => {
    const run = timing();
    run.mark("state", 1820.4, "ready");
    run.mark("state", 5000, "error");
    run.mark("header", 1830);
    expect(run.complete).toBe(false);
    run.mark("diff", 2400);
    expect(run.complete).toBe(true);
    expect(run.takeReport()).toEqual({
      sessionId: "conv_1",
      path: "host",
      outcome: "ready",
      cached: false,
      stateMs: 820,
      headerMs: 830,
      diffMs: 1400,
    });
  });

  it("splits a diff's time into its request, its read and its showing in Changes", () => {
    const run = timing();
    run.mark("state", 1800, "ready");
    run.mark("header", 1800);
    run.showChanges(true, 1800);
    run.mark("patchRequested", 1810);
    run.mark("diff", 3000);
    // Read, with Changes waiting on it: the opening goes on until the diff shows there.
    expect(run.complete).toBe(false);
    run.mark("diffShown", 3050);
    expect(run.complete).toBe(true);
    expect(run.takeReport()).toMatchObject({
      changesShownMs: 800,
      patchRequestedMs: 810,
      diffMs: 2000,
      diffShownMs: 2050,
    });
  });

  it("reports at the read when the user left Changes before the patch answered", () => {
    const run = timing();
    run.mark("state", 1800, "ready");
    run.showChanges(true, 1900);
    run.showChanges(false, 2100);
    run.mark("diff", 3000);
    expect(run.complete).toBe(true);
    const report = run.takeReport();
    expect(report).toMatchObject({ changesShownMs: 900, diffMs: 2000 });
    expect(report).not.toHaveProperty("diffShownMs");
  });

  it("keeps the first time Changes showed when it shows again", () => {
    const run = timing();
    run.mark("state", 1800, "ready");
    run.showChanges(true, 1900);
    run.showChanges(false, 2100);
    run.showChanges(true, 2500);
    run.mark("diff", 3000);
    expect(run.complete).toBe(false);
    run.mark("diffShown", 3010);
    expect(run.takeReport()).toMatchObject({ changesShownMs: 900, diffShownMs: 2010 });
  });

  it("reports at the diff's read when Changes isn't showing, as nobody waits on it", () => {
    const run = timing();
    run.mark("state", 1800, "ready");
    run.mark("patchRequested", 1810);
    run.mark("diff", 3000);
    expect(run.complete).toBe(true);
    const report = run.takeReport();
    expect(report).toMatchObject({ patchRequestedMs: 810, diffMs: 2000 });
    expect(report).not.toHaveProperty("changesShownMs");
    expect(report).not.toHaveProperty("diffShownMs");
  });

  it("completes at a whole-panel state, with no header or diff", () => {
    const run = timing(true);
    run.mark("state", 1003, "repo-unresolved");
    expect(run.complete).toBe(true);
    expect(run.takeReport()).toEqual({
      sessionId: "conv_1",
      path: "host",
      outcome: "repo-unresolved",
      cached: true,
      stateMs: 3,
    });
  });

  it("reports once", () => {
    const run = timing();
    run.mark("state", 1100, "no-pr");
    expect(run.takeReport()).not.toBeNull();
    expect(run.takeReport()).toBeNull();
  });

  it("reports nothing before the panel leaves loading, but keeps a header reported first", () => {
    const run = timing();
    run.mark("header", 1100);
    expect(run.complete).toBe(false);
    expect(run.takeReport()).toBeNull();
    run.mark("state", 1100, "ready");
    run.mark("diff", 1500);
    expect(run.takeReport()).toMatchObject({ stateMs: 100, headerMs: 100, diffMs: 500 });
  });

  it("waits for a state that names its outcome", () => {
    const run = timing();
    run.mark("state", 1200);
    expect(run.complete).toBe(false);
    run.mark("state", 1500, "error");
    expect(run.takeReport()).toMatchObject({ outcome: "error", stateMs: 500 });
  });

  it("takes Changes showing only through showChanges", () => {
    const run = timing();
    // @ts-expect-error -- marking it directly would skip waiting for the diff to show
    run.mark("changesShown", 1100);
  });

  it("drops a duration from a suspended window", () => {
    const run = timing();
    run.mark("state", 1000 + 10 * 60_000 + 1, "ready");
    expect(run.takeReport()).toBeNull();
  });
});
