import { describe, expect, it } from "vitest";
import { planProgressLine, type SessionPlanProgress } from "@/lib/sessionPlanProgress";

const step = (name: string, state: NonNullable<SessionPlanProgress["step"]>["state"]) => ({
  name,
  state,
});

describe("planProgressLine", () => {
  it("counts finished steps and names the step in progress", () => {
    expect(
      planProgressLine(
        { total: 5, done: 3, started: 1, step: step("Open PRs for safe upgrades", "started") },
        false,
      ),
    ).toEqual({ finished: 3, total: 5, lead: null, text: "Open PRs for safe upgrades", more: 0 });
  });

  it("counts the other steps running alongside the named one", () => {
    const line = planProgressLine(
      { total: 6, done: 1, started: 3, step: step("Upgrade web-app", "started") },
      false,
    );
    expect(line?.more).toBe(2);
  });

  it("says a step waits for the user unless the badge already does", () => {
    const progress = {
      total: 5,
      done: 3,
      started: 1,
      needs_input: 1,
      step: step("Approve major upgrades", "needs_input"),
    };
    expect(planProgressLine(progress, false)).toMatchObject({
      lead: { text: "Waiting for you", tone: "waiting" },
      text: "Approve major upgrades",
      // A step in progress doesn't count as waiting.
      more: 0,
    });
    expect(planProgressLine(progress, true)?.lead).toBeNull();
  });

  it("names a failure holding up the rest, and the next step between steps", () => {
    expect(
      planProgressLine({ total: 3, failed: 1, step: step("Run the tests", "failed") }, false),
    ).toMatchObject({
      finished: 1,
      lead: { text: "Failed", tone: "failed" },
      text: "Run the tests",
    });
    expect(
      planProgressLine({ total: 3, done: 1, step: step("Ship it", "next") }, false),
    ).toMatchObject({ lead: { text: "Next", tone: "muted" }, text: "Ship it" });
  });

  it("says how a finished plan ended", () => {
    expect(planProgressLine({ total: 5, done: 5, step: null }, false)).toMatchObject({
      finished: 5,
      text: "All done",
    });
    expect(planProgressLine({ total: 5, done: 4, failed: 1 }, false)).toMatchObject({
      finished: 5,
      text: "Finished, 1 failed",
    });
  });

  it("doesn't call a plan done beside a prompt for a step still in progress", () => {
    // The last step finished after an earlier one restarted, and a prompt now waits.
    const progress = { total: 3, done: 2, started: 1, step: null };
    expect(planProgressLine(progress, true)).toBeNull();
    expect(planProgressLine(progress, false)?.text).toBe("All done");
  });

  it("names the step for a state it doesn't know", () => {
    const progress = { total: 2, started: 1, step: { name: "Deploy", state: "paused" as never } };
    expect(planProgressLine(progress, false)).toMatchObject({ lead: null, text: "Deploy" });
  });

  it("shows nothing until a step has a status", () => {
    // A planning chat's proposal, or a run whose first step hasn't started.
    expect(planProgressLine({ total: 5, step: step("Inventory", "next") }, false)).toBeNull();
    expect(planProgressLine({ total: 0 }, false)).toBeNull();
  });
});
