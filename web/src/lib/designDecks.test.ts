import { describe, expect, it } from "vitest";
import type { Conversation, ProjectSummary } from "@/hooks/useConversations";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import {
  DESIGN_SESSION_CAP,
  buildDesignGroups,
  deckName,
  filterDesignGroups,
  isDeckPath,
  isDesignListEmpty,
  kitIndicator,
  selectDesignWorkspaces,
  type DeckSearchState,
} from "./designDecks";

function row(id: string, updatedAt: number, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: `Title ${id}`,
    created_at: 1,
    updated_at: updatedAt,
    labels: {},
    permission_level: null,
    workspace: `/work/${id}`,
    ...overrides,
  };
}

const NO_PROJECTS: ProjectSummary[] = [];

function kitFile(
  content: string,
  overrides: { encoding?: "utf-8" | "base64"; bytes?: number } = {},
) {
  return { encoding: "utf-8" as const, content, bytes: content.length, ...overrides };
}

describe("selectDesignWorkspaces", () => {
  it("keeps one entry per workspace, read through its most recent session", () => {
    const workspaces = selectDesignWorkspaces(
      [
        row("old", 1, { workspace: "/work/app" }),
        row("new", 3, { workspace: "/work/app" }),
        row("other", 2, { workspace: "/work/site" }),
      ],
      NO_PROJECTS,
      null,
    );

    expect(workspaces.map((w) => [w.path, w.session.id])).toEqual([
      ["/work/app", "new"],
      ["/work/site", "other"],
    ]);
  });

  it("treats a trailing slash as the same workspace", () => {
    const workspaces = selectDesignWorkspaces(
      [row("a", 2, { workspace: "/work/app/" }), row("b", 1, { workspace: "/work/app" })],
      NO_PROJECTS,
      null,
    );

    expect(workspaces).toHaveLength(1);
    expect(workspaces[0].session.id).toBe("a");
  });

  it("skips archived, child, and workspace-less sessions", () => {
    const workspaces = selectDesignWorkspaces(
      [
        row("archived", 5, { archived: true }),
        row("child", 4, { parent_session_id: "parent" }),
        row("nowhere", 3, { workspace: null }),
        row("kept", 2),
      ],
      NO_PROJECTS,
      null,
    );

    expect(workspaces.map((w) => w.session.id)).toEqual(["kept"]);
  });

  it("caps the scan at the most recent sessions before deduping", () => {
    const sessions = Array.from({ length: DESIGN_SESSION_CAP + 5 }, (_, i) =>
      row(`s${i}`, 1_000 - i),
    );
    // An older session in a workspace no recent session uses stays out.
    sessions.push(row("ancient", 0, { workspace: "/work/ancient" }));

    const workspaces = selectDesignWorkspaces(sessions, NO_PROJECTS, null);

    expect(DESIGN_SESSION_CAP).toBe(50);
    expect(workspaces).toHaveLength(50);
    expect(workspaces.at(-1)?.session.id).toBe("s49");
    expect(workspaces.some((w) => w.path === "/work/ancient")).toBe(false);
  });

  it("labels a workspace with its session's project, else the folder name", () => {
    const projects: ProjectSummary[] = [
      { id: "p1", name: "Launch" },
      { id: null, name: "Legacy" },
    ];
    const workspaces = selectDesignWorkspaces(
      [
        row("filed", 4, { project_id: "p1", workspace: "/work/launch" }),
        row("labelled", 3, { labels: { [PROJECT_LABEL_KEY]: "Legacy" }, workspace: "/w/old" }),
        row("loose", 2, { workspace: "/Users/me/code/site/" }),
        row("shared", 1, { project_id: "p1", owner: "someone-else", workspace: "/w/shared" }),
      ],
      projects,
      "me",
    );

    expect(workspaces.map((w) => w.label)).toEqual(["Launch", "Legacy", "site", "shared"]);
  });
});

describe("deck paths", () => {
  it("accepts slide decks and drops nested worktrees and dependencies", () => {
    expect(isDeckPath("decks/q3.slides.html")).toBe(true);
    expect(isDeckPath("q3.slides.html")).toBe(true);
    expect(isDeckPath("q3.html")).toBe(false);
    expect(isDeckPath("slides.html")).toBe(false);
    expect(isDeckPath("decks/.slides.html")).toBe(false);
    expect(isDeckPath(".worktrees/feature/q3.slides.html")).toBe(false);
    expect(isDeckPath("web/node_modules/pkg/demo.slides.html")).toBe(false);
    expect(isDeckPath("my.worktrees/q3.slides.html")).toBe(true);
  });

  it("names a deck by its file name without the suffix", () => {
    expect(deckName("decks/Q3 review.slides.html")).toBe("Q3 review");
    expect(deckName("pitch.slides.html")).toBe("pitch");
  });
});

