import { afterEach, describe, expect, it } from "vitest";

import { runningBulkRuns, startBulkRun } from "./bulkRuns";
import {
  isBeingRemoved,
  REMOVAL_PENDING_REASON,
  removalBusyLabel,
  removalIncludes,
  removalPendingReason,
  type RemovalProgress,
  removalStopper,
} from "./removalProgress";
import { pullRequestKey } from "@/lib/pullRequestReference";

const PR_A = "https://github.com/example/app/pull/1";
const PR_B = "https://github.com/example/app/pull/2";

function progress(overrides: Partial<RemovalProgress> = {}): RemovalProgress {
  return {
    phase: "removing",
    urls: new Set([pullRequestKey(PR_A)]),
    done: 3,
    total: 8,
    stop: "available",
    runId: "run-1",
    ...overrides,
  };
}

describe("removalPendingReason", () => {
  it("counts a batch the user's rows are in", () => {
    expect(removalPendingReason(progress(), true)).toBe("Removing 3 of 8 pull requests…");
  });

  it("asks rows outside the batch to wait for it", () => {
    expect(removalPendingReason(progress(), false)).toBe(
      "Wait for the 8 pull requests being removed to finish.",
    );
  });

  it("never offers Stop for a single removal, which has none", () => {
    const single = progress({ done: 0, total: 1, stop: "none" });
    expect(removalPendingReason(single, true)).toBe("Removing this pull request…");
    expect(removalPendingReason(single, false)).toBe(
      "Wait for the pull request being removed to finish.",
    );
  });

  it("says it's stopping once Stop was asked", () => {
    const stopping = progress({ stop: "requested" });
    expect(removalPendingReason(stopping, true)).toBe("Stopping after the requests already sent…");
    expect(removalPendingReason(stopping, false)).toBe("Stopping after the requests already sent…");
  });

  it("says a queued removal is waiting, with no count or Stop", () => {
    const queued = progress({ phase: "waiting", done: null, stop: "none" });
    expect(removalPendingReason(queued, true)).toBe("Waiting for another removal to finish.");
    expect(removalPendingReason(queued, false)).toBe("Waiting for another removal to finish.");
  });

  it("falls back to the plain wait without progress", () => {
    expect(removalPendingReason(null, true)).toBe(REMOVAL_PENDING_REASON);
  });
});

describe("removalBusyLabel", () => {
  it("counts a batch, and stays short for one or a queued removal", () => {
    expect(removalBusyLabel(progress())).toBe("Removing 3 of 8");
    expect(removalBusyLabel(progress({ total: 1, done: 0 }))).toBe("Removing…");
    expect(removalBusyLabel(progress({ phase: "waiting", done: null }))).toBe("Waiting…");
  });

  it("says it's stopping once Stop was asked", () => {
    expect(removalBusyLabel(progress({ stop: "requested", runId: null }))).toBe("Stopping…");
  });
});

describe("removalStopper", () => {
  const ends: (() => void)[] = [];
  afterEach(() => {
    for (const end of ends.splice(0)) end();
  });

  it("stops the running batch it names", () => {
    const run = startBulkRun({ label: "Removing pull requests", total: 8 });
    ends.push(run.end);
    const stop = removalStopper(progress({ runId: run.id }));
    expect(stop).toEqual(expect.any(Function));
    stop!();
    expect(runningBulkRuns().find((snapshot) => snapshot.id === run.id)?.stopRequested).toBe(true);
  });

  it("offers no Stop for a removal without one, one asked to stop, or one queued", () => {
    expect(removalStopper(null)).toBeNull();
    expect(removalStopper(progress({ stop: "none", runId: null }))).toBeNull();
    expect(removalStopper(progress({ stop: "requested" }))).toBeNull();
    expect(removalStopper(progress({ phase: "waiting", done: null }))).toBeNull();
    expect(removalStopper(progress({ runId: null }))).toBeNull();
  });
});

describe("isBeingRemoved and removalIncludes", () => {
  it("match canonical URLs, and only a running removal marks rows as being removed", () => {
    expect(isBeingRemoved(progress(), "https://GitHub.com/example/app/pull/1/")).toBe(true);
    expect(isBeingRemoved(progress(), PR_B)).toBe(false);
    expect(isBeingRemoved(progress({ phase: "waiting" }), PR_A)).toBe(false);
    expect(removalIncludes(progress({ phase: "waiting" }), [PR_B, PR_A])).toBe(true);
    expect(removalIncludes(null, [PR_A])).toBe(false);
  });
});
