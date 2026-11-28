import { describe, expect, it } from "vitest";

import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import type { StackParse } from "@/lib/pullRequestStacks/stackModel";
import {
  AT_STACK_BASE,
  AT_STACK_TOP,
  CHILD_NOT_PUSHED,
  firstInStack,
  foldStackRows,
  IN_OTHER_REPOSITORY,
  lastInStack,
  MAX_STACK_INDENT,
  isStraightStack,
  revealStackRows,
  STACK_FOLDED,
  type StackFoldSegment,
  stackRowKey,
  NOT_PUSHED,
  treeGuides,
  NOTHING_STACKED,
  nextInStack,
  PARENT_NOT_PUSHED,
  previousInStack,
  stackPosition,
  stackTreeRows,
} from "@/lib/pullRequestStacks/stackNavigation";

// Stack descriptions in an example format, which the page reads once a build registers it.
const STACK_FORMATS = [EXAMPLE_STACK_FORMAT];
withPullRequestIntegrations({ stackFormats: STACK_FORMATS });

const prUrl = (number: number, repository = "acme/app") =>
  `https://github.com/${repository}/pull/${number}`;

/** `[depth, number or branch, repository?]` per entry; a string is a branch not pushed yet. */
type Entry = [number, number | string, string?];

/** A stack description listing `entries`, read as PR `self`'s. */
function stackOf(entries: Entry[], self: number): StackParse {
  const lines = entries.map(([depth, entry, repository]) => {
    const indent = " ".repeat(depth * 2);
    if (typeof entry === "string") return `${indent}- ${entry}`;
    const label = entry === self ? `**stack/${entry}**` : `stack/${entry}`;
    const marker = entry === self ? " ← _this PR_" : "";
    return `${indent}- [${label}](${prUrl(entry, repository)})${marker}`;
  });
  const body = `## Stacked changes\n${lines.join("\n")}\n\n---------\nThe change.\n`;
  const parse = parseStack(STACK_FORMATS, body, {
    host: "github.com",
    repository: "acme/app",
    number: self,
  });
  if (!parse) throw new Error("not a stack");
  return parse;
}

const numberOf = (step: ReturnType<typeof nextInStack>) =>
  step.kind === "go" ? step.destination.node.ref?.number : step.kind;

describe("stackPosition", () => {
  it("names this PR's level of the stack's levels, base first", () => {
    const stack: Entry[] = [
      [0, 100],
      [1, 101],
      [2, 103],
      [2, 104],
    ];
    expect(stackPosition(stackOf(stack, 101))).toEqual({ level: 2, levels: 3 });
    expect(stackPosition(stackOf(stack, 100))).toEqual({ level: 1, levels: 3 });
  });
});

describe("previousInStack", () => {
  it("goes to the direct parent, not the stack's base", () => {
    const step = previousInStack(
      stackOf(
        [
          [0, 100],
          [1, 101],
          [2, 102],
        ],
        102,
      ),
    );
    expect(step).toMatchObject({ kind: "go", destination: { url: prUrl(101) } });
  });

  it("explains why it can't go below the base", () => {
    expect(
      previousInStack(
        stackOf(
          [
            [0, 100],
            [1, 101],
          ],
          100,
        ),
      ),
    ).toEqual({ kind: "unavailable", reason: AT_STACK_BASE });
  });

  it("can't go to a parent that isn't pushed yet", () => {
    expect(
      previousInStack(
        stackOf(
          [
            [0, 100],
            [1, "stack/middle"],
            [2, 102],
          ],
          102,
        ),
      ),
    ).toEqual({ kind: "unavailable", reason: PARENT_NOT_PUSHED });
  });

  it("doesn't open a parent in another repository in place", () => {
    expect(
      previousInStack(
        stackOf(
          [
            [0, 100, "other-org/lib"],
            [1, 101],
          ],
          101,
        ),
      ),
    ).toEqual({ kind: "unavailable", reason: IN_OTHER_REPOSITORY });
  });
});

describe("nextInStack", () => {
  it("goes to the only PR stacked on this one", () => {
    const step = nextInStack(
      stackOf(
        [
          [0, 100],
          [1, 101],
          [2, 102],
        ],
        100,
      ),
    );
    expect(numberOf(step)).toBe(101);
  });

  it("asks which of several PRs stacked on this one, never picking a sibling", () => {
    const step = nextInStack(
      stackOf(
        [
          [0, 100],
          [1, 101],
          [2, 103],
          [2, "stack/docs"],
          [1, 104],
        ],
        101,
      ),
    );
    expect(step.kind).toBe("choose");
    if (step.kind !== "choose") return;
    // Only this PR's own children, the unpushed one listed but not openable.
    expect(step.destinations.map((choice) => [choice.node.branch, choice.url])).toEqual([
      ["stack/103", prUrl(103)],
      ["stack/docs", null],
    ]);
    expect(step.destinations[1].unavailableReason).toBe(NOT_PUSHED);
  });

  it("explains a stack's top, and a child not pushed yet", () => {
    expect(
      nextInStack(
        stackOf(
          [
            [0, 100],
            [1, 101],
          ],
          101,
        ),
      ),
    ).toEqual({ kind: "unavailable", reason: NOTHING_STACKED });
    expect(
      nextInStack(
        stackOf(
          [
            [0, 100],
            [1, "stack/next"],
          ],
          100,
        ),
      ),
    ).toEqual({ kind: "unavailable", reason: CHILD_NOT_PUSHED });
  });
});