describe("kitIndicator", () => {
  it("is none without a kit.json", () => {
    expect(kitIndicator(null)).toEqual({ status: "none" });
  });

  it("names a kit that parses", () => {
    expect(kitIndicator(kitFile('{"name":"Acme"}'))).toEqual({ status: "ok", name: "Acme" });
  });

  it.each([
    ["broken JSON", kitFile("{"), /not valid JSON/],
    ["a missing name", kitFile("{}"), /needs a "name"/],
    ["a binary file", kitFile("AAAA", { encoding: "base64" }), /not a text file/],
    ["an oversize file", kitFile('{"name":"Big"}', { bytes: 3 * 1024 * 1024 }), /larger than/],
  ])("is invalid for %s", (_label, file, reason) => {
    const kit = kitIndicator(file);
    expect(kit.status).toBe("invalid");
    expect(kit.status === "invalid" && kit.reason).toMatch(reason);
  });
});

describe("buildDesignGroups", () => {
  const workspaces = selectDesignWorkspaces(
    [row("a", 4, { title: "Pitch work" }), row("b", 3), row("c", 2), row("d", 1), row("e", 0)],
    NO_PROJECTS,
    null,
  );

  it("lists sorted, filtered decks per workspace with their session title", () => {
    const searches: DeckSearchState[] = [
      {
        status: "ok",
        paths: ["z.slides.html", ".worktrees/x/z.slides.html", "a/b.slides.html", "notes.md"],
        truncated: false,
      },
    ];

    const [group] = buildDesignGroups(workspaces.slice(0, 1), searches, [
      { status: "ok", name: "Acme" },
    ]);

    expect(group.status).toBe("ready");
    expect(group.truncated).toBe(false);
    expect(group.kit).toEqual({ status: "ok", name: "Acme" });
    expect(group.decks).toEqual([
      { sessionId: "a", path: "a/b.slides.html", name: "b", sessionTitle: "Pitch work" },
      { sessionId: "a", path: "z.slides.html", name: "z", sessionTitle: "Pitch work" },
    ]);
  });

  it("keeps a truncated search with no decks so the cap is visible", () => {
    const [group] = buildDesignGroups(
      workspaces.slice(0, 1),
      [{ status: "ok", paths: ["notes.md"], truncated: true }],
      [],
    );

    expect(group.status).toBe("ready");
    expect(group.decks).toEqual([]);
    expect(group.truncated).toBe(true);
  });

  it("keeps loading, unavailable, and error groups and drops empty finished ones", () => {
    const groups = buildDesignGroups(
      workspaces,
      [
        { status: "ok", paths: ["notes.md"], truncated: false },
        { status: "loading" },
        { status: "unavailable" },
        { status: "error", message: "500 Internal Server Error" },
      ],
      [],
    );

    expect(groups.map((g) => [g.workspace.session.id, g.status])).toEqual([
      ["b", "loading"],
      ["c", "unavailable"],
      ["d", "error"],
      ["e", "loading"],
    ]);
    expect(groups.find((g) => g.status === "error")?.error).toBe("500 Internal Server Error");
  });

  it("marks the kit as loading until its read settles", () => {
    const [group] = buildDesignGroups(
      workspaces.slice(0, 1),
      [{ status: "ok", paths: ["d.slides.html"], truncated: false }],
      [],
    );
    expect(group.kit).toEqual({ status: "loading" });
  });
});

describe("isDesignListEmpty", () => {
  it("is empty only once the session list settled and no group remains", () => {
    expect(isDesignListEmpty([], true)).toBe(true);
    expect(isDesignListEmpty([], false)).toBe(false);
    const [loading] = buildDesignGroups(
      selectDesignWorkspaces([row("a", 1)], NO_PROJECTS, null),
      [{ status: "loading" }],
      [],
    );
    expect(isDesignListEmpty([loading], true)).toBe(false);
  });
});

describe("filterDesignGroups", () => {
  const groups = buildDesignGroups(
    selectDesignWorkspaces(
      [row("a", 3, { title: "Quarterly review" }), row("b", 2), row("offline", 1)],
      NO_PROJECTS,
      null,
    ),
    [
      { status: "ok", paths: ["decks/pitch.slides.html", "decks/roadmap.slides.html"] },
      { status: "ok", paths: ["decks/launch.slides.html"] },
      { status: "loading" },
    ],
    [],
  );

  it("returns every group for an empty query", () => {
    expect(filterDesignGroups(groups, "  ")).toBe(groups);
  });

  it("matches deck names case-insensitively and drops other groups", () => {
    const result = filterDesignGroups(groups, "PITCH");
    expect(result.map((g) => g.workspace.label)).toEqual(["a"]);
    expect(result[0].decks.map((d) => d.name)).toEqual(["pitch"]);
  });

  it("keeps all decks of a group whose workspace label or session title matches", () => {
    expect(filterDesignGroups(groups, "b")[0].decks.map((d) => d.name)).toEqual(["launch"]);
    expect(filterDesignGroups(groups, "quarterly")[0].decks).toHaveLength(2);
  });

  it("drops groups that are not ready while searching", () => {
    expect(filterDesignGroups(groups, "offline")).toEqual([]);
  });
});
