import { describe, expect, it } from "vitest";
import { isValidSandboxBranch } from "./sandboxRepositories";

describe("isValidSandboxBranch", () => {
  it.each(["", "  ", "main", " feature/orders ", "release-1.2", "fix/café"])(
    "accepts a default or branch selection: %j",
    (branch) => expect(isValidSandboxBranch(branch)).toBe(true),
  );

  it.each([
    "my branch",
    "my\tbranch",
    "my\nbranch",
    "my\u00a0branch",
    "my\u0000branch",
    "feat#1",
    "a".repeat(40),
    "-main",
    "/main",
    "main/",
    "main.",
    "feature..next",
    "feature@{old}",
    "feature~1",
    "feature^1",
    "feature:next",
    "feature?next",
    "feature*next",
    "feature[next",
    "feature\\next",
  ])("rejects branches the managed workspace parser refuses: %j", (branch) => {
    expect(isValidSandboxBranch(branch)).toBe(false);
  });
});
