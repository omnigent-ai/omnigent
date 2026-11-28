import { describe, expect, it } from "vitest";
import { pullRequestTableLayout, type PullRequestTableLayout } from "./pullRequestTableLayout";

function shape(layout: PullRequestTableLayout) {
  return layout.kind === "rows"
    ? "rows"
    : `${layout.columns.join(" ")}${layout.statusText ? "" : " (dot)"}`;
}

describe("pullRequestTableLayout", () => {
  it.each([
    [900, "number title status checks diff reviewers updated actions"],
    [574, "number title status checks diff reviewers updated actions"],
    [573, "number title status checks diff updated actions"],
    [504, "number title status checks diff updated actions"],
    [503, "number title status checks updated actions"],
    [430, "number title status checks updated actions"],
    [429, "number title status checks updated actions (dot)"],
    [370, "number title status checks updated actions (dot)"],
    [369, "number title status updated actions (dot)"],
    [336, "number title status updated actions (dot)"],
    [335, "rows"],
    [0, "rows"],
  ])(
    "drops Reviewers, Diff, Status text, then CI as the list narrows (%ipx)",
    (width, expected) => {
      expect(shape(pullRequestTableLayout(width, { statsColumns: true }))).toBe(expected);
    },
  );

  it.each([
    [570, "number title status checks reviewers actions"],
    [430, "number title status checks reviewers actions"],
    [429, "number title status checks actions"],
    [360, "number title status checks actions"],
    [359, "number title status checks actions (dot)"],
    [299, "number title status actions (dot)"],
    [266, "number title status actions (dot)"],
    [265, "rows"],
  ])(
    "never shows Diff or Updated without stats, so columns fit sooner (%ipx)",
    (width, expected) => {
      expect(shape(pullRequestTableLayout(width, { statsColumns: false }))).toBe(expected);
    },
  );

  it("widens the # column for long references, so columns drop sooner", () => {
    // `#1032858`: four more digits than `#123`, 30px wider.
    expect(shape(pullRequestTableLayout(574, { statsColumns: true, numberLength: 8 }))).toBe(
      "number title status checks diff updated actions",
    );
    expect(shape(pullRequestTableLayout(604, { statsColumns: true, numberLength: 8 }))).toBe(
      "number title status checks diff reviewers updated actions",
    );
    expect(pullRequestTableLayout(604, { statsColumns: true, numberLength: 8 })).toMatchObject({
      gridTemplate: "62px minmax(0, 1fr) 72px 28px 68px 64px 64px 20px",
    });
  });

  it("keeps Updated in the narrowest table, then hands it to the compact rows", () => {
    // `#1032858` needs 30px more, so the table gives way at 366px instead of 336px.
    expect(shape(pullRequestTableLayout(366, { statsColumns: true, numberLength: 8 }))).toBe(
      "number title status updated actions (dot)",
    );
    expect(shape(pullRequestTableLayout(365, { statsColumns: true, numberLength: 8 }))).toBe(
      "rows",
    );
    expect(shape(pullRequestTableLayout(320, { statsColumns: true, numberLength: 8 }))).toBe(
      "rows",
    );
  });
});
