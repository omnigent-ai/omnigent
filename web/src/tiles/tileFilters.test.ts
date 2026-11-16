import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  favoritesFirst,
  filterTiles,
  matchesQuery,
  projectOf,
  projectsOf,
  statusCounts,
} from "./tileFilters";

function row(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: `Session ${id}`,
    created_at: 1,
    updated_at: 1,
    labels: {},
    permission_level: null,
    status: "idle",
    ...overrides,
  };
}

const pinned = { labels: { "omnigent.pinned": "1700000000" } };

describe("projectOf", () => {
  it("uses the working directory's last segment", () => {
    expect(projectOf({ workspace: "/workspace/example-app/" })).toBe("example-app");
    expect(projectOf({ workspace: "  " })).toBeNull();
    expect(projectOf({ workspace: null })).toBeNull();
  });
});

describe("matchesQuery", () => {
  const session = row("a", {
    title: "Fix billing",
    harness: "codex",
    workspace: "/w/example-app",
    git_branch: "feature/invoices",
  });

  it("requires every word, case-insensitively, across title and context", () => {
    expect(matchesQuery(session, "")).toBe(true);
    expect(matchesQuery(session, "BILLING codex")).toBe(true);
    expect(matchesQuery(session, "invoices example-app")).toBe(true);
    expect(matchesQuery(session, "billing pi")).toBe(false);
  });
});

describe("filterTiles", () => {
  const rows = [
    row("fav", { ...pinned, workspace: "/w/one" }),
    row("waiting", { pending_elicitations_count: 2, workspace: "/w/two" }),
    row("running", { status: "running", workspace: "/w/one" }),
    row("idle", { workspace: "/w/two" }),
  ];
  const ids = (
    status: Parameters<typeof filterTiles>[1]["status"],
    project: string | null = null,
  ) => filterTiles(rows, { status, project, query: "" }).map((r) => r.id);

  it("filters by status", () => {
    expect(ids("all")).toEqual(["fav", "waiting", "running", "idle"]);
    expect(ids("favorites")).toEqual(["fav"]);
    expect(ids("needs-you")).toEqual(["waiting"]);
    expect(ids("working")).toEqual(["running"]);
  });

  it("combines status with project", () => {
    expect(ids("all", "two")).toEqual(["waiting", "idle"]);
    expect(ids("working", "two")).toEqual([]);
  });

  it("counts statuses and lists projects", () => {
    expect(statusCounts(rows)).toEqual({ all: 4, favorites: 1, "needs-you": 1, working: 1 });
    expect(projectsOf(rows)).toEqual(["one", "two"]);
  });
});

describe("favoritesFirst", () => {
  it("moves favorites ahead and keeps the rest in order", () => {
    const ordered = favoritesFirst([row("a"), row("b", pinned), row("c")]);
    expect(ordered.map((r) => r.id)).toEqual(["b", "a", "c"]);
  });
});
