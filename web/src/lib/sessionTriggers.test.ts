import { describe, expect, it } from "vitest";
import { describeSessionTrigger, describeTaskTrigger } from "./sessionTriggers";

describe("describeSessionTrigger", () => {
  it.each([
    [["turn.completed"], undefined, "When a turn finishes"],
    [["turn.completed", "turn.failed"], undefined, "When a turn finishes or a turn fails"],
    [
      ["session.created", "session.renamed", "input.requested"],
      "deploy",
      'When a session is created, a session is renamed or an agent asks for input in sessions titled "deploy"',
    ],
  ] as const)("describes %j", (events, titleContains, expected) => {
    expect(describeSessionTrigger({ events: [...events], titleContains })).toBe(expected);
  });

  it("prefers the trigger and otherwise restates the schedule", () => {
    expect(
      describeTaskTrigger({ rrule: null, sessionTrigger: { events: ["comment.created"] } }),
    ).toBe("When someone comments");
    expect(
      describeTaskTrigger({ rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0", sessionTrigger: null }),
    ).toBe("Every day at 9:00 AM");
  });
});
