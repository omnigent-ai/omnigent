// The design's history traces, run against the real tab store: each step acts
// through the store's own open, select, navigate, Back/Forward and close.

import { describe, expect, it } from "vitest";

import { describeHistory } from "./__fixtures__/historyWorkspace";
import type { Disposition } from "./planOpen";
import { currentVisit } from "./workspaceHistory";
import type { TabId, WorkspacePlace } from "./workspacePlace";
import { samePlace } from "./planOpen";
import { createWorkspaceTabsStore, type WorkspaceTabsStore } from "./workspaceTabsStore";

const PR_12 = "https://github.com/o/r/pull/12";

function file(path: string, line?: number): WorkspacePlace {
  return line === undefined
    ? { kind: "files", path }
    : { kind: "files", path, line: { start: line } };
}
function pull(view: "summary" | "changes" = "summary", path?: string): WorkspacePlace {
  return path === undefined
    ? { kind: "pull", url: PR_12, view }
    : { kind: "pull", url: PR_12, view, path };
}

interface TraceTab {
  id: TabId;
  place: WorkspacePlace;
  role?: "files";
}
function tab(id: string, place: WorkspacePlace, extra: { role?: "files" } = {}): TraceTab {
  return { id, place, ...extra };
}

/** The traces' workspace, as the tab store: tabs it creates take the ids the trace names. */
class StoreWorkspace {
  readonly store: WorkspaceTabsStore;
  /** The leave guard's answer for the next guarded navigation. */
  guardAnswer: "leave" | "cancel" = "leave";
  readonly guardCalls: TabId[][] = [];
  private readonly dirty = new Set<TabId>();
  private readonly nextIds: TabId[] = [];
  private created = 0;

  constructor(first: TraceTab) {
    this.store = createWorkspaceTabsStore({
      scopeId: "s1",
      initial: {
        tabs: [{ ...first, openedBy: "restore" }],
        selectedTabId: first.id,
        timeline: [{ tabId: first.id, place: first.place }],
        cursor: 0,
        closed: [],
      },
      save: () => "saved",
      requestLeave: async (tabIds) => {
        this.guardCalls.push([...tabIds]);
        return this.guardAnswer;
      },
      isTabDirty: (tabId) => this.dirty.has(tabId),
      newTabId: () => this.nextIds.shift() ?? `new-${this.created++}`,
      newViewKey: () => `view-${this.created++}`,
      newDraftKey: () => `pending:${this.created++}`,
    });
  }

  get history() {
    return this.store.history.state;
  }

  get selectedTabId() {
    return this.store.selectedTabId();
  }

  tab(id: TabId) {
    const found = this.store.findTab(id);
    if (!found) throw new Error(`no tab ${id}`);
    return { place: found.place, dot: found.unseen === true, attention: found.attention?.place };
  }

  /** The user, or a chat link, opens a place into a tab of its own. */
  openSelected({ id, place }: TraceTab, disposition: Disposition = "new") {
    this.nextIds.push(id);
    return this.store.open({ place }, { opener: "user", disposition });
  }

  /** An agent opens a place: into the tab the trace names when it's open, else a new one. */
  openInBackground({ id, place }: TraceTab) {
    if (this.store.findTab(id)) return this.store.open({ place, tab: id }, { opener: "agent" });
    this.nextIds.push(id);
    return this.store.open({ place }, { opener: "agent" });
  }

  /** An agent moves an unselected tab, whose file then has unsaved edits. */
  async editInBackground(id: TabId, place: WorkspacePlace) {
    await this.openInBackground({ id, place });
    this.dirty.add(id);
  }

  select(id: TabId) {
    return this.store.select(id);
  }

  navigate(id: TabId, place: WorkspacePlace) {
    return this.store.navigate(id, place);
  }

  /** A link to another line of the file a tab shows. */
  moveWithin(_tabShowingTheFile: TabId, place: WorkspacePlace) {
    return this.store.open({ place }, { opener: "link" });
  }

