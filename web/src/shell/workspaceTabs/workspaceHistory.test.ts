import { describe, expect, it } from "vitest";

import { describeHistory } from "./__fixtures__/historyWorkspace";
import {
  alignWithSelection,
  backTarget,
  cursorMatches,
  EMPTY_WORKSPACE_HISTORY,
  forwardTarget,
  goBack,
  goForward,
  historyStartingAt,
  moveCursorTo,
  recordStep,
  removeTab,
  replaceCurrent,
  replaceNewTab,
  restoreHistory,
  serializeHistory,
  WORKSPACE_HISTORY_LIMIT,
  WorkspaceHistory,
  type WorkspaceHistoryState,
} from "./workspaceHistory";
import type { Visit, WorkspacePlace } from "./workspacePlace";

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
function visit(tabId: string, place: WorkspacePlace): Visit {
  return { tabId, place };
}

describe("recordStep", () => {
  const start = historyStartingAt(visit("F", file("a")));

  it("drops Forward after a new step", () => {
    const three = recordStep(
      recordStep(start, visit("P", pull())),
      visit("C", { kind: "changes" }),
    );
    const afterBack = goBack(goBack(three));
    const stepped = recordStep(afterBack, visit("S", { kind: "search" }));
    expect(describeHistory(stepped)).toBe("[F:a, S*]");
    expect(forwardTarget(stepped)).toBeNull();
  });

  it("does nothing for the visit the cursor is already on", () => {
    expect(recordStep(start, visit("F", file("a")))).toBe(start);
  });

  it("keeps the newest 100 visits and the cursor on the last", () => {
    let state: WorkspaceHistoryState = EMPTY_WORKSPACE_HISTORY;
    for (let index = 0; index < WORKSPACE_HISTORY_LIMIT + 5; index++) {
      state = recordStep(state, visit("F", file(`f${index}.ts`)));
    }
    expect(state.visits).toHaveLength(WORKSPACE_HISTORY_LIMIT);
    expect(state.visits[0]).toEqual(visit("F", file("f5.ts")));
    expect(state.cursor).toBe(WORKSPACE_HISTORY_LIMIT - 1);
  });

  it("starts an empty timeline", () => {
    expect(describeHistory(recordStep(EMPTY_WORKSPACE_HISTORY, visit("F", file("a"))))).toBe(
      "[F:a*]",
    );
  });
});

describe("replaceCurrent", () => {
  it("never touches a visit other than the cursor's", () => {
    const state = goBack(
      recordStep(historyStartingAt(visit("F", file("a.ts", 1))), visit("F", file("b.ts"))),
    );
    const replaced = replaceCurrent(state, visit("F", file("a.ts", 9)));
    expect(describeHistory(replaced)).toBe("[F:a.ts:9*, F:b.ts]");
  });

  it("appends when the cursor visit belongs to another tab", () => {
    const state = historyStartingAt(visit("P", pull()));
    expect(describeHistory(replaceCurrent(state, visit("F", file("a.ts", 9))))).toBe(
      "[P:#12, F:a.ts:9*]",
    );
  });

  it("collapses into an identical neighbour instead of leaving a no-op Back", () => {
    const state = recordStep(
      historyStartingAt(visit("W", { kind: "web", url: "https://x.test/a" })),
      {
        tabId: "W",
        place: { kind: "web", url: "https://x.test/redirecting" },
      },
    );
    const replaced = replaceCurrent(state, visit("W", { kind: "web", url: "https://x.test/a" }));
    expect(describeHistory(replaced)).toBe("[W:/a*]");
  });
});

describe("replaceNewTab", () => {
  it("turns the new-tab page into an app in the same tab without a step", () => {
    const state = recordStep(historyStartingAt(visit("F", file("a"))), visit("N", { kind: "new" }));
    expect(describeHistory(replaceNewTab(state, "N", visit("N", { kind: "changes" })))).toBe(
      "[F:a, N*]",
    );
  });

  it("puts a bound tab in the new-tab page's slot and drops its other visits", () => {
    let state = recordStep(historyStartingAt(visit("N", { kind: "new" })), visit("F", file("a")));
    state = recordStep(state, visit("N", { kind: "new" }));
    const replaced = replaceNewTab(state, "N", visit("T", { kind: "terminal", key: "k" }));
    expect(describeHistory(replaced)).toBe("[F:a, T*]");
  });

  it("appends when the new-tab page has no visit", () => {
    const state = historyStartingAt(visit("F", file("a")));
    expect(
      describeHistory(replaceNewTab(state, "N", visit("T", { kind: "terminal", key: "k" }))),
    ).toBe("[F:a, T*]");
  });
});

describe("removeTab", () => {
  it("moves to the nearest later visit when nothing earlier survives", () => {
    const state = goBack(recordStep(historyStartingAt(visit("F", file("a"))), visit("P", pull())));
    const { state: next, restore } = removeTab(state, "F");
    expect(describeHistory(next)).toBe("[P:#12*]");
    expect(restore).toEqual(visit("P", pull()));
  });

  it("returns no visit when the cursor was elsewhere, keeping the cursor on its visit", () => {
    let state = historyStartingAt(visit("F", file("a")));
    state = recordStep(state, visit("P", pull()));
    state = recordStep(state, visit("C", { kind: "changes" }));
    const { state: next, restore } = removeTab(state, "F");
    expect(restore).toBeNull();
    expect(describeHistory(next)).toBe("[P:#12, C*]");
  });

  it("collapses the identical neighbours a removal leaves behind", () => {
    let state = historyStartingAt(visit("F", file("a")));
    state = recordStep(state, visit("C", { kind: "changes", path: "x" }));
    state = recordStep(state, visit("F", file("a")));
    const { state: next, restore } = removeTab(state, "C");
    expect(describeHistory(next)).toBe("[F:a*]");
    expect(restore).toBeNull();
  });

  it("returns null with an empty timeline when nothing survives", () => {
    const { state, restore } = removeTab(historyStartingAt(visit("F", file("a"))), "F");
    expect(state).toEqual(EMPTY_WORKSPACE_HISTORY);
    expect(restore).toBeNull();
  });

  it("leaves a timeline without that tab untouched", () => {
    const state = historyStartingAt(visit("F", file("a")));
    expect(removeTab(state, "P").state).toBe(state);
  });
});

