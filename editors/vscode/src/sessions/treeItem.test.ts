/**
 * Tests for the pure Sessions view-model helpers (no VS Code host).
 */
import { describe, it, expect } from "vitest";
import {
  deriveLabel,
  relativeTime,
  sortSessions,
  statusThemeIconId,
  toItemView,
} from "./treeItem";
import type { Session } from "../api/client";

const NOW = 1_000_000 * 1000; // a fixed wall-clock in ms

describe("deriveLabel", () => {
  it("uses the trimmed title when present", () => {
    expect(deriveLabel({ id: "conv_x", title: "  Hello  " })).toBe("Hello");
  });

  it("falls back to a short id tail after the underscore", () => {
    expect(deriveLabel({ id: "conv_abcdefghij" })).toBe("Session abcdefgh");
  });

  it("handles an id with no underscore", () => {
    expect(deriveLabel({ id: "abcdefghij" })).toBe("Session abcdefgh");
  });

  it("returns a bare label for an empty id", () => {
    expect(deriveLabel({ id: "" })).toBe("Session");
  });
});

describe("relativeTime", () => {
  const now = 1000 * 1000; // nowMs
  it("renders sub-minute as 'just now'", () => {
    expect(relativeTime(1000 - 30, now)).toBe("just now");
  });
  it("renders minutes", () => {
    expect(relativeTime(1000 - 5 * 60, now)).toBe("5m ago");
  });
  it("renders hours", () => {
    expect(relativeTime(1000 - 3 * 3600, now)).toBe("3h ago");
  });
  it("renders days", () => {
    expect(relativeTime(1000 - 2 * 86400, now)).toBe("2d ago");
  });
  it("clamps a future timestamp to 'just now'", () => {
    expect(relativeTime(1000 + 100, now)).toBe("just now");
  });
});

describe("statusThemeIconId", () => {
  it("archived wins over status", () => {
    expect(statusThemeIconId("running", true)).toBe("archive");
  });
  it("maps running", () => {
    expect(statusThemeIconId("running")).toBe("play-circle");
  });
  it("maps idle", () => {
    expect(statusThemeIconId("idle")).toBe("circle-outline");
  });
  it("maps error/failure substrings", () => {
    expect(statusThemeIconId("error")).toBe("error");
    expect(statusThemeIconId("failed")).toBe("error");
  });
  it("defaults unknown/absent to circle-outline", () => {
    expect(statusThemeIconId(undefined)).toBe("circle-outline");
    expect(statusThemeIconId("weird")).toBe("circle-outline");
  });
});

describe("toItemView", () => {
  it("composes description from agent + relative time", () => {
    const v = toItemView({ id: "conv_1", agent_name: "claude", updated_at: 1_000_000 - 60 }, NOW);
    expect(v.description).toBe("claude · 1m ago");
    expect(v.contextValue).toBe("omnigentSession");
  });

  it("builds a tooltip with the present fields", () => {
    const s: Session = {
      id: "conv_1",
      title: "T",
      workspace: "/w",
      git_branch: "main",
      status: "running",
      created_at: 1_000_000 - 3600,
      updated_at: 1_000_000 - 60,
    };
    const v = toItemView(s, NOW);
    expect(v.tooltip).toContain("Workspace: /w");
    expect(v.tooltip).toContain("Branch: main");
    expect(v.tooltip).toContain("Status: running");
    expect(v.themeIconId).toBe("play-circle");
  });

  it("falls back to just the label when no detail fields exist", () => {
    const v = toItemView({ id: "conv_xyz123ab" }, NOW);
    expect(v.tooltip).toBe("Session xyz123ab");
    expect(v.description).toBe("");
  });
});

describe("sortSessions", () => {
  it("orders by updated_at desc, id tiebreak, without mutating input", () => {
    const input: Session[] = [
      { id: "b", updated_at: 100 },
      { id: "a", updated_at: 100 },
      { id: "c", updated_at: 200 },
    ];
    const out = sortSessions(input);
    expect(out.map((s) => s.id)).toEqual(["c", "a", "b"]);
    expect(input.map((s) => s.id)).toEqual(["b", "a", "c"]); // original untouched
  });

  it("treats a missing updated_at as 0", () => {
    const out = sortSessions([{ id: "a" }, { id: "b", updated_at: 5 }]);
    expect(out.map((s) => s.id)).toEqual(["b", "a"]);
  });
});