describe("stackTreeRows", () => {
  it("draws a tee before a sibling, an elbow for the last child, and carries guides", () => {
    const rows = stackTreeRows(
      stackOf(
        [
          [0, 100],
          [1, 101],
          [2, 103],
          [2, 104],
          [1, 105],
          [2, 106],
        ],
        101,
      ),
    );
    expect(rows.map(({ connector, guides }) => [connector, guides])).toEqual([
      [null, []],
      ["tee", []],
      ["tee", [true]],
      ["elbow", [true]],
      ["elbow", []],
      ["elbow", [false]],
    ]);
  });

  it("stops indenting past the deepest level and names the level instead", () => {
    const depth = MAX_STACK_INDENT + 2;
    const entries: Entry[] = Array.from({ length: depth + 1 }, (_value, index) => [
      index,
      100 + index,
    ]);
    const rows = stackTreeRows(stackOf(entries, 100));
    const deepest = rows.at(-1);
    expect(deepest?.guides.length).toBe(MAX_STACK_INDENT - 1);
    expect(deepest?.deepLevel).toBe(depth + 1);
    expect(rows[MAX_STACK_INDENT].deepLevel).toBeNull();
  });
});

describe("treeGuides", () => {
  it("draws each ancestor's line once its later sibling is known, capped at the indent limit", () => {
    // 0 ─┬ 1 ── 2 ── 3 ── 4 ── 5, and 0 ─ 6 after them: 1 has a later sibling.
    const parents = [null, 0, 1, 2, 3, 4, 0];
    const nodes = parents.map((parentIndex, index) => ({
      parentIndex,
      depth: index === 6 ? 1 : index,
    }));
    const guides = treeGuides(nodes);
    expect(guides.map((row) => row.connector)).toEqual([
      null,
      "tee",
      "elbow",
      "elbow",
      "elbow",
      "elbow",
      "elbow",
    ]);
    expect(guides[1]!.guides).toEqual([]);
    expect(guides[2]!.guides).toEqual([true]);
    expect(guides[3]!.guides).toEqual([true, false]);
    expect(guides[4]!.guides).toEqual([true, false, false]);
    expect(guides[5]!.guides).toEqual([true, false, false]);
    expect(guides[5]!.guides).toHaveLength(MAX_STACK_INDENT - 1);
    expect(guides[6]!.guides).toEqual([]);
  });

  it("works out a row whose parent is listed after it", () => {
    const guides = treeGuides([
      { parentIndex: 2, depth: 2 },
      { parentIndex: null, depth: 0 },
      { parentIndex: 1, depth: 1 },
    ]);
    expect(guides.map((row) => [row.connector, row.guides])).toEqual([
      ["elbow", [false]],
      [null, []],
      ["elbow", []],
    ]);
  });
});

describe("isStraightStack", () => {
  it("is a line where every entry has at most one stacked on it, and not where one forks", () => {
    const chain: Entry[] = [
      [0, 100],
      [1, 101],
      [2, 102],
    ];
    expect(isStraightStack(stackOf(chain, 101).nodes)).toBe(true);
    // #101 has two stacked on it: #102 and an unpushed branch.
    const forked: Entry[] = [...chain, [2, "stack/docs"]];
    expect(isStraightStack(stackOf(forked, 101).nodes)).toBe(false);
    // Two bases fork at the root.
    expect(isStraightStack([{ parentIndex: null }, { parentIndex: null }])).toBe(false);
    expect(isStraightStack([{ parentIndex: null }])).toBe(true);
    expect(isStraightStack([])).toBe(true);
  });
});

describe("firstInStack and lastInStack", () => {
  const straight: Entry[] = [
    [0, 100],
    [1, 101],
    [2, 102],
  ];

  it("go to the stack's first and last entries from the middle", () => {
    const parse = stackOf(straight, 101);
    expect(firstInStack(parse)).toMatchObject({ kind: "go", destination: { url: prUrl(100) } });
    expect(lastInStack(parse)).toMatchObject({ kind: "go", destination: { url: prUrl(102) } });
  });

  it("say this PR is already the end it would go to", () => {
    expect(firstInStack(stackOf(straight, 100))).toEqual({
      kind: "unavailable",
      reason: AT_STACK_BASE,
    });
    expect(lastInStack(stackOf(straight, 102))).toEqual({
      kind: "unavailable",
      reason: AT_STACK_TOP,
    });
  });

  it("can't open an end that isn't pushed or is in another repository", () => {
    const parse = stackOf(
      [
        [0, 100, "other-org/lib"],
        [1, 101],
        [2, "stack/docs"],
      ],
      101,
    );
    expect(firstInStack(parse)).toEqual({ kind: "unavailable", reason: IN_OTHER_REPOSITORY });
    expect(lastInStack(parse)).toEqual({ kind: "unavailable", reason: NOT_PUSHED });
  });
});

