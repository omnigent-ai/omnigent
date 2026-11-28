import { describe, expect, it } from "vitest";

import { sectionCommands, type SectionCommandFacts } from "./sectionCommands";

const ids = (facts: Partial<SectionCommandFacts>) =>
  sectionCommands({ available: true, shown: "summary", commitOpen: false, ...facts }).map(
    (command) => command.id,
  );

describe("sectionCommands", () => {
  it("offers every section but the one shown", () => {
    expect(ids({ shown: "summary" })).toEqual(["show-comments", "show-changes"]);
    expect(ids({ shown: "comments" })).toEqual(["show-overview", "show-changes"]);
    expect(ids({ shown: "changes" })).toEqual(["show-overview", "show-comments"]);
  });

  it("leaves Commits to its own commands, and Changes to an open commit's", () => {
    expect(ids({ shown: "commits" })).toEqual(["show-overview", "show-comments", "show-changes"]);
    expect(ids({ shown: "commits", commitOpen: true })).toEqual(["show-overview", "show-comments"]);
  });

  it("offers nothing while the details aren't shown", () => {
    expect(ids({ available: false })).toEqual([]);
  });

  it("shows each command's own section", () => {
    const targets = sectionCommands({ available: true, shown: "commits", commitOpen: false }).map(
      (command) => [command.paletteLabel, command.target],
    );
    expect(targets).toEqual([
      ["Show the pull request’s overview", "summary"],
      ["Show the pull request’s comments", "comments"],
      ["Show the pull request’s changes", "changes"],
    ]);
  });
});
