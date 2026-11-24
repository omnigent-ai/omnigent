import { describe, expect, it } from "vitest";

import {
  shortTimeLimit,
  stepTimeLimit,
  timeLimitLength,
  timeLimitMinutes,
  withSavedTimeLimits,
  withStepTimeLimit,
} from "./stepTimeLimit";

const GOAL = [
  "**Inputs:** the PR's checks.",
  "",
  "**Outputs:** the PR with passing checks.",
  "",
  "**Details:** run `gh pr checks <number>`.",
].join("\n");

describe("stepTimeLimit", () => {
  it("reads the limit from its own line, in any case", () => {
    expect(stepTimeLimit(withStepTimeLimit(GOAL, "2 hours"))).toBe("2 hours");
    expect(stepTimeLimit("**time limit:** 30 minutes, then tell me")).toBe(
      "30 minutes, then tell me",
    );
  });

  it("finds none without the line, or with nothing after it", () => {
    expect(stepTimeLimit(GOAL)).toBeNull();
    expect(stepTimeLimit(null)).toBeNull();
    expect(stepTimeLimit("**Time limit:**")).toBeNull();
    expect(stepTimeLimit("Mind the **Time limit:** here")).toBeNull();
  });
});

describe("timeLimitMinutes and shortTimeLimit", () => {
  it.each([
    ["2 hours", 120, "2h"],
    ["30 minutes", 30, "30m"],
    ["1 day", 1440, "24h"],
    ["90 min", 90, "1h 30m"],
    ["1.5h, then tell me", 90, "1h 30m"],
  ])("%s is %i minutes, %s", (limit, minutes, short) => {
    expect(timeLimitMinutes(limit)).toBe(minutes);
    expect(shortTimeLimit(limit)).toBe(short);
  });

  it("reads the length a limit worded at more length starts with", () => {
    expect(timeLimitLength("2 hours. Tell me if it runs longer.")).toBe("2 hours");
    expect(timeLimitLength("an afternoon")).toBeNull();
  });

  it("has no length for words it can't read", () => {
    expect(timeLimitMinutes("an afternoon")).toBeNull();
    expect(timeLimitMinutes("2 months")).toBeNull();
    expect(shortTimeLimit("an afternoon")).toBeNull();
  });
});

describe("withStepTimeLimit", () => {
  it("puts a new limit before Details, as a paragraph of its own", () => {
    expect(withStepTimeLimit(GOAL, "2 hours")).toBe(
      [
        "**Inputs:** the PR's checks.",
        "",
        "**Outputs:** the PR with passing checks.",
        "",
        "**Time limit:** 2 hours",
        "",
        "**Details:** run `gh pr checks <number>`.",
      ].join("\n"),
    );
    expect(withStepTimeLimit("Fix it.\n**Details:** run the tests.", "1 hour")).toBe(
      "Fix it.\n\n**Time limit:** 1 hour\n\n**Details:** run the tests.",
    );
  });

  it("puts it last without Details, and alone without a description", () => {
    expect(withStepTimeLimit("Fix the test.", "1 hour")).toBe(
      "Fix the test.\n\n**Time limit:** 1 hour",
    );
    expect(withStepTimeLimit(null, "1 day")).toBe("**Time limit:** 1 day");
  });

  it("replaces the limit's line where it is", () => {
    const withLimit = "Fix it.\n\n**Time limit:** 2 hours. Tell me after.\n\n**Details:** run it.";
    expect(withStepTimeLimit(withLimit, "4 hours")).toBe(
      "Fix it.\n\n**Time limit:** 4 hours\n\n**Details:** run it.",
    );
  });

  it("removes the line and one blank line around it, and leaves a description without one alone", () => {
    expect(withStepTimeLimit(withStepTimeLimit(GOAL, "2 hours"), null)).toBe(GOAL);
    expect(withStepTimeLimit("Fix it.\n\n**Time limit:** 1 hour", null)).toBe("Fix it.");
    expect(withStepTimeLimit("**Time limit:** 1 hour", null)).toBeNull();
    expect(withStepTimeLimit(GOAL, null)).toBe(GOAL);
  });
});

describe("withSavedTimeLimits", () => {
  const saved = [
    { id: "fix", name: "Fix", goal: withStepTimeLimit(GOAL, "2 hours"), depends_on: [] },
    { id: "ship", name: "Ship", goal: "**Time limit:** 1 hour", depends_on: ["fix"] },
  ];

  it("keeps a saved step's limit in a proposal that doesn't word one", () => {
    const proposed = [
      { ...saved[0]!, goal: GOAL },
      { ...saved[1]!, goal: null },
    ];
    expect(withSavedTimeLimits(proposed, saved)).toEqual(saved);
  });

  it("keeps the proposal's own limit, and adds none to a step that wasn't saved", () => {
    const proposed = [
      { ...saved[0]!, goal: withStepTimeLimit(GOAL, "4 hours") },
      { id: "report", name: "Report", goal: null, depends_on: [] },
    ];
    expect(withSavedTimeLimits(proposed, saved)).toEqual(proposed);
  });
});
