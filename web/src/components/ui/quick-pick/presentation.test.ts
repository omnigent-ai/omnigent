import { describe, expect, it } from "vitest";

import type { QuickPickItem, QuickPickRow } from "./model";
import {
  buildQuickPickRows,
  navigateQuickPick,
  RECENT_HEADING_LABEL,
  reconcileActiveIndex,
} from "./presentation";

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

  it("ranks within groups and keeps group order", () => {
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

  it("does not borrow a provider's highlights for a client-ranked row", () => {
    const provided: QuickPickItem<string> = {
      id: "open",
      label: "Open settings",
      description: "Preferences",
      highlights: { description: [{ start: 0, end: 4 }] },
      value: "open",
    };
    const [row] = buildQuickPickRows({
      segments: [{ sourceId: "commands", ranking: "client", items: [provided] }],
      query: "open",
    });
    expect(row).toMatchObject({ kind: "item", descriptionRanges: [] });
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

describe("buildQuickPickRows with recents", () => {
  it("lists recents first under a Recent heading when the query is empty", () => {
    const rows = buildQuickPickRows({
      segments: [{ sourceId: "commands", ranking: "client", items: commands }],
      query: "",
      recentIds: ["open-settings", "missing", "new-session"],
    });
    expect(describeRows(rows)).toEqual([
      `# ${RECENT_HEADING_LABEL}`,
      "open-settings",
      "new-session",
      "# Sessions",
      "archive-session",
      "# View",
      "toggle-sidebar",
    ]);
  });

  it("moves from the Recent group to the next group with the group keys", () => {
    const rows = buildQuickPickRows({
      segments: [{ sourceId: "commands", ranking: "client", items: commands }],
      query: "",
      recentIds: ["open-settings", "new-session"],
    });
    const next = navigateQuickPick(rows, 1, { type: "nextGroup" });
    expect(rows[next]?.kind === "item" && rows[next].item.id).toBe("archive-session");
  });

  it("does not list items excluded from history as recents", () => {
    const rows = buildQuickPickRows({
      segments: [
        {
          sourceId: "commands",
          ranking: "client",
          items: [item("a", { rememberInHistory: false }), item("b")],
        },
      ],
      query: "",
      recentIds: ["a"],
    });
    expect(describeRows(rows)).toEqual(["a", "b"]);
  });

  it("never lists a provider-ranked row as recent", () => {
    const rows = buildQuickPickRows({
      segments: [{ sourceId: "search", ranking: "provider", items: [item("a"), item("b")] }],
      query: "",
      recentIds: ["b"],
    });
    expect(describeRows(rows)).toEqual(["a", "b"]);
  });

  it("uses recency only to break ties between equal matches", () => {
    const items = [item("alpha one"), item("alpha two"), item("beta alpha")];
    const rows = buildQuickPickRows({
      segments: [{ sourceId: "s", ranking: "client", items }],
      query: "alpha",
      recentIds: ["beta alpha", "alpha two"],
    });
    expect(describeRows(rows)).toEqual(["alpha two", "alpha one", "beta alpha"]);
  });
});

describe("buildQuickPickRows ranked across groups", () => {
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
  ];
  const rows = (groupOrder?: "source" | "match") =>
    buildQuickPickRows({
      segments: [
        { sourceId: "commands", ranking: "client", groupOrder, fuzzyMatching: "words", items },
      ],
      query: "theme",
    });

  it("keeps each group together by default", () => {
    // "Show the comments column" is too scattered to match.
    expect(describeRows(rows())).toEqual([
      "select-theme",
      "appearance",
      "# Settings",
      "terminal-theme",
      "settings-appearance",
    ]);
  });

  it("can rank rows across groups, each naming its group, so a group never buries a better match", () => {
    expect(
      rows("match").map((row) =>
        row.kind === "item" ? `${row.sectionLabel ?? ""}/${row.item.id}` : `# ${row.label}`,
      ),
    ).toEqual([
      "/select-theme",
      "Settings/terminal-theme",
      "/appearance",
      "Settings/settings-appearance",
    ]);
  });

  it("keeps the first row for an ID that an earlier segment listed", () => {
    const ranked = buildQuickPickRows({
      segments: [
        { sourceId: "first", ranking: "client", items: [item("select-theme")] },
        { sourceId: "commands", ranking: "client", groupOrder: "match", items },
      ],
      query: "theme",
    });
    expect(describeRows(ranked).filter((id) => id === "select-theme")).toHaveLength(1);
  });
});

describe("buildQuickPickRows with a free-text item", () => {
  it("puts it first and drops a duplicate suggestion with its ID", () => {
    const rows = buildQuickPickRows({
      segments: [
        {
          sourceId: "prs",
          ranking: "client",
          items: [item("pr:12", { label: "#12 Fix", group: "Suggested" })],
        },
      ],
      query: "12",
      freeTextItem: item("pr:12", { label: "Attach #12" }),
    });
    expect(describeRows(rows)).toEqual(["pr:12"]);
    expect(rows[0]?.kind === "item" && rows[0].isFreeText).toBe(true);
  });
});

describe("buildQuickPickRows with sources first", () => {
  const commandSegment = {
    sourceId: "commands",
    ranking: "client" as const,
    fuzzyMatching: "words" as const,
    items: [item("Zoom In"), item("Go to Settings"), item("Toggle sidebar")],
  };
  // A provider-ranked source, such as session search, whose rows arrive already matched.
  const sessions = (ids: string[]) => ({
    sourceId: "sessions",
    ranking: "provider" as const,
    origin: "source" as const,
    items: ids.map((id) => item(id)),
  });
  const rows = (
    query: string,
    sessionIds: string[],
    sourcesFirst = true,
    recentIds: string[] = [],
  ) =>
    describeRows(
      buildQuickPickRows({
        segments: [commandSegment, sessions(sessionIds)],
        query,
        recentIds,
        sourcesFirst,
      }),
    );

  it("lists the sources' rows above the items before anything is typed", () => {
    expect(rows("", ["Recent session"])).toEqual([
      "Recent session",
      "Zoom In",
      "Go to Settings",
      "Toggle sidebar",
    ]);
  });

  it("keeps them above recently picked items too, which still lead the other items", () => {
    expect(rows("", ["Recent session"], true, ["Toggle sidebar"])).toEqual([
      "Recent session",
      `# ${RECENT_HEADING_LABEL}`,
      "Toggle sidebar",
      "Zoom In",
      "Go to Settings",
    ]);
  });

  it("keeps them above the items when no item's label starts with the whole query", () => {
    expect(rows("parser", ["Parser rewrite"])).toEqual(["Parser rewrite"]);
    // "ebar" is inside "sidebar", not at a word's start, so it doesn't name the command.
    expect(rows("ebar", ["Rebar plan"]).slice(0, 1)).toEqual(["Rebar plan"]);
  });

  it("puts the items first when one's label matches the whole query from a word's start", () => {
    // A session that matches "zoom in" loosely mustn't take Enter from the command it names.
    expect(rows("Zoom In", ["Room in the zoo"])).toEqual(["Zoom In", "Room in the zoo"]);
    expect(rows("settings", ["Settings migration"])[0]).toBe("Go to Settings");
  });

  it("keeps the sources first when only an item that doesn't lead them matches", () => {
    const pullRequestAction = item("Remove #12 Fix the rich prompt from this session", {
      leadsSources: false,
    });
    const segments = [
      { ...commandSegment, items: [...commandSegment.items, pullRequestAction] },
      sessions(["Rich prompt editing"]),
    ];
    expect(
      describeRows(buildQuickPickRows({ segments, query: "rich", sourcesFirst: true })),
    ).toEqual(["Rich prompt editing", "Remove #12 Fix the rich prompt from this session"]);
  });

  it("leaves the given order alone without sourcesFirst", () => {
    expect(rows("", ["Recent session"], false)[0]).toBe("Zoom In");
  });
});

describe("navigateQuickPick", () => {
  const rows = buildQuickPickRows({
    segments: [{ sourceId: "commands", ranking: "client", items: commands }],
    query: "",
  });
  // ["# Sessions", new, archive, "# View", toggle, settings]

  it("skips headings and stops at both ends unless looping", () => {
    expect(navigateQuickPick(rows, 1, { type: "next" })).toBe(2);
    expect(navigateQuickPick(rows, 2, { type: "next" })).toBe(4);
    expect(navigateQuickPick(rows, 5, { type: "next" })).toBe(5);
    expect(navigateQuickPick(rows, 1, { type: "previous" })).toBe(1);
    expect(navigateQuickPick(rows, 5, { type: "next" }, { loop: true })).toBe(1);
    expect(navigateQuickPick(rows, 1, { type: "previous" }, { loop: true })).toBe(5);
  });

  it("moves from a row that can't be picked to its neighbours, not the top", () => {
    const loading = buildQuickPickRows({
      segments: [
        {
          sourceId: "s",
          ranking: "provider",
          items: [item("r0"), item("r1"), item("more", { disabledReason: "Loading" }), item("r3")],
        },
      ],
      query: "",
    });
    // [r0, r1, more (disabled), r3]
    expect(navigateQuickPick(loading, 2, { type: "next" })).toBe(3);
    expect(navigateQuickPick(loading, 2, { type: "previous" })).toBe(1);
    expect(navigateQuickPick(loading, 2, { type: "pageDown", pageSize: 1 })).toBe(3);
    expect(navigateQuickPick(loading, 2, { type: "pageUp", pageSize: 1 })).toBe(1);
    // With nothing active yet, ArrowDown still starts at the top.
    expect(navigateQuickPick(loading, -1, { type: "next" })).toBe(0);
  });

  it("jumps to the first row of the next or previous group", () => {
    expect(navigateQuickPick(rows, 1, { type: "nextGroup" })).toBe(4);
    expect(navigateQuickPick(rows, 2, { type: "nextGroup" })).toBe(4);
    expect(navigateQuickPick(rows, 5, { type: "previousGroup" })).toBe(1);
    // With no group beyond, it moves one row, and stays at the ends.
    expect(navigateQuickPick(rows, 4, { type: "nextGroup" })).toBe(5);
    expect(navigateQuickPick(rows, 5, { type: "nextGroup" })).toBe(5);
    expect(navigateQuickPick(rows, 2, { type: "previousGroup" })).toBe(1);
  });

  it("treats an ungrouped row after a group as outside it", () => {
    const mixed = buildQuickPickRows({
      segments: [
        { sourceId: "early", ranking: "provider", items: [item("p1", { group: "Earlier" })] },
        {
          sourceId: "sessions",
          ranking: "provider",
          items: [item("a1", { group: "Sessions" }), item("a2", { group: "Sessions" })],
        },
        { sourceId: "loose", ranking: "provider", items: [item("u")] },
        { sourceId: "actions", ranking: "provider", items: [item("z1", { group: "Actions" })] },
      ],
      query: "",
    });
    // ["# Earlier", p1, "# Sessions", a1, a2, u, "# Actions", z1]
    const at = (id: string) => mixed.findIndex((row) => row.kind === "item" && row.item.id === id);
    expect(navigateQuickPick(mixed, at("u"), { type: "previousGroup" })).toBe(at("a2"));
    expect(navigateQuickPick(mixed, at("u"), { type: "nextGroup" })).toBe(at("z1"));
    expect(navigateQuickPick(mixed, at("a1"), { type: "nextGroup" })).toBe(at("z1"));
  });

  it("does not count an ungrouped row as the first row of a disabled group", () => {
    const rows = buildQuickPickRows({
      segments: [
        { sourceId: "sessions", ranking: "provider", items: [item("a1", { group: "Sessions" })] },
        {
          sourceId: "disabled",
          ranking: "provider",
          items: [item("d1", { group: "Disabled", disabledReason: "Not here" }), item("u")],
        },
        { sourceId: "actions", ranking: "provider", items: [item("z1", { group: "Actions" })] },
      ],
      query: "",
    });
    const at = (id: string) => rows.findIndex((row) => row.kind === "item" && row.item.id === id);
    expect(navigateQuickPick(rows, at("a1"), { type: "nextGroup" })).toBe(at("z1"));
  });

  it("moves one row by group keys when rows have no headings", () => {
    const flat = buildQuickPickRows({
      segments: [{ sourceId: "s", ranking: "client", items: [item("a"), item("b")] }],
      query: "",
    });
    expect(navigateQuickPick(flat, 0, { type: "nextGroup" })).toBe(1);
    expect(navigateQuickPick(flat, 1, { type: "previousGroup" })).toBe(0);
  });

  it("pages without wrapping and jumps to the ends", () => {
    expect(navigateQuickPick(rows, 1, { type: "pageDown", pageSize: 10 })).toBe(5);
    expect(navigateQuickPick(rows, 1, { type: "pageDown", pageSize: Number.NaN })).toBe(2);
    expect(navigateQuickPick(rows, 5, { type: "pageUp", pageSize: 0 })).toBe(4);
    expect(navigateQuickPick(rows, 4, { type: "pageUp", pageSize: 1 })).toBe(2);
    expect(navigateQuickPick(rows, 5, { type: "pageUp", pageSize: 10 })).toBe(1);
    expect(navigateQuickPick(rows, 4, { type: "first" })).toBe(1);
    expect(navigateQuickPick(rows, 1, { type: "last" })).toBe(5);
  });

  it("starts from the top when nothing is active and returns -1 for no items", () => {
    expect(navigateQuickPick(rows, -1, { type: "next" })).toBe(1);
    expect(navigateQuickPick(rows, -1, { type: "previous" })).toBe(-1);
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
    expect(navigateQuickPick(withDisabled, 4, { type: "next" })).toBe(4);
    expect(navigateQuickPick(withDisabled, 4, { type: "previous" })).toBe(1);
    expect(navigateQuickPick(withDisabled, 1, { type: "nextGroup" })).toBe(4);
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
