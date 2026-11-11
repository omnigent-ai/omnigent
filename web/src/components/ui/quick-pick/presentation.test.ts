import { describe, expect, it } from "vitest";

import type { QuickPickItem, QuickPickRow } from "./model";
import { buildQuickPickRows, navigateQuickPick, reconcileActiveIndex } from "./presentation";

function item(id: string, overrides: Partial<QuickPickItem<string>> = {}): QuickPickItem<string> {
  return { id, label: id, value: id, ...overrides };
}

function describeRows(rows: readonly QuickPickRow<unknown>[]): string[] {
  return rows.map((row) => (row.kind === "heading" ? `# ${row.label}` : row.item.id));
}

const commands = [
  item("new-session", { label: "New session", group: "Sessions" }),
  item("archive-session", { label: "Archive session", group: "Sessions" }),
  item("toggle-sidebar", { label: "Toggle sidebar", group: "View" }),
  item("open-settings", { label: "Open settings", group: "View", keywords: ["preferences"] }),
];

describe("buildQuickPickRows", () => {
  it("lists every group in source order when the query is empty", () => {
    const rows = buildQuickPickRows({
      segments: [{ sourceId: "commands", ranking: "client", items: commands }],
      query: "  ",
    });
    expect(describeRows(rows)).toEqual([
      "# Sessions",
      "new-session",
      "archive-session",
      "# View",
      "toggle-sidebar",
      "open-settings",
    ]);
  });

  it("ranks within groups, keeps group order, and drops groups without matches", () => {
    const rows = buildQuickPickRows({
      segments: [{ sourceId: "commands", ranking: "client", items: commands }],
      query: "se",
    });
    expect(describeRows(rows)).toEqual([
      "# Sessions",
      "new-session",
      "archive-session",
      "# View",
      "open-settings",
      "toggle-sidebar",
    ]);
    const settings = rows.find((row) => row.kind === "item" && row.item.id === "open-settings");
    expect(settings?.kind === "item" && settings.labelRanges).toEqual([{ start: 5, end: 7 }]);
  });

  it("ranks within each group and breaks ties by source order", () => {
    const items = [
      item("appearance", { label: "Appearance", keywords: ["theme"] }),
      item("comments", { label: "Show the comments column", group: "File" }),
      item("terminal-theme", { label: "Terminal theme", group: "Settings" }),
      item("select-theme", { label: "Select theme…" }),
      item("settings-appearance", {
        label: "Settings: Appearance",
        group: "Settings",
        keywords: ["theme"],
      }),
      item("theme-two", { label: "Terminal theme", group: "Settings" }),
    ];
    const rows = buildQuickPickRows({
      segments: [{ sourceId: "commands", ranking: "client", fuzzyMatching: "words", items }],
      query: "theme",
    });
    // Each group stays together; "Show the comments column" is too scattered to match.
    expect(describeRows(rows)).toEqual([
      "select-theme",
      "appearance",
      "# Settings",
      "terminal-theme",
      "theme-two",
      "settings-appearance",
    ]);
  });

  it("keeps scattered fuzzy matches unless the segment asks for word matching", () => {
    const items = [item("comments", { label: "Show the comments column" })];
    const rows = (fuzzyMatching?: "fuzzy" | "words") =>
      describeRows(
        buildQuickPickRows({
          segments: [{ sourceId: "s", ranking: "client", fuzzyMatching, items }],
          query: "theme",
        }),
      );
    expect(rows()).toEqual(["comments"]);
    expect(rows("words")).toEqual([]);
  });

  it("matches hidden keywords without highlighting the label", () => {
    const rows = buildQuickPickRows({
      segments: [{ sourceId: "commands", ranking: "client", items: commands }],
      query: "prefer",
    });
    expect(describeRows(rows)).toEqual(["# View", "open-settings"]);
    expect(rows[1].kind === "item" && rows[1].labelRanges).toEqual([]);
  });

  it("keeps provider-ranked order and highlights untouched", () => {
    const highlighted = item("hit", { highlights: { label: [{ start: 0, end: 1 }] } });
    const rows = buildQuickPickRows({
      segments: [{ sourceId: "search", ranking: "provider", items: [item("zeta"), highlighted] }],
      query: "unrelated",
    });
    expect(describeRows(rows)).toEqual(["zeta", "hit"]);
    expect(rows[1].kind === "item" && rows[1].labelRanges).toEqual([{ start: 0, end: 1 }]);
  });

  it("keeps the first row for an ID that two segments both list", () => {
    const rows = buildQuickPickRows({
      segments: [
        { sourceId: "titles", ranking: "provider", items: [item("s1", { group: "Sessions" })] },
        {
          sourceId: "content",
          ranking: "provider",
          items: [item("s1", { group: "In messages" }), item("s2", { group: "In messages" })],
        },
      ],
      query: "kestrel",
    });
    expect(describeRows(rows)).toEqual(["# Sessions", "s1", "# In messages", "s2"]);
  });

  it("drops a heading whose only item was a duplicate", () => {
    const rows = buildQuickPickRows({
      segments: [
        { sourceId: "titles", ranking: "provider", items: [item("s1", { group: "Sessions" })] },
        { sourceId: "content", ranking: "provider", items: [item("s1", { group: "In messages" })] },
      ],
      query: "kestrel",
    });
    expect(describeRows(rows)).toEqual(["# Sessions", "s1"]);
  });

  it("keeps an emptied heading from labeling the next segment's ungrouped rows", () => {
    for (const query of ["", "s"]) {
      const rows = buildQuickPickRows({
        segments: [
          { sourceId: "titles", ranking: "provider", items: [item("s1", { group: "Sessions" })] },
          {
            sourceId: "content",
            ranking: "provider",
            items: [item("s1", { group: "In messages" })],
          },
          { sourceId: "commands", ranking: "client", items: [item("search", { label: "Search" })] },
        ],
        query,
      });
      expect(describeRows(rows)).toEqual(["# Sessions", "s1", "search"]);
    }
  });

  it("keeps an emptied group's heading off the same segment's ungrouped rows", () => {
    for (const query of ["", "s"]) {
      const rows = buildQuickPickRows({
        segments: [
          { sourceId: "titles", ranking: "provider", items: [item("s1", { group: "Sessions" })] },
          {
            sourceId: "content",
            ranking: "provider",
            items: [item("s1", { group: "In messages" }), item("search", { label: "Search" })],
          },
        ],
        query,
      });
      expect(describeRows(rows)).toEqual(["# Sessions", "s1", "search"]);
    }
  });

  it("keeps the visible first row when one ID sits in two groups of a segment", () => {
    const rows = buildQuickPickRows({
      segments: [
        {
          sourceId: "search",
          ranking: "client",
          items: [
            item("shared", { label: "Alpha beta", group: "First" }),
            item("other", { label: "Alpha", group: "Second" }),
            item("shared", { label: "Alpha", group: "Second" }),
          ],
        },
      ],
      query: "alpha",
    });
    expect(describeRows(rows)).toEqual(["# First", "shared", "# Second", "other"]);
  });

  it("keeps the first of two items with one ID inside a segment", () => {
    const rows = buildQuickPickRows({
      segments: [
        {
          sourceId: "search",
          ranking: "provider",
          items: [item("s1", { label: "First" }), item("s1", { label: "Second" })],
        },
      ],
      query: "",
    });
    expect(rows).toHaveLength(1);
    expect(rows[0].kind === "item" && rows[0].item.label).toBe("First");
  });

  it("concatenates segments in source order", () => {
    const rows = buildQuickPickRows({
      segments: [
        { sourceId: "one", ranking: "client", items: [item("b1", { group: "B" })] },
        { sourceId: "two", ranking: "client", items: [item("b2", { group: "B" })] },
      ],
      query: "b",
    });
    expect(describeRows(rows)).toEqual(["# B", "b1", "# B", "b2"]);
    expect(new Set(rows.map((row) => row.key)).size).toBe(rows.length);
  });
});

