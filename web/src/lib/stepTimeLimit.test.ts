import { describe, expect, it } from "vitest";

import {
  shortTimeLimit,
  splitTimeLimit,
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

  it("reads a description whose lines break with CRLF", () => {
    expect(stepTimeLimit("Fix it.\r\n\r\n**Time limit:** 1 hour\r\n")).toBe("1 hour");
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
    ["1 hour 30 minutes", 90, "1h 30m"],
    ["1h 30m", 90, "1h 30m"],
    ["1h30m", 90, "1h 30m"],
    ["1 day and 2 hours", 1560, "26h"],
  ])("%s is %i minutes, %s", (limit, minutes, short) => {
    expect(timeLimitMinutes(limit)).toBe(minutes);
    expect(shortTimeLimit(limit)).toBe(short);
  });

  it("reads the length a limit worded at more length starts with", () => {
    expect(timeLimitLength("2 hours. Tell me if it runs longer.")).toBe("2 hours");
    expect(timeLimitLength("1 hour 30 minutes, then tell me")).toBe("1 hour 30 minutes");
    expect(timeLimitLength("2 hours and then tell me")).toBe("2 hours");
    expect(timeLimitLength("an afternoon")).toBeNull();
  });

  it("has no length for words it can't read, or for none at all", () => {
    expect(timeLimitMinutes("0 hours")).toBeNull();
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

  it("keeps one Time limit line, the first, when a description has several", () => {
    const twice = "Fix it.\n\n**Time limit:** 1 hour\n\n**Time limit:** 2 hours\n\nShip it.";
    expect(withStepTimeLimit(twice, "4 hours")).toBe(
      "Fix it.\n\n**Time limit:** 4 hours\n\nShip it.",
    );
    expect(withStepTimeLimit(twice, null)).toBe("Fix it.\n\nShip it.");
    expect(withStepTimeLimit("Fix it.\r\n\r\n**Time limit:** 1 hour", null)).toBe("Fix it.");
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
    { id: "fix", name: "Fix", goal: withStepTimeLimit(GOAL, "4 hours"), depends_on: [] },
    { id: "ship", name: "Ship", goal: "Ship it.", depends_on: ["fix"] },
    { id: "tell", name: "Tell", goal: "**Time limit:** 1 hour", depends_on: ["ship"] },
  ];

  it("gives an older proposal the saved limits, set, changed or cleared since", () => {
    const proposed = [
      { ...saved[0]!, goal: withStepTimeLimit(GOAL, "2 hours") },
      { ...saved[1]!, goal: "Ship it.\n\n**Time limit:** 2 hours" },
      { ...saved[2]!, goal: null },
    ];
    expect(withSavedTimeLimits(proposed, saved)).toEqual(saved);
  });

  it("leaves a step the saved workflow doesn't have as proposed", () => {
    const report = { id: "report", name: "Report", goal: "**Time limit:** 1 day", depends_on: [] };
    expect(withSavedTimeLimits([report], saved)).toEqual([report]);
  });
});

describe("splitTimeLimit", () => {
  it("separates the limit from the rest of the description, wherever its line sits", () => {
    const after = `${GOAL}\n\n**Time limit:** 2 hours`;
    expect(splitTimeLimit(after)).toEqual(splitTimeLimit(withStepTimeLimit(GOAL, "2 hours")));
    expect(splitTimeLimit(after)).toEqual([GOAL, "2 hours"]);
    expect(splitTimeLimit(null)).toEqual([null, null]);
  });
});
