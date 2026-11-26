import { describe, expect, it } from "vitest";

import {
  shortTimeLimit,
  splitTimeLimit,
  stepTimeLimit,
  withTimeLimitLength,
  timeLimitLength,
  timeLimitMinutes,
  sameStepsApartFromTimeLimits,
  withChosenTimeLimit,
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

  it("breaks lines where Markdown does, at a lone CR too", () => {
    expect(stepTimeLimit("Fix it.\r**Time limit:** 2 hours")).toBe("2 hours");
    expect(stepTimeLimit("Example:\r\r```\r**Time limit:** 2 hours\r```")).toBeNull();
    expect(withStepTimeLimit("Fix it.\r\r**Details:** run it.", "1 hour")).toBe(
      "Fix it.\r\r**Time limit:** 1 hour\r\r**Details:** run it.",
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

  it("goes by whole minutes, and has no length under one or too long to count", () => {
    expect(timeLimitMinutes("1.333h")).toBe(80);
    expect(timeLimitMinutes("0.001 minutes")).toBeNull();
    expect(timeLimitMinutes(`${"9".repeat(310)} hours`)).toBeNull();
    expect(shortTimeLimit(`${"9".repeat(310)} hours`)).toBeNull();
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
    // Where Details continues a paragraph, the limit joins it, so clearing it undoes it.
    expect(withStepTimeLimit("Fix it.\n**Details:** run the tests.", "1 hour")).toBe(
      "Fix it.\n**Time limit:** 1 hour\n**Details:** run the tests.",
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

  it("reads and edits the line as a list item, and joins a list that holds Details", () => {
    const listed =
      "- **Outputs:** passing CI.\n- **Time limit:** 2 hours\n- **Details:** rerun it.";
    expect(stepTimeLimit(listed)).toBe("2 hours");
    expect(withStepTimeLimit(listed, "4 hours")).toBe(
      "- **Outputs:** passing CI.\n- **Time limit:** 4 hours\n- **Details:** rerun it.",
    );
    expect(
      withStepTimeLimit("- **Outputs:** passing CI.\n- **Details:** rerun it.", "1 hour"),
    ).toBe("- **Outputs:** passing CI.\n- **Time limit:** 1 hour\n- **Details:** rerun it.");
    expect(withStepTimeLimit("Fix it.\n\n  **Details:** rerun it.", "1 hour")).toBe(
      "Fix it.\n\n  **Time limit:** 1 hour\n\n  **Details:** rerun it.",
    );
  });

  it.each([
    ["the colon after the bold", "**Time limit**: 2 hours", "**Time limit:** 4 hours"],
    [
      "a numbered item",
      "1. Fix it.\n2. **Time limit:** 2 hours",
      "1. Fix it.\n2. **Time limit:** 4 hours",
    ],
    ["a quote", "> **Time limit:** 2 hours\n> Tell me.", "> **Time limit:** 4 hours\n> Tell me."],
  ])("reads and edits a limit written with %s", (_, written, changed) => {
    expect(stepTimeLimit(written)).toBe("2 hours");
    expect(withStepTimeLimit(written, "4 hours")).toBe(changed);
    expect(stepTimeLimit(withStepTimeLimit(written, null))).toBeNull();
  });

  it.each([
    "Fix it.\n\n**Details:** run it.",
    "Fix it.\n**Details:** run it.",
    "**Details:** run it.",
    "> **Details:** rerun it.",
    "> Fix it.\n>\n> **Details:** rerun it.",
    "> Fix it.\n> **Details:** rerun it.",
    "Intro\n\n> **Details:** x",
    "Intro\n> **Details:** x",
    "- Fix it.\n\n  **Details:** rerun it.",
    "- Fix it.\n  **Details:** rerun it.",
    "1. Fix it.\n2. **Details:** rerun it.",
    "Fix it.\r\n\r\n**Details:** run it.",
    "Fix it.\r**Details:** run it.",
    "Fix it.",
    "Fix it.\n\n```\nsome code",
    "Fix it.\n```\nsome code",
    "Fix it.\r\n\r\n```\nmake test\n```",
    "Fix it.\r\n\r\n**Details:** run it.\nThen check it.",
    "Fix it.\n\r\n**Details:** run it.",
    GOAL,
  ])("gives %j back once a limit set on it is cleared", (goal) => {
    const limited = withStepTimeLimit(goal, "1 hour");
    expect(stepTimeLimit(limited)).toBe("1 hour");
    expect(withStepTimeLimit(limited, null)).toBe(goal);
  });

  it("breaks a new line as the line before it, and leaves the other breaks alone", () => {
    expect(withStepTimeLimit("Fix it.\r\n\r\n```\nmake test\n```", "1 hour")).toBe(
      "Fix it.\r\n\r\n```\nmake test\n```\n\n**Time limit:** 1 hour",
    );
    expect(withStepTimeLimit("Fix it.\r\n\r\n**Details:** run it.\nThen check it.", "1 hour")).toBe(
      "Fix it.\r\n\r\n**Time limit:** 1 hour\r\n\r\n**Details:** run it.\nThen check it.",
    );
  });

  it("puts a new limit in the list item or quote that holds Details", () => {
    expect(withStepTimeLimit("- Fix it.\n\n  **Details:** rerun it.", "1 hour")).toBe(
      "- Fix it.\n\n  **Time limit:** 1 hour\n\n  **Details:** rerun it.",
    );
    const quoted = "> Fix it.\n>\n> **Details:** rerun it.";
    expect(withStepTimeLimit(quoted, "1 hour")).toBe(
      "> Fix it.\n>\n> **Time limit:** 1 hour\n>\n> **Details:** rerun it.",
    );
    // Clearing it gives back the quote as it was.
    expect(withStepTimeLimit(withStepTimeLimit(quoted, "1 hour"), null)).toBe(quoted);
  });

  it("puts a new limit before Details written another way", () => {
    expect(withStepTimeLimit("Fix it.\n\n**Details**: rerun it.", "1 hour")).toBe(
      "Fix it.\n\n**Time limit:** 1 hour\n\n**Details**: rerun it.",
    );
    expect(withStepTimeLimit("1. Fix it.\n2. **Details:** rerun it.", "1 hour")).toBe(
      "1. Fix it.\n2. **Time limit:** 1 hour\n2. **Details:** rerun it.",
    );
  });

  it.each([
    ["Fix it.\n\n```markdown\n**Time limit:** 1 day\n```"],
    // A shorter fence, or one with an info string, doesn't close a longer one.
    ["Fix it.\n\n````\n```\n**Time limit:** 1 day\n````"],
    ["Fix it.\n\n```\n```js\n**Time limit:** 1 day\n```"],
  ])("leaves a Time limit line in a fenced code block alone: %j", (example) => {
    expect(stepTimeLimit(example)).toBeNull();
    expect(withStepTimeLimit(example, "2 hours")).toBe(`${example}\n\n**Time limit:** 2 hours`);
    expect(withStepTimeLimit(example, null)).toBe(example);
  });

  it("leaves a Time limit line in an indented code block alone", () => {
    const example = [
      "Copy this example:",
      "",
      "    **Time limit:** 2 hours",
      "",
      "**Details:** Keep the example unchanged.",
    ].join("\n");
    expect(stepTimeLimit(example)).toBeNull();
    expect(withStepTimeLimit(example, "1 hour")).toBe(
      [
        "Copy this example:",
        "",
        "    **Time limit:** 2 hours",
        "",
        "**Time limit:** 1 hour",
        "",
        "**Details:** Keep the example unchanged.",
      ].join("\n"),
    );
    expect(withStepTimeLimit(example, null)).toBe(example);
  });

  it("puts the limit before a code block left open at the end, where it stays readable", () => {
    const open = "Fix it.\n\n```\nsome code";
    const limited = withStepTimeLimit(open, "2 hours");
    expect(limited).toBe("Fix it.\n\n**Time limit:** 2 hours\n\n```\nsome code");
    expect(stepTimeLimit(limited)).toBe("2 hours");
    expect(withStepTimeLimit(limited, "4 hours")).toBe(
      "Fix it.\n\n**Time limit:** 4 hours\n\n```\nsome code",
    );
    // With Details, the limit goes before it as usual.
    expect(withStepTimeLimit("**Details:** run it.\n\n```\nsome code", "1 hour")).toBe(
      "**Time limit:** 1 hour\n\n**Details:** run it.\n\n```\nsome code",
    );
    // A fence indented four spaces is code, not the block's end.
    const unclosed = withStepTimeLimit("Fix it.\n\n```\nsome code\n    ```", "2 hours");
    expect(unclosed).toBe("Fix it.\n\n**Time limit:** 2 hours\n\n```\nsome code\n    ```");
    expect(stepTimeLimit(unclosed)).toBe("2 hours");
    // A list item's fence, indented with the item, does close its block.
    const listed = withStepTimeLimit("- Run it:\n  ```\n  make test\n  ```", "2 hours");
    expect(listed).toBe("- Run it:\n  ```\n  make test\n  ```\n\n**Time limit:** 2 hours");
    expect(stepTimeLimit(listed)).toBe("2 hours");
  });

  it("leaves a Time limit line inside inline code that runs across lines alone", () => {
    const example = "Copy `example\n**Time limit:** 2 hours\nend` unchanged.";
    expect(stepTimeLimit(example)).toBeNull();
    expect(withStepTimeLimit(example, null)).toBe(example);
    const limited = withStepTimeLimit(example, "1 hour");
    expect(limited).toBe(`${example}\n\n**Time limit:** 1 hour`);
    expect(stepTimeLimit(limited)).toBe("1 hour");
    expect(withStepTimeLimit(limited, "4 hours")).toBe(`${example}\n\n**Time limit:** 4 hours`);
    expect(withStepTimeLimit(limited, null)).toBe(example);
  });

  it("changes only the edited lines: their breaks and the first line's indent stay", () => {
    expect(withStepTimeLimit("Fix it.\r\n\r\n**Details:** run it.", "1 hour")).toBe(
      "Fix it.\r\n\r\n**Time limit:** 1 hour\r\n\r\n**Details:** run it.",
    );
    expect(withStepTimeLimit("    indented code\n\nFix it.", "1 hour")).toBe(
      "    indented code\n\nFix it.\n\n**Time limit:** 1 hour",
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

describe("withTimeLimitLength", () => {
  it("changes only the length, keeping the words after it", () => {
    const noted = "Fix it.\n\n**Time limit:** 4 hours. Tell me if it runs longer.";
    expect(withTimeLimitLength(noted, "8 hours")).toBe(
      "Fix it.\n\n**Time limit:** 8 hours. Tell me if it runs longer.",
    );
    expect(withTimeLimitLength("**Time limit:** an afternoon", "1 hour")).toBe(
      "**Time limit:** 1 hour",
    );
    expect(withTimeLimitLength("Fix it.", "2 hours")).toBe("Fix it.\n\n**Time limit:** 2 hours");
    // A no-break space before the length goes, and the words after it stay whole.
    expect(withTimeLimitLength("**Time limit:**\u00a02 hours. Tell me.", "8 hours")).toBe(
      "**Time limit:** 8 hours. Tell me.",
    );
  });

  it("sets the length a menu chose, and clears the line for no limit", () => {
    const noted = "Fix it.\n\n**Time limit:** 4 hours. Tell me after.";
    expect(withChosenTimeLimit(noted, "8 hours")).toBe(
      "Fix it.\n\n**Time limit:** 8 hours. Tell me after.",
    );
    expect(withChosenTimeLimit(noted, null)).toBe("Fix it.");
    expect(withChosenTimeLimit("**Time limit:** an afternoon", "1 hour")).toBe(
      "**Time limit:** 1 hour",
    );
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

describe("sameStepsApartFromTimeLimits", () => {
  const steps = [
    { id: "fix", name: "Fix", goal: "Fix it.", depends_on: [] },
    { id: "ship", name: "Ship", goal: null, depends_on: ["fix"] },
  ];

  it("holds two plans the same when only their time limits differ", () => {
    const limited = [{ ...steps[0]!, goal: "Fix it.\n\n**Time limit:** 2 hours" }, steps[1]!];
    expect(sameStepsApartFromTimeLimits(limited, steps)).toBe(true);
  });

  it.each([
    ["a name", [{ ...steps[0]!, name: "Repair" }, steps[1]!]],
    ["a goal", [{ ...steps[0]!, goal: "Fix it twice." }, steps[1]!]],
    ["a dependency", [steps[0]!, { ...steps[1]!, depends_on: [] }]],
    ["the order", [steps[1]!, steps[0]!]],
  ])("tells two plans apart by %s", (_, changed) => {
    expect(sameStepsApartFromTimeLimits(changed, steps)).toBe(false);
  });
});

describe("splitTimeLimit", () => {
  it("separates the limit from the rest of the description, wherever its line sits", () => {
    const after = `${GOAL}\n\n**Time limit:** 2 hours`;
    expect(splitTimeLimit(after)).toEqual(splitTimeLimit(withStepTimeLimit(GOAL, "2 hours")));
    expect(splitTimeLimit(after)).toEqual([GOAL, "2 hours"]);
    expect(splitTimeLimit(null)).toEqual([null, null]);
  });

  // Matching that backtracked over a long run of spaces would take minutes on these.
  it("reads descriptions with long runs of spaces quickly", () => {
    const spaces = " ".repeat(200_000);
    const example = `Copy this example:\n\n${spaces}x`;
    expect(splitTimeLimit(example)).toEqual([example, null]);
    // With a limit to find, every line goes through the Time limit and Details patterns.
    expect(splitTimeLimit(`**Time limit:** 1 hour\n\n${spaces}x`)).toEqual([
      `${spaces}x`,
      "1 hour",
    ]);
    expect(withStepTimeLimit(`Fix it.\n${spaces}\n**Details:** x`, "1 hour")).toBe(
      `Fix it.\n${spaces}\n**Time limit:** 1 hour\n\n**Details:** x`,
    );
    expect(stepTimeLimit(`**Time limit:** a${spaces}b`)).toBe(`a${spaces}b`);
    expect(timeLimitMinutes(`1h${spaces}30m`)).toBe(90);
  });
});