describe("foldStackRows", () => {
  const rows = (count: number) => Array.from({ length: count }, (_, index) => index + 1);
  const keyOf = (row: number) => `pr:${row}`;
  /** Each segment as its row, or `[first hidden, last hidden]` for a gap. */
  const shape = (segments: StackFoldSegment<number>[]) =>
    segments.map((segment) =>
      segment.kind === "row" ? segment.row : [segment.rows[0], segment.rows.at(-1)],
    );

  it("shows a stack of up to twelve rows whole, and folds a longer one", () => {
    expect(shape(foldStackRows(rows(12), 5, keyOf, STACK_FOLDED))).toEqual(rows(12));
    expect(shape(foldStackRows(rows(13), 6, keyOf, STACK_FOLDED))).toEqual([
      1,
      [2, 3],
      4,
      5,
      6,
      7,
      8,
      9,
      10,
      [11, 12],
      13,
    ]);
  });

  it("keeps the first and last rows and three on each side of this PR", () => {
    const segments = foldStackRows(rows(40), 10, keyOf, STACK_FOLDED);
    expect(shape(segments)).toEqual([1, [2, 7], 8, 9, 10, 11, 12, 13, 14, [15, 39], 40]);
    expect(segments[1]).toMatchObject({ kind: "gap", before: 1, after: 8 });
    expect(segments[1]!.kind === "gap" && segments[1]!.keys).toEqual([
      "pr:2",
      "pr:3",
      "pr:4",
      "pr:5",
      "pr:6",
      "pr:7",
    ]);
  });

  it("moves the context inward at either end, so as many rows show", () => {
    expect(shape(foldStackRows(rows(40), 0, keyOf, STACK_FOLDED))).toEqual([
      1,
      2,
      3,
      4,
      5,
      6,
      7,
      8,
      [9, 39],
      40,
    ]);
    expect(shape(foldStackRows(rows(40), 39, keyOf, STACK_FOLDED))).toEqual([
      1,
      [2, 32],
      33,
      34,
      35,
      36,
      37,
      38,
      39,
      40,
    ]);
  });

  it("shows a lone folded row instead of a gap for it", () => {
    expect(shape(foldStackRows(rows(40), 4, keyOf, STACK_FOLDED))).toEqual([
      1,
      2,
      3,
      4,
      5,
      6,
      7,
      8,
      [9, 39],
      40,
    ]);
  });

  it("opens exactly a gap's rows, and all of them with Expand all", () => {
    const folded = foldStackRows(rows(40), 10, keyOf, STACK_FOLDED);
    const gap = folded[1]!;
    if (gap.kind !== "gap") throw new Error("expected a gap");
    const opened = revealStackRows(STACK_FOLDED, gap.keys);
    expect(shape(foldStackRows(rows(40), 10, keyOf, opened))).toEqual([...rows(14), [15, 39], 40]);
    expect(
      shape(foldStackRows(rows(40), 10, keyOf, { ...STACK_FOLDED, expandedAll: true })),
    ).toEqual(rows(40));
  });

  it("keeps opened rows by identity when a later read lists the stack in another order", () => {
    const opened = revealStackRows(STACK_FOLDED, ["pr:3", "pr:4"]);
    // The same PRs listed in reverse: #3 and #4 now sit near the top, and stay shown there.
    const reversed = rows(40).reverse();
    expect(shape(foldStackRows(reversed, 29, keyOf, opened))).toEqual([
      40,
      [39, 15],
      14,
      13,
      12,
      11,
      10,
      9,
      8,
      [7, 5],
      4,
      3,
      2,
      1,
    ]);
  });

  it("drops an opened row that's gone from the stack, and opens nothing in its place", () => {
    const opened = revealStackRows(STACK_FOLDED, ["pr:3", "pr:4"]);
    const without = rows(40).filter((row) => row !== 3 && row !== 4);
    expect(shape(foldStackRows(without, 8, keyOf, opened))).toEqual([
      1,
      [2, 7],
      8,
      9,
      10,
      11,
      12,
      13,
      14,
      [15, 39],
      40,
    ]);
  });

  it("counts only the rows a stack read in part has, never its unread positions", () => {
    // Positions 1, 10–30 and 40 read: the first gap folds #10–#15, six rows, not 1 to 16.
    const sparse = [1, ...Array.from({ length: 21 }, (_, index) => index + 10), 40];
    const segments = foldStackRows(sparse, 10, keyOf, STACK_FOLDED);
    const gaps = segments.filter((segment) => segment.kind === "gap");
    expect(gaps.map((gap) => [gap.rows.length, gap.rows[0], gap.rows.at(-1)])).toEqual([
      [6, 10, 15],
      [8, 23, 30],
    ]);
  });

  it("keys a row by its PR, or by its branch before it has one", () => {
    expect(stackRowKey({ ref: { url: prUrl(7) } as never, branch: "stack/a" })).toBe(prUrl(7));
    expect(stackRowKey({ ref: null, branch: "stack/a" })).toBe("branch:stack/a");
  });
});
