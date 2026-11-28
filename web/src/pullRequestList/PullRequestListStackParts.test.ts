import { describe, expect, it } from "vitest";

import { stackHintCopy } from "./PullRequestListStackParts";

describe("stackHintCopy for GitHub's own stacks", () => {
  it("says where a row sits when the PR below it couldn't be read, leaving out what's unknown", () => {
    expect(stackHintCopy({ kind: "native-gap", position: 2, size: 5 })).toEqual({
      chip: "Part of a stack",
      detail: "Stacked on another pull request in this stack (2 of 5).",
    });
    expect(stackHintCopy({ kind: "native-gap", position: 2, size: null }).detail).toBe(
      "Stacked on another pull request in this stack (position 2).",
    );
    expect(stackHintCopy({ kind: "native-gap", position: null, size: 5 }).detail).toBe(
      "Part of a GitHub stack whose other pull requests couldn’t be read here.",
    );
  });

  it("names the stack info that disagrees, never blaming descriptions that list no stack", () => {
    const parent = {
      url: "https://github.com/acme/app/pull/100",
      host: "github.com",
      repository: "acme/app",
      number: 100,
    };
    expect(stackHintCopy({ kind: "conflict", parent, source: "github" }).detail).toBe(
      "GitHub stacks this PR on #100, but GitHub’s read of #100 doesn’t list it directly above.",
    );
    expect(stackHintCopy({ kind: "conflict", parent, source: "mixed" }).detail).toBe(
      "This PR’s description stacks it on #100, but GitHub’s stack doesn’t list it directly above #100.",
    );
    expect(stackHintCopy({ kind: "unconfirmed", parent, source: "github" }).detail).toBe(
      "GitHub stacks this PR on #100, but #100’s own stack couldn’t be read here.",
    );
    expect(stackHintCopy({ kind: "conflict", parent, source: "descriptions" }).detail).toBe(
      "This PR’s description stacks it on #100, but #100’s description doesn’t list it directly below.",
    );
  });
});