  pickApp(newTabId: TabId, app: TraceTab) {
    this.nextIds.push(app.id);
    return this.store.fillNewTab(newTabId, app.place);
  }

  back() {
    return this.store.go("back");
  }

  forward() {
    return this.store.go("forward");
  }

  close(id: TabId) {
    return this.store.close(id);
  }

  /** The design's invariant: the cursor visit belongs to the selected tab and equals its place. */
  invariantHolds(): boolean {
    const visit = currentVisit(this.history);
    const selected = this.store.selectedTab();
    return (
      visit !== null &&
      selected !== undefined &&
      visit.tabId === selected.id &&
      samePlace(visit.place, selected.place) &&
      visit.entry?.index === selected.entry?.index
    );
  }
}

interface TraceStep {
  name: string;
  run: (workspace: StoreWorkspace) => unknown;
  timeline: string;
  selected?: string;
}

interface Trace {
  name: string;
  setup: () => Promise<StoreWorkspace>;
  steps: TraceStep[];
}

// Traces 1-9 of the design's history section. The invariant is checked after every step.
const traces: Trace[] = [
  {
    name: "1. an agent's background open is not a step until the user selects it",
    setup: async () => new StoreWorkspace(tab("F", file("a.ts"))),
    steps: [
      {
        name: "agent opens PR #12",
        run: (ws) => ws.openInBackground(tab("P", pull())),
        timeline: "[F:a.ts*]",
        selected: "F",
      },
      { name: "user clicks P", run: (ws) => ws.select("P"), timeline: "[F:a.ts, P:#12*]" },
      { name: "Back", run: (ws) => ws.back(), timeline: "[F:a.ts*, P:#12]", selected: "F" },
    ],
  },
  {
    name: "2. in-tab steps are steps, and Back walks them",
    setup: async () => new StoreWorkspace(tab("F", file("a"))),
    steps: [
      {
        name: "a chat link opens #12",
        run: (ws) => ws.openSelected(tab("P", pull())),
        timeline: "[F:a, P:#12*]",
      },
      {
        name: "Changes",
        run: (ws) => ws.navigate("P", pull("changes")),
        timeline: "[F:a, P:#12, P:#12/changes*]",
      },
      {
        name: "main.go",
        run: (ws) => ws.navigate("P", pull("changes", "main.go")),
        timeline: "[F:a, P:#12, P:#12/changes, P:#12/changes:main.go*]",
      },
      {
        name: "Back",
        run: (ws) => ws.back(),
        timeline: "[F:a, P:#12, P:#12/changes*, P:#12/changes:main.go]",
      },
      {
        name: "Back",
        run: (ws) => ws.back(),
        timeline: "[F:a, P:#12*, P:#12/changes, P:#12/changes:main.go]",
      },
      {
        name: "Back",
        run: (ws) => ws.back(),
        timeline: "[F:a*, P:#12, P:#12/changes, P:#12/changes:main.go]",
        selected: "F",
      },
    ],
  },
  {
    name: "3. closing the selected tab lands on the nearest earlier visit",
    setup: async () => {
      const ws = new StoreWorkspace(tab("F", file("a")));
      await ws.openSelected(tab("C", { kind: "changes", path: "x.go" }));
      await ws.openSelected(tab("F2", file("b.ts")));
      return ws;
    },
    steps: [
      { name: "close F2", run: (ws) => ws.close("F2"), timeline: "[F:a, C:x.go*]", selected: "C" },
    ],
  },
  {
    name: "4. closing with Forward keeps Forward",
    setup: async () => {
      const ws = new StoreWorkspace(tab("F", file("a")));
      await ws.openSelected(tab("C", { kind: "changes", path: "x" }));
      await ws.openSelected(tab("P", pull()));
      return ws;
    },
    steps: [
      { name: "Back", run: (ws) => ws.back(), timeline: "[F:a, C:x*, P:#12]" },
      { name: "close C", run: (ws) => ws.close("C"), timeline: "[F:a*, P:#12]", selected: "F" },
      { name: "Forward", run: (ws) => ws.forward(), timeline: "[F:a, P:#12*]", selected: "P" },
    ],
  },
  {
    name: "5a. a line move in the selected tab replaces the cursor visit",
    setup: async () => new StoreWorkspace(tab("F", file("a.ts", 10))),
    steps: [
      {
        name: "link to a.ts:40",
        run: (ws) => ws.moveWithin("F", file("a.ts", 40)),
        timeline: "[F:a.ts:40*]",
      },
    ],
  },
  {
    name: "5b. the same move into another tab is a tab switch and appends",
    setup: async () => {
      const ws = new StoreWorkspace(tab("F", file("a.ts", 10)));
      await ws.openSelected(tab("P", pull()));
      return ws;
    },
    steps: [
      {
        name: "link to a.ts:40",
        run: (ws) => ws.moveWithin("F", file("a.ts", 40)),
        timeline: "[F:a.ts:10, P:#12, F:a.ts:40*]",
        selected: "F",
      },
    ],
  },
  {
    name: "6. a background open into a visited, unselected, clean file tab navigates it silently",
    setup: async () => {
      const ws = new StoreWorkspace(tab("A", file("a.ts")));
      await ws.openSelected(tab("P", pull()));
      return ws;
    },
    steps: [
      {
        name: "agent opens b.ts in A",
        run: (ws) => ws.openInBackground(tab("A", file("b.ts"))),
        timeline: "[A:a.ts, P:#12*]",
        selected: "P",
      },
      { name: "user selects A", run: (ws) => ws.select("A"), timeline: "[A:a.ts, P:#12, A:b.ts*]" },
      { name: "Back", run: (ws) => ws.back(), timeline: "[A:a.ts, P:#12*, A:b.ts]", selected: "P" },
      { name: "Back", run: (ws) => ws.back(), timeline: "[A:a.ts*, P:#12, A:b.ts]", selected: "A" },
    ],
  },
  {
    name: "7. Cancel in the leave guard changes nothing",
    setup: async () => {
      const ws = new StoreWorkspace(tab("F", file("z.ts")));
      await ws.openSelected(tab("P", pull()));
      await ws.editInBackground("F", file("a.ts"));
      ws.guardAnswer = "cancel";
      return ws;
    },
    steps: [
      {
        name: "Back → Cancel",
        run: (ws) => ws.back(),
        timeline: "[F:z.ts, P:#12*]",
        selected: "P",
      },
    ],
  },
  {
    name: "8. new tab → shell; the new-tab page is never a place Back returns to",
    setup: async () => new StoreWorkspace(tab("F", file("a"))),
    steps: [
      {
        name: '"+"',
        run: (ws) => ws.openSelected(tab("N", { kind: "new" })),
        timeline: "[F:a, N*]",
      },
      {
        name: "pick Terminal",
        run: (ws) => ws.pickApp("N", tab("T", { kind: "terminal", key: "k1" })),
        timeline: "[F:a, T*]",
        selected: "T",
      },
      { name: "Back", run: (ws) => ws.back(), timeline: "[F:a*, T]", selected: "F" },
      { name: "Forward", run: (ws) => ws.forward(), timeline: "[F:a, T*]", selected: "T" },
      { name: "close T", run: (ws) => ws.close("T"), timeline: "[F:a*]", selected: "F" },
    ],
  },
  {
    name: "9. `current` into a bound place opens a new tab and leaves F unchanged",
    setup: async () => new StoreWorkspace(tab("F", file("a"))),
    steps: [
      {
        name: "omnigent://terminal/k1 with current",
        run: (ws) => ws.openSelected(tab("T1", { kind: "terminal", key: "k1" }), "current"),
        timeline: "[F:a, T1*]",
        selected: "T1",
      },
    ],
  },
];

