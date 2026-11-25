// Every preview of a pane placement shows where the pane lands: for each spot a strip drag or a
// dropped sidebar tab offers in a few layouts, the preview matches the layout the commit saves.

import { afterEach, describe, expect, it } from "vitest";

import {
  terminalLayoutGeometry,
  terminalLayoutKeys,
  type TerminalLayout,
} from "@/lib/paneSplitLayout";

import { appPaneDropAt, dropTabAsPane, resetAppPanes } from "./appPanes";
import { moveSplitPane, PANE_SIDES, paneMoveDestinations, placedPaneArea } from "./paneMove";
import { readPaneSplit, resetPaneSplitsCache, updatePaneSplit } from "./paneSplitStore";
import { mainSplitTarget } from "./terminalSplitTarget";

const AGENT = "agent-terminal";
const columns = (first: TerminalLayout, second: TerminalLayout, ratio = 0.5): TerminalLayout => ({
  axis: "columns",
  ratio,
  children: [first, second],
});
const rows = (first: TerminalLayout, second: TerminalLayout, ratio = 0.5): TerminalLayout => ({
  axis: "rows",
  ratio,
  children: [first, second],
});

const LAYOUTS: readonly [string, TerminalLayout][] = [
  ["side by side", columns(AGENT, "b")],
  ["a column beside a stack", columns(AGENT, rows("b", "c"), 0.4)],
  ["a two by two grid", rows(columns(AGENT, "b"), columns("c", "d", 0.3))],
  ["thirds in a row", columns(AGENT, columns("b", "c"), 1 / 3)],
];

let nextSession = 0;
const VIEW = { width: 1000, height: 500 };

function startSession(layout: TerminalLayout): string {
  const conversationId = `placement-${nextSession++}`;
  updatePaneSplit(conversationId, () => ({ layout, focusedKey: AGENT }));
  const root = document.createElement("div");
  root.dataset.splitPanes = conversationId;
  root.getBoundingClientRect = () => new DOMRect(0, 0, VIEW.width, VIEW.height);
  document.body.append(root);
  return conversationId;
}

function landedArea(conversationId: string, key: string) {
  const pane = terminalLayoutGeometry(readPaneSplit(conversationId)!.layout).panes.find(
    (each) => each.key === key,
  );
  return pane && { left: pane.left, top: pane.top, width: pane.width, height: pane.height };
}

function expectSameArea(
  preview: { left: number; top: number; width: number; height: number } | null | undefined,
  landed: { left: number; top: number; width: number; height: number } | undefined,
) {
  expect(preview).toBeTruthy();
  expect(landed).toBeTruthy();
  for (const edge of ["left", "top", "width", "height"] as const) {
    expect(landed![edge]).toBeCloseTo(preview![edge]);
  }
}

/** A point in `side`'s zone of `targetKey`'s pane, in client pixels of the view. */
function pointInZone(layout: TerminalLayout, targetKey: string, side: (typeof PANE_SIDES)[number]) {
  const pane = terminalLayoutGeometry(layout).panes.find((each) => each.key === targetKey)!;
  const across = { left: 0.1, right: 0.9, top: 0.5, bottom: 0.5 }[side];
  const down = { left: 0.5, right: 0.5, top: 0.1, bottom: 0.9 }[side];
  return {
    x: ((pane.left + pane.width * across) / 100) * VIEW.width,
    y: ((pane.top + pane.height * down) / 100) * VIEW.height,
  };
}

afterEach(() => {
  document.body.replaceChildren();
  window.localStorage.clear();
  resetPaneSplitsCache();
  resetAppPanes();
});

describe("pane placement previews", () => {
  describe.each(LAYOUTS)("in %s", (_name, layout) => {
    it("previews every strip move where the pane lands", () => {
      for (const key of terminalLayoutKeys(layout)) {
        for (const { targetKey, side } of paneMoveDestinations(layout, key, null)) {
          const conversationId = startSession(layout);
          const preview = placedPaneArea(layout, { kind: "move", key, targetKey, side });
          moveSplitPane(mainSplitTarget(conversationId), { key, targetKey, side }, null);
          expectSameArea(preview, landedArea(conversationId, key));
        }
      }
    });

    it("previews every sidebar tab drop where the tab lands", () => {
      for (const targetKey of terminalLayoutKeys(layout)) {
        for (const side of PANE_SIDES) {
          const conversationId = startSession(layout);
          const drop = appPaneDropAt(conversationId, pointInZone(layout, targetKey, side), AGENT);
          expect(drop).toMatchObject({ targetKey, side });
          expect(dropTabAsPane(conversationId, "files", drop!, AGENT)).toBe(true);
          expectSameArea(drop!.area, landedArea(conversationId, "app:files"));
        }
      }
    });
  });
});
