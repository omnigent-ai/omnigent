import { describe, expect, it } from "vitest";

import { type BulkOutcome, type BulkProgress, runBulk } from "./pullRequestBulkRunner";

const urls = ["pr-1", "pr-2", "pr-3", "pr-4", "pr-5"];

/** A step whose answers the test gives, recording how many are in flight. */
function controlledStep() {
  const pending = new Map<string, (outcome: BulkOutcome) => void>();
  let inFlight = 0;
  let mostInFlight = 0;
  const started: string[] = [];
  const step = (url: string) =>
    new Promise<BulkOutcome>((resolve) => {
      started.push(url);
      inFlight += 1;
      mostInFlight = Math.max(mostInFlight, inFlight);
      pending.set(url, (outcome) => {
        inFlight -= 1;
        resolve(outcome);
      });
    });
  const answer = async (url: string, outcome: BulkOutcome) => {
    pending.get(url)!(outcome);
    pending.delete(url);
    await Promise.resolve();
    await Promise.resolve();
  };
  return { step, answer, started, mostInFlight: () => mostInFlight };
}

describe("runBulk", () => {
  it("runs at most three at a time, in order, and reports each outcome", async () => {
    const control = controlledStep();
    const progress: BulkProgress[] = [];
    const run = runBulk(urls, control.step, {
      onProgress: (next) => progress.push({ ...next, outcomes: new Map(next.outcomes) }),
    });
    expect(control.started).toEqual(["pr-1", "pr-2", "pr-3"]);
    await control.answer("pr-2", "refused");
    expect(control.started).toEqual(["pr-1", "pr-2", "pr-3", "pr-4"]);
    await control.answer("pr-1", "applied");
    await control.answer("pr-3", "uncertain");
    await control.answer("pr-4", "applied");
    await control.answer("pr-5", "applied");
    const result = await run;
    expect(control.mostInFlight()).toBe(3);
    expect(Object.fromEntries(result.outcomes)).toEqual({
      "pr-1": "applied",
      "pr-2": "refused",
      "pr-3": "uncertain",
      "pr-4": "applied",
      "pr-5": "applied",
    });
    expect(progress.map((entry) => entry.done)).toEqual([1, 2, 3, 4, 5, 5]);
  });

  it("starts nothing more once its account changed; those sent settle as they would", async () => {
    const control = controlledStep();
    let current = true;
    const run = runBulk(urls, control.step, { stillCurrent: () => current });
    current = false;
    await control.answer("pr-1", "applied");
    await control.answer("pr-2", "applied");
    await control.answer("pr-3", "refused");
    const result = await run;
    expect(control.started).toEqual(["pr-1", "pr-2", "pr-3"]);
    expect(Object.fromEntries(result.outcomes)).toEqual({
      "pr-1": "applied",
      "pr-2": "applied",
      "pr-3": "refused",
      "pr-4": "skipped",
      "pr-5": "skipped",
    });
  });

  it("counts a step that throws as refused and goes on", async () => {
    const result = await runBulk(["pr-1", "pr-2"], async (url) => {
      if (url === "pr-1") throw new Error("boom");
      return "applied";
    });
    expect(Object.fromEntries(result.outcomes)).toEqual({ "pr-1": "refused", "pr-2": "applied" });
  });
});