describe("navigateQuickPick", () => {
  const rows = buildQuickPickRows({
    segments: [{ sourceId: "commands", ranking: "client", items: commands }],
    query: "",
  });
  // ["# Sessions", new, archive, "# View", toggle, settings]

  it("skips headings and wraps at both ends", () => {
    expect(navigateQuickPick(rows, 1, { type: "next" })).toBe(2);
    expect(navigateQuickPick(rows, 2, { type: "next" })).toBe(4);
    expect(navigateQuickPick(rows, 5, { type: "next" })).toBe(1);
    expect(navigateQuickPick(rows, 1, { type: "previous" })).toBe(5);
  });

  it("pages without wrapping and jumps to the ends", () => {
    expect(navigateQuickPick(rows, 1, { type: "pageDown", pageSize: 10 })).toBe(5);
    expect(navigateQuickPick(rows, 4, { type: "pageUp", pageSize: 1 })).toBe(2);
    expect(navigateQuickPick(rows, 5, { type: "pageUp", pageSize: 10 })).toBe(1);
    expect(navigateQuickPick(rows, 4, { type: "first" })).toBe(1);
    expect(navigateQuickPick(rows, 1, { type: "last" })).toBe(5);
  });

  it("starts from the top when nothing is active and returns -1 for no items", () => {
    expect(navigateQuickPick(rows, -1, { type: "next" })).toBe(1);
    expect(navigateQuickPick([], -1, { type: "next" })).toBe(-1);
  });

  it("passes over disabled items in every direction", () => {
    const withDisabled = buildQuickPickRows({
      segments: [
        {
          sourceId: "commands",
          ranking: "client",
          items: commands.map((entry) =>
            entry.id === "archive-session" || entry.id === "open-settings"
              ? { ...entry, disabledReason: "Open a session first" }
              : entry,
          ),
        },
      ],
      query: "",
    });
    expect(navigateQuickPick(withDisabled, 1, { type: "next" })).toBe(4);
    expect(navigateQuickPick(withDisabled, 4, { type: "next" })).toBe(1);
    expect(navigateQuickPick(withDisabled, 1, { type: "previous" })).toBe(4);
    expect(navigateQuickPick(withDisabled, 1, { type: "last" })).toBe(4);
    expect(navigateQuickPick(withDisabled, 4, { type: "pageDown", pageSize: 10 })).toBe(4);
  });
});

describe("reconcileActiveIndex", () => {
  const rows = buildQuickPickRows({
    segments: [{ sourceId: "commands", ranking: "client", items: commands }],
    query: "",
  });

  it("keeps a surviving active item and otherwise falls back to the first item", () => {
    expect(reconcileActiveIndex(rows, "open-settings")).toBe(5);
    expect(reconcileActiveIndex(rows, "gone")).toBe(1);
    expect(reconcileActiveIndex(rows, null)).toBe(1);
    expect(reconcileActiveIndex([], null)).toBe(-1);
  });

  it("falls back to the first enabled item, or the first item when all are disabled", () => {
    const disabled = (ids: string[]) =>
      buildQuickPickRows({
        segments: [
          {
            sourceId: "commands",
            ranking: "client",
            items: commands.map((entry) =>
              ids.includes(entry.id) ? { ...entry, disabledReason: "Not here" } : entry,
            ),
          },
        ],
        query: "",
      });
    expect(reconcileActiveIndex(disabled(["new-session"]), null)).toBe(2);
    expect(reconcileActiveIndex(disabled(commands.map((entry) => entry.id)), null)).toBe(1);
  });
});
