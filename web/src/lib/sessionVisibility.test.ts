import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { filterSessionScope, sessionVisibility } from "./sessionVisibility";

describe("frontend session scope", () => {
  it.each([
    [{ owner: "alice", permission_level: null }, "mine"],
    [{ owner: "bob", permission_level: null }, "shared"],
    [{ owner: "alice", permission_level: 1 }, "mine"],
    [{ owner: "bob", permission_level: 4 }, "shared"],
    [{ permission_level: 4 }, "mine"],
    [{ permission_level: 1 }, "shared"],
    [{ permission_level: 2 }, "shared"],
    [{ permission_level: 3 }, "shared"],
    [{ permission_level: null }, "mine"],
  ] as const)("classifies %j as %s", (row, expected) => {
    expect(sessionVisibility(row, "alice")).toBe(expected);
  });
  it("does not invent shared ownership without viewer identity or a grant", () => {
    expect(sessionVisibility({ owner: "alice", permission_level: null }, null)).toBe("mine");
    expect(sessionVisibility({ permission_level: 1 }, null)).toBe("shared");
    expect(sessionVisibility({ owner: "bob", permission_level: 4 }, null)).toBe("mine");
  });
  it("separates a mixed response and excludes archived rows", () => {
    const rows = [
      { id: "mine", owner: "alice", permission_level: null },
      { id: "shared", owner: "bob", permission_level: 4 },
      { id: "archived", owner: "alice", permission_level: 4, archived: true },
    ] as Conversation[];
    expect(filterSessionScope(rows, "mine", "alice").map((r) => r.id)).toEqual(["mine"]);
    expect(filterSessionScope(rows, "shared", "alice").map((r) => r.id)).toEqual(["shared"]);
  });
});
