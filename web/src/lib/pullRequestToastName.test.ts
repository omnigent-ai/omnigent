import { describe, expect, it } from "vitest";

import { pullRequestToastName } from "./pullRequestToastName";

describe("pullRequestToastName", () => {
  it("names a PR by its number alone, never its title or repository", () => {
    expect(pullRequestToastName({ number: 13617 })).toBe("#13617");
    expect(pullRequestToastName("https://github.com/acme/app/pull/42")).toBe("#42");
    expect(pullRequestToastName("https://github.com/acme/app/pull/42/files")).toBe("#42");
  });

  it("reads generically for a link that names no pull request", () => {
    expect(pullRequestToastName("https://github.com/acme/app")).toBe("The pull request");
    expect(pullRequestToastName("not a link")).toBe("The pull request");
  });
});
