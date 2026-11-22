import { describe, expect, it } from "vitest";

import {
  groupKeys,
  groupOfTerminal,
  normalizeTerminalGroups,
  parseTerminalGroups,
  projectStrip,
  type TerminalGroup,
  withoutTerminal,
} from "./terminalGroups";

const group = (id: string, ...keys: string[]): TerminalGroup => {
  // Columns from left to right, as repeated splits along a row build them.
  let layout: TerminalGroup["split"]["layout"] = keys[keys.length - 1];
  for (let index = keys.length - 2; index >= 0; index--) {
    layout = { axis: "columns", ratio: 1 / (keys.length - index), children: [keys[index], layout] };
  }
  return { id, split: { layout, focusedKey: keys[0] } };
};
const notGone = () => false;

describe("parseTerminalGroups", () => {
  it("keeps each valid group and drops malformed ones on their own", () => {
    const valid = group("g_one", "terminal:a", "terminal:b");
    expect(
      parseTerminalGroups([
        valid,
        { id: "not-a-group-id", split: valid.split },
        { id: "g_two", split: { layout: "terminal:a", focusedKey: "terminal:a" } },
        { id: "g_one", split: valid.split },
        { id: "g_deep", split: { layout: deeplyNested(100_000), focusedKey: "terminal:a" } },
        null,
      ]),
    ).toEqual([valid]);
    expect(parseTerminalGroups("nothing")).toEqual([]);
  });
});

/** A damaged saved layout nested `depth` splits deep. */
function deeplyNested(depth: number): unknown {
  let layout: unknown = "terminal:a";
  for (let index = 0; index < depth; index++) {
    layout = { axis: "rows", ratio: 0.5, children: [`terminal:p${index}`, layout] };
  }
  return layout;
}

describe("withoutTerminal", () => {
  it("takes a shell out of its group, dissolving a group left with one pane", () => {
    const three = group("g_three", "terminal:a", "terminal:b", "terminal:c");
    const fewer = withoutTerminal([three], "terminal:b");
    expect(groupKeys(fewer[0])).toEqual(["terminal:a", "terminal:c"]);
    expect(withoutTerminal(fewer, "terminal:a")).toEqual([]);
    const groups = [three];
    expect(withoutTerminal(groups, "terminal:z")).toBe(groups);
  });
});

describe("normalizeTerminalGroups", () => {
  it("leaves a shell where it was first placed: the main split, then earlier groups", () => {
    const first = group("g_first", "terminal:a", "terminal:b");
    const second = group("g_second", "terminal:b", "terminal:c", "terminal:d");
    const normalized = normalizeTerminalGroups([first, second], {
      mainKeys: ["terminal:a"],
      isGone: notGone,
    });
    // a belongs to the main split, so the first group dissolves; b then stays in the second.
    expect(normalized.map((entry) => [entry.id, groupKeys(entry)])).toEqual([
      ["g_second", ["terminal:b", "terminal:c", "terminal:d"]],
    ]);
  });

  it("keeps a shell in the earlier group that survives, taking it from the later one", () => {
    const first = group("g_first", "terminal:a", "terminal:b");
    const second = group("g_second", "terminal:b", "terminal:c", "terminal:d");
    const normalized = normalizeTerminalGroups([first, second], { mainKeys: [], isGone: notGone });
    expect(normalized.map((entry) => [entry.id, groupKeys(entry)])).toEqual([
      ["g_first", ["terminal:a", "terminal:b"]],
      ["g_second", ["terminal:c", "terminal:d"]],
    ]);
  });

  it("drops shells known to be gone, and returns the same groups when nothing changes", () => {
    const groups = [group("g_one", "terminal:a", "terminal:b", "terminal:c")];
    expect(
      groupKeys(
        normalizeTerminalGroups(groups, {
          mainKeys: [],
          isGone: (key) => key === "terminal:c",
        })[0],
      ),
    ).toEqual(["terminal:a", "terminal:b"]);
    expect(normalizeTerminalGroups(groups, { mainKeys: [], isGone: notGone })).toBe(groups);
  });
});

describe("projectStrip", () => {
  const tabs = ["files", "t1", "t2", "t3", "t2-again", "t4"];
  const shellOf: Record<string, string | null> = {
    files: null,
    t1: "terminal:a",
    t2: "terminal:b",
    t3: "terminal:c",
    "t2-again": "terminal:b",
    t4: "terminal:d",
  };
  const keyOf = (id: string) => shellOf[id];

  it("shows a group once, at its first member, with every tab of its shells", () => {
    const split = group("g_bc", "terminal:b", "terminal:c");
    const strip = projectStrip(tabs, keyOf, [split]);
    expect(strip.map((entry) => (entry.kind === "tab" ? entry.id : entry.group.id))).toEqual([
      "files",
      "t1",
      "g_bc",
      "t4",
    ]);
    const shown = strip[2];
    expect(shown.kind === "group" && shown.memberIds).toEqual(["t2", "t3", "t2-again"]);
  });

  it("shows nothing for a group whose shells have no tab here", () => {
    expect(projectStrip(["files"], keyOf, [group("g_x", "terminal:x", "terminal:y")])).toEqual([
      { kind: "tab", id: "files" },
    ]);
  });

  it("finds a shell's group", () => {
    const split = group("g_bc", "terminal:b", "terminal:c");
    expect(groupOfTerminal([split], "terminal:c")).toBe(split);
    expect(groupOfTerminal([split], "terminal:a")).toBeUndefined();
  });
});
