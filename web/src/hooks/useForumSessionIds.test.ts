import { describe, it, expect } from "vitest";
import { isForumSession } from "./useForumSessionIds";

describe("Forum session visibility", () => {
  it("hides labeled parents and participants", () => {
    expect(isForumSession({ id: "parent", labels: { "omnigent.forum": "true" } }, new Set())).toBe(
      true,
    );
    expect(
      isForumSession({ id: "child", labels: { "omnigent.forum.parent": "parent" } }, new Set()),
    ).toBe(true);
  });
  it("hides legacy forum sessions and preserves ordinary sessions", () => {
    const ids = new Set(["legacy-parent", "legacy-child"]);
    expect(isForumSession({ id: "legacy-parent" }, ids)).toBe(true);
    expect(isForumSession({ id: "legacy-child" }, ids)).toBe(true);
    expect(isForumSession({ id: "ordinary" }, ids)).toBe(false);
  });
});
