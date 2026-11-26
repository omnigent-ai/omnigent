import { describe, expect, it } from "vitest";

import {
  anchorDrift,
  createAlignment,
  fileAnchorOffset,
  fileViewSettled,
  switchSettled,
  type AlignOutcome,
  type AlignReading,
} from "./fileScrollAnchor";

describe("fileAnchorOffset", () => {
  it("keeps a file whose header is on screen where it is", () => {
    expect(fileAnchorOffset(493, 265)).toBe(228);
    expect(fileAnchorOffset(265, 265)).toBe(0);
  });

  it("starts a file the reader is partway into at the top of the viewport", () => {
    expect(fileAnchorOffset(-900, 265)).toBe(0);
    expect(fileAnchorOffset(264.5, 265)).toBe(0);
  });
});

describe("anchorDrift", () => {
  it("is how far the file moved from its place, in either direction", () => {
    expect(anchorDrift(600, 265, 228)).toBe(107);
    expect(anchorDrift(400, 265, 228)).toBe(-93);
  });

  it("ignores less than a pixel, so sub-pixel layout never scrolls", () => {
    expect(anchorDrift(493.6, 265, 228)).toBe(0);
    expect(anchorDrift(492.2, 265, 228)).toBe(0);
    expect(anchorDrift(494, 265, 228)).toBe(1);
  });
});

describe("fileViewSettled", () => {
  it("settles a source diff at once", () => {
    expect(fileViewSettled(false, undefined)).toBe(true);
    expect(fileViewSettled(false, { status: "loading" })).toBe(true);
  });

  it("settles a rich diff once its preview has any answer", () => {
    expect(fileViewSettled(true, undefined)).toBe(false);
    expect(fileViewSettled(true, { status: "loading", previous: [] })).toBe(false);
    expect(fileViewSettled(true, { status: "ready", sections: [] })).toBe(true);
    expect(fileViewSettled(true, { status: "failed", message: "Couldn't load." })).toBe(true);
    expect(fileViewSettled(true, { status: "unavailable", message: "Too large." })).toBe(true);
  });
});

/** A view whose target sits at `targetTop`; scrolling moves it, plus any layout shift queued. */
function alignmentHarness(options: { corrections?: number; settled?: () => boolean } = {}) {
  const state = {
    targetTop: 300,
    shiftPerScroll: 0,
    reading: undefined as AlignReading | undefined,
  };
  const outcomes: AlignOutcome[] = [];
  const frames: (() => void)[] = [];
  const scrolls: number[] = [];
  const attempt = createAlignment({
    offset: 100,
    corrections: options.corrections ?? 4,
    settled: options.settled,
    read: () => state.reading ?? { targetTop: state.targetTop, viewTop: 0 },
    scrollBy: (top) => {
      scrolls.push(top);
      state.targetTop -= top - state.shiftPerScroll;
    },
    afterLayout: (next) => frames.push(next),
    done: (outcome) => outcomes.push(outcome),
  });
  const runFrames = () => {
    while (frames.length) frames.shift()!();
  };
  return { state, outcomes, scrolls, attempt, runFrames, frames };
}

describe("createAlignment", () => {
  it("scrolls the target to its offset and reports it aligned", () => {
    const harness = alignmentHarness();
    harness.attempt();
    harness.runFrames();
    expect(harness.scrolls).toEqual([200]);
    expect(harness.outcomes).toEqual(["aligned"]);
  });

  it("reports exhausted, not aligned, when layout keeps moving the target past its corrections", () => {
    const harness = alignmentHarness({ corrections: 2 });
    harness.state.shiftPerScroll = 50;
    harness.attempt();
    harness.runFrames();
    expect(harness.scrolls).toHaveLength(2);
    expect(harness.outcomes).toEqual(["exhausted"]);
  });

  it("finishes exactly once, even with frames and renders still queued", () => {
    const harness = alignmentHarness();
    harness.attempt();
    // Two more renders queue a second correction chain before the first frame runs.
    harness.attempt();
    harness.runFrames();
    harness.attempt();
    harness.runFrames();
    expect(harness.outcomes).toEqual(["aligned"]);
    harness.state.reading = "abandoned";
    harness.attempt();
    expect(harness.outcomes).toEqual(["aligned"]);
  });

  it("waits while the target isn't rendered, and gives way when the placement goes stale", () => {
    const harness = alignmentHarness();
    harness.state.reading = "pending";
    harness.attempt();
    expect(harness.outcomes).toEqual([]);
    harness.state.reading = "abandoned";
    harness.attempt();
    expect(harness.outcomes).toEqual(["abandoned"]);
    expect(harness.scrolls).toEqual([]);
  });

  it("holds an aligned target until layout settles, looking again after each layout", () => {
    let settled = false;
    const harness = alignmentHarness({ settled: () => settled });
    harness.state.targetTop = 100;
    harness.attempt();
    // Renders asking too add no second look.
    harness.attempt();
    expect(harness.frames).toHaveLength(1);
    harness.frames.shift()!();
    expect(harness.outcomes).toEqual([]);
    // Layout moved the target while it held: corrected, then held again.
    harness.state.targetTop = 160;
    settled = true;
    harness.runFrames();
    expect(harness.scrolls).toEqual([60]);
    expect(harness.outcomes).toEqual(["aligned"]);
  });
});

describe("switchSettled", () => {
  const loading = { status: "loading" } as const;
  const failed = { status: "failed", message: "Couldn't render this file." } as const;

  it("isn't settled until the view asked for has rendered", () => {
    // Rich to source: the source diff is ready at once, but not until it renders.
    expect(switchSettled(false, true, undefined)).toBe(false);
    expect(switchSettled(false, false, undefined)).toBe(true);
    // Source to a cached, ready preview: not before the rich view renders.
    expect(switchSettled(true, false, failed)).toBe(false);
    expect(switchSettled(true, true, failed)).toBe(true);
  });

  it("waits for a rendered rich view's preview to finish loading", () => {
    expect(switchSettled(true, true, loading)).toBe(false);
    expect(switchSettled(true, true, undefined)).toBe(false);
  });
});
