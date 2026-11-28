import { describe, expect, it } from "vitest";

import { pullRequestParticipants } from "./markdownCompletionContext";

describe("pullRequestParticipants", () => {
  it("lists the author, assignees and reviewers, naming teams with their organization", () => {
    expect(
      pullRequestParticipants(
        "acme/app",
        "olivia",
        ["sam"],
        [
          { login: "octo", kind: "user" },
          { login: "web", kind: "team" },
          { login: "other/ops", kind: "team" },
        ],
      ),
    ).toEqual([
      { login: "olivia", kind: "user", name: null },
      { login: "sam", kind: "user", name: null },
      { login: "octo", kind: "user", name: null },
      { login: "acme/web", kind: "team", name: null },
      { login: "other/ops", kind: "team", name: null },
    ]);
  });

  it("adds the people who commented, each person once and without apps' accounts", () => {
    expect(
      pullRequestParticipants(
        "acme/app",
        "olivia",
        ["sam"],
        [{ login: "Octo", kind: "user" }],
        ["octo", null, "dependabot[bot]", "riley", "Olivia", "riley"],
      ).map((person) => person.login),
    ).toEqual(["olivia", "sam", "Octo", "riley"]);
  });

  it("skips a missing author, and a team it can't name without the repository", () => {
    expect(pullRequestParticipants(null, null, [], [{ login: "web", kind: "team" }])).toEqual([]);
  });
});