describe("back and forward", () => {
  const state = goBack(
    recordStep(historyStartingAt(visit("F", file("a"))), visit("W", { kind: "web" })),
  );

  it("targets without moving the cursor", () => {
    expect(forwardTarget(state)).toEqual(visit("W", { kind: "web" }));
    expect(state.cursor).toBe(0);
    expect(backTarget(state)).toBeNull();
    expect(goBack(state)).toBe(state);
  });

  it("is disabled onto a blocked visit, not skipped past it", () => {
    const blocked = { isBlocked: (target: Visit) => target.tabId === "W" };
    expect(forwardTarget(state, blocked)).toBeNull();
    expect(goForward(state, blocked)).toBe(state);
  });

  it("moveCursorTo keeps the timeline and ignores out-of-range indexes", () => {
    const forward = goForward(state);
    expect(moveCursorTo(forward, 0).visits).toBe(forward.visits);
    expect(moveCursorTo(forward, 0).cursor).toBe(0);
    expect(moveCursorTo(forward, 7)).toBe(forward);
  });
});

describe("alignWithSelection", () => {
  it("replaces the cursor visit of the selected tab and steps into another", () => {
    const state = historyStartingAt(visit("F", file("a", 1)));
    expect(describeHistory(alignWithSelection(state, visit("F", file("a", 5))))).toBe("[F:a:5*]");
    expect(describeHistory(alignWithSelection(state, visit("P", pull())))).toBe("[F:a:1, P:#12*]");
    expect(cursorMatches(state, "F", file("a", 1))).toBe(true);
    expect(cursorMatches(state, "F", file("a", 2))).toBe(false);
  });
});

describe("visits compare places by their canonical link", () => {
  it("a place that differs only by an unset field is not a new step", () => {
    const state = historyStartingAt(visit("F", { kind: "files", path: "a" }));
    expect(recordStep(state, visit("F", { kind: "files", path: "a", line: undefined }))).toBe(
      state,
    );
    expect(recordStep(state, visit("F", file("a", 2)))).not.toBe(state);
  });
});

describe("serialize and restore", () => {
  it("round-trips", () => {
    let state = historyStartingAt(visit("F", file("a", 3)));
    state = recordStep(state, {
      tabId: "W",
      place: { kind: "web", url: "https://x.test/a" },
      entry: { index: 2 },
    });
    state = goBack(state);
    const json = JSON.parse(JSON.stringify(serializeHistory(state)));
    expect(restoreHistory(json, () => true)).toEqual(state);
  });

  it("drops malformed visits and visits of tabs that no longer exist, keeping the cursor on its visit", () => {
    const raw = {
      visits: [
        { tabId: "F", place: { kind: "files", path: "a" } },
        { tabId: "", place: { kind: "files" } },
        { tabId: "gone", place: { kind: "pulls" } },
        { tabId: "P", place: "not a place" },
        { tabId: "C", place: { kind: "changes" }, entry: { index: -1 } },
        { tabId: "S", place: { kind: "search" } },
      ],
      cursor: 4,
    };
    const restored = restoreHistory(raw, (tabId) => tabId !== "gone");
    expect(describeHistory(restored)).toBe("[F:a, C*, S]");
    expect(restored.visits[1]).toEqual(visit("C", { kind: "changes" }));
  });

  it("falls back to the nearest earlier survivor, else the first", () => {
    const raw = {
      visits: [
        { tabId: "gone", place: { kind: "pulls" } },
        { tabId: "F", place: { kind: "files" } },
      ],
      cursor: 0,
    };
    expect(describeHistory(restoreHistory(raw, (tabId) => tabId !== "gone"))).toBe("[F*]");
  });

  it.each([null, "x", [], { visits: "no" }, { visits: [] }])("treats %j as empty", (raw) => {
    expect(restoreHistory(raw, () => true)).toEqual(EMPTY_WORKSPACE_HISTORY);
  });
});

describe("WorkspaceHistory", () => {
  it("exposes the store-facing methods over the pure functions", () => {
    const history = new WorkspaceHistory(historyStartingAt(visit("F", file("a"))));
    history.recordStep(visit("P", pull()));
    expect(history.canGoBack()).toBe(true);
    expect(history.backTarget()).toEqual(visit("F", file("a")));
    expect(history.back()).toEqual(visit("F", file("a")));
    expect(history.canGoForward()).toBe(true);
    expect(history.forward()).toEqual(visit("P", pull()));
    history.replaceCurrent(visit("P", pull("changes")));
    expect(history.removeTab("P")).toEqual(visit("F", file("a")));
    history.replaceNewTab("F", visit("T", { kind: "terminal", key: "k" }));
    expect(describeHistory(history.state)).toBe("[T*]");
    const copy = new WorkspaceHistory();
    copy.restore(history.serialize(), () => true);
    expect(copy.state).toEqual(history.state);
    expect(copy.back()).toBeNull();
  });
});