describe("workspace history traces", () => {
  it.each(traces)("$name", async ({ setup, steps }) => {
    const workspace = await setup();
    expect(workspace.invariantHolds()).toBe(true);
    for (const step of steps) {
      // Each step acts on the state the previous one left.
      // oxlint-disable-next-line no-await-in-loop
      await step.run(workspace);
      expect(describeHistory(workspace.history), step.name).toBe(step.timeline);
      if (step.selected) expect(workspace.selectedTabId, step.name).toBe(step.selected);
      expect(workspace.invariantHolds(), `invariant after ${step.name}`).toBe(true);
    }
  });

  it("2. Back into an in-tab step navigates that tab in place", async () => {
    const ws = new StoreWorkspace(tab("F", file("a")));
    await ws.openSelected(tab("P", pull()));
    await ws.navigate("P", pull("changes", "main.go"));
    await ws.back();
    expect(ws.tab("P").place).toEqual(pull());
  });

  it("6. Back into A navigates it back to the file it showed", async () => {
    const ws = new StoreWorkspace(tab("A", file("a.ts")));
    await ws.openSelected(tab("P", pull()));
    await ws.openInBackground(tab("A", file("b.ts")));
    expect(ws.tab("A").dot).toBe(true);
    await ws.select("A");
    await ws.back();
    await ws.back();
    expect(ws.tab("A").place).toEqual(file("a.ts"));
  });

  it("6. the same open naming the Files tab only sets attention, since agents never move it", async () => {
    const ws = new StoreWorkspace(tab("F", file("a.ts"), { role: "files" }));
    await ws.openSelected(tab("P", pull()));
    await ws.openInBackground(tab("F", file("b.ts")));
    expect(ws.tab("F")).toMatchObject({ place: file("a.ts"), attention: file("b.ts") });
    expect(describeHistory(ws.history)).toBe("[F:a.ts, P:#12*]");
  });

  it("7. Cancel asks once and leaves the dirty tab's place; Leave navigates it", async () => {
    const ws = new StoreWorkspace(tab("F", file("z.ts")));
    await ws.openSelected(tab("P", pull()));
    await ws.editInBackground("F", file("a.ts"));
    ws.guardAnswer = "cancel";
    await ws.back();
    expect(ws.guardCalls).toEqual([["F"]]);
    expect(ws.tab("F").place).toEqual(file("a.ts"));

    ws.guardAnswer = "leave";
    await ws.back();
    expect(describeHistory(ws.history)).toBe("[F:z.ts*, P:#12]");
    expect(ws.tab("F").place).toEqual(file("z.ts"));
  });

  it("1. an agent's open into the selected or a dirty tab only sets attention", async () => {
    const ws = new StoreWorkspace(tab("F", file("a.ts")));
    await ws.openInBackground(tab("F", file("main.go")));
    expect(ws.tab("F").place).toEqual(file("a.ts"));
    expect(ws.tab("F").attention).toEqual(file("main.go"));
    expect(describeHistory(ws.history)).toBe("[F:a.ts*]");
  });

  it("closing the last visited tab opens a new-tab page with a fresh timeline", async () => {
    const ws = new StoreWorkspace(tab("F", file("a")));
    await ws.close("F");
    expect(describeHistory(ws.history)).toBe("[new-0*]");
    expect(ws.invariantHolds()).toBe(true);
  });

  it("closing an unselected tab only removes its visits", async () => {
    const ws = new StoreWorkspace(tab("F", file("a")));
    await ws.openSelected(tab("P", pull()));
    await ws.openSelected(tab("C", { kind: "changes", path: "x" }));
    await ws.close("P");
    expect(describeHistory(ws.history)).toBe("[F:a, C:x*]");
    expect(ws.selectedTabId).toBe("C");
  });
});
