import { describe, expect, it } from "vitest";

import { stackPosition, previousInStack, nextInStack } from "./stackNavigation";
import { trustedForComparison, descriptionMetadataEditable } from "./stackTrust";
import {
  type NativeStackAnswer,
  type NativeStackMember,
  nativeStackFingerprint,
  nativeStackKey,
  partialNativeStack,
  readNativeStack,
} from "./nativeStack";

const SELF = { host: "github.com", repository: "acme/app", number: 102 };
const url = (number: number, repository = "acme/app") =>
  `https://github.com/${repository}/pull/${number}`;

function member(number: number, over: Partial<NativeStackMember> = {}): NativeStackMember {
  return {
    url: url(number),
    number,
    state: "OPEN",
    isDraft: false,
    headRefName: `stack/${number}`,
    ...over,
  };
}

/** A stack of `numbers`, base first, with `self` at its own position. */
function answer(
  numbers: number[],
  {
    self = SELF.number,
    hasNextPage = false,
    size = numbers.length,
    baseRefName = "main",
    id = "PRS_1",
    nodes,
  }: {
    self?: number;
    hasNextPage?: boolean;
    size?: number;
    baseRefName?: string;
    id?: string;
    nodes?: NonNullable<NativeStackAnswer["stack"]>["entries"]["nodes"];
  } = {},
): NativeStackAnswer {
  const entries =
    nodes ?? numbers.map((number, index) => ({ position: index + 1, pullRequest: member(number) }));
  const own = numbers.indexOf(self);
  return {
    stackEntry: own === -1 ? null : { position: own + 1 },
    stack: { id, number: 7, size, baseRefName, entries: { hasNextPage, nodes: entries } },
  };
}

describe("readNativeStack", () => {
  it("leaves a host or shell that can't say, and a PR GitHub says isn't stacked, apart", () => {
    expect(readNativeStack(undefined, SELF)).toEqual({ kind: "unsupported" });
    expect(readNativeStack({ stack: null, stackEntry: null }, SELF)).toEqual({
      kind: "not-stacked",
    });
  });

  it.each([
    { numbers: [102], selfIndex: 0 },
    { numbers: [101, 102], selfIndex: 1 },
    { numbers: [102, 103, 104, 105, 106], selfIndex: 0 },
    { numbers: [100, 101, 102, 103, 104], selfIndex: 2 },
    { numbers: [98, 99, 100, 101, 102], selfIndex: 4 },
  ])(
    "reads a whole stack of $numbers.length with this PR at $selfIndex",
    ({ numbers, selfIndex }) => {
      const read = readNativeStack(answer(numbers), SELF);
      expect(read.kind).toBe("stacked");
      if (read.kind !== "stacked") return;
      expect(read.parse.trust).toBe("native");
      expect(read.parse.format).toBe("github-native");
      expect(read.parse.selfIndex).toBe(selfIndex);
      expect(read.parse.nodes.map((node) => node.ref?.number)).toEqual(numbers);
      expect(read.parse.nodes.map((node) => node.parentIndex)).toEqual(
        numbers.map((_, index) => (index === 0 ? null : index - 1)),
      );
      expect(read.parse.nodes.filter((node) => node.markers.current)).toHaveLength(1);
      expect(stackPosition(read.parse)).toEqual({ level: selfIndex + 1, levels: numbers.length });
      // Navigable and comparable, never a description to rewrite.
      expect(trustedForComparison(read.parse)).toBe(true);
      expect(descriptionMetadataEditable(read.parse)).toBe(false);
      expect(previousInStack(read.parse).kind).toBe(selfIndex === 0 ? "unavailable" : "go");
      expect(nextInStack(read.parse).kind).toBe(
        selfIndex === numbers.length - 1 ? "unavailable" : "go",
      );
    },
  );

  it("orders entries by position whatever order GitHub lists them in, and marks merged ones", () => {
    const read = readNativeStack(
      answer([100, 101, 102], {
        nodes: [
          { position: 3, pullRequest: member(102) },
          { position: 1, pullRequest: member(100, { state: "MERGED" }) },
          { position: 2, pullRequest: member(101) },
        ],
      }),
      SELF,
    );
    if (read.kind !== "stacked") throw new Error(read.kind);
    expect(read.parse.nodes.map((node) => [node.ref?.number, node.markers.merged])).toEqual([
      [100, true],
      [101, false],
      [102, false],
    ]);
    expect(read.parse.nodes.map((node) => node.branch)).toEqual([
      "stack/100",
      "stack/101",
      "stack/102",
    ]);
  });

  it.each([
    {
      cause: "more entries on GitHub",
      input: answer([101, 102], { hasNextPage: true }),
      gaps: ["more-entries"],
    },
    {
      cause: "fewer entries than the stack's size",
      input: answer([101, 102], { size: 4 }),
      gaps: ["missing-entries"],
    },
    {
      cause: "a member this account can't see",
      input: answer([101, 102], {
        nodes: [
          { position: 1, pullRequest: null },
          { position: 2, pullRequest: member(102) },
        ],
      }),
      gaps: ["invisible-member"],
    },
    {
      cause: "a repeated position",
      input: answer([101, 102], {
        nodes: [
          { position: 1, pullRequest: member(101) },
          { position: 1, pullRequest: member(102) },
        ],
      }),
      gaps: ["positions", "self-missing"],
    },
    {
      cause: "a position outside the stack",
      input: answer([101, 102], {
        nodes: [
          { position: 1, pullRequest: member(101) },
          { position: 3, pullRequest: member(102) },
        ],
      }),
      gaps: ["positions", "self-missing"],
    },
    {
      cause: "this PR not among the entries",
      input: { ...answer([100, 101]), stackEntry: { position: 3 } },
      gaps: ["self-missing"],
    },
    {
      cause: "a member in another repository",
      input: answer([101, 102], {
        nodes: [
          { position: 1, pullRequest: member(101, { url: url(101, "other/app") }) },
          { position: 2, pullRequest: member(102) },
        ],
      }),
      gaps: ["member-link"],
    },
  ])("keeps only what it read when there's $cause", ({ input, gaps }) => {
    const read = readNativeStack(input, SELF);
    expect(read.kind).toBe("stacked-partial");
    if (read.kind !== "stacked-partial") return;
    expect([...read.gaps].sort()).toEqual([...gaps].sort());
    expect(read.key).toBe(nativeStackKey("github.com", "PRS_1"));
    // Every entry kept was read with its PR in this repository.
    for (const entry of read.entries) {
      expect(entry.ref.repository).toBe("acme/app");
      expect(entry.member.number).toBe(entry.ref.number);
    }
  });

  it("keeps no entry whose position repeats or falls outside the stack", () => {
    const read = readNativeStack(
      answer([100, 101, 102], {
        nodes: [
          { position: 1, pullRequest: member(100) },
          { position: 2, pullRequest: member(101) },
          { position: 2, pullRequest: member(103) },
          { position: 3, pullRequest: member(102) },
          { position: 9, pullRequest: member(104) },
        ],
      }),
      SELF,
    );
    if (read.kind !== "stacked-partial") throw new Error(read.kind);
    expect(read.gaps).toContain("positions");
    expect(read.entries.map((entry) => [entry.position, entry.ref.number])).toEqual([
      [1, 100],
      [3, 102],
    ]);
  });

  it.each([
    {
      case: "this PR listed twice",
      nodes: [
        { position: 1, pullRequest: member(100) },
        { position: 2, pullRequest: member(102) },
        { position: 3, pullRequest: member(102) },
      ],
      kept: [[1, 100]],
    },
    {
      case: "another member listed twice",
      nodes: [
        { position: 1, pullRequest: member(100) },
        { position: 2, pullRequest: member(100) },
        { position: 3, pullRequest: member(102) },
      ],
      kept: [[3, 102]],
    },
    {
      case: "the same PR under a differently cased link",
      nodes: [
        { position: 1, pullRequest: member(100) },
        {
          position: 2,
          pullRequest: member(100, { url: "https://GitHub.com/ACME/App/pull/100" }),
        },
        { position: 3, pullRequest: member(102) },
      ],
      kept: [[3, 102]],
    },
  ])("trusts no place for $case", ({ nodes, kept }) => {
    const read = readNativeStack(
      { ...answer([100, 101, 102], { nodes }), stackEntry: { position: 3 } },
      SELF,
    );
    if (read.kind !== "stacked-partial") throw new Error(read.kind);
    expect(read.gaps).toContain("duplicate-member");
    expect(read.entries.map((entry) => [entry.position, entry.ref.number])).toEqual(kept);
  });

  it("finds this PR by its own entry when a lower member is invisible", () => {
    const read = readNativeStack(
      answer([100, 101, 102], {
        nodes: [
          { position: 1, pullRequest: null },
          { position: 2, pullRequest: member(101) },
          { position: 3, pullRequest: member(102) },
        ],
      }),
      SELF,
    );
    if (read.kind !== "stacked-partial") throw new Error(read.kind);
    expect(read.position).toBe(3);
    expect(read.size).toBe(3);
    expect(read.entries.map((entry) => [entry.position, entry.ref.number])).toEqual([
      [2, 101],
      [3, 102],
    ]);
  });

  it("knows this PR is stacked when GitHub names its position but not its stack", () => {
    const read = readNativeStack({ stack: null, stackEntry: { position: 2 } }, SELF);
    expect(read).toMatchObject({
      kind: "stacked-partial",
      key: null,
      size: null,
      position: 2,
      entries: [],
    });
  });

  it("tells stacks with the same number in other repositories and on other hosts apart", () => {
    const here = readNativeStack(answer([101, 102], { id: "PRS_a" }), SELF);
    const elsewhere = readNativeStack(answer([101, 102], { id: "PRS_b" }), {
      ...SELF,
      repository: "acme/web",
    });
    expect(here.kind === "stacked" && here.key).toBe("github.com\nPRS_a");
    expect(nativeStackKey("github.com", "PRS_a")).not.toBe(
      nativeStackKey("ghes.example.com", "PRS_a"),
    );
    expect(elsewhere.kind).toBe("stacked-partial");
  });
});

describe("nativeStackFingerprint", () => {
  const base = answer([100, 101, 102]);

  it("changes when the stack is retargeted though its members and order are the same", () => {
    expect(nativeStackFingerprint(answer([100, 101, 102], { baseRefName: "release" }))).not.toBe(
      nativeStackFingerprint(base),
    );
  });

  it("changes when this PR moves outside the entries read", () => {
    const window = (position: number): NativeStackAnswer => ({
      ...answer([100, 101], { size: 60, hasNextPage: true }),
      stackEntry: { position },
    });
    expect(nativeStackFingerprint(window(55))).not.toBe(nativeStackFingerprint(window(56)));
  });

  it("changes on a reorder, a merge, a draft and an added member", () => {
    const fingerprints = [
      base,
      answer([101, 100, 102]),
      answer([100, 101, 102], {
        nodes: [
          { position: 1, pullRequest: member(100, { state: "MERGED" }) },
          { position: 2, pullRequest: member(101) },
          { position: 3, pullRequest: member(102) },
        ],
      }),
      answer([100, 101, 102], {
        nodes: [
          { position: 1, pullRequest: member(100) },
          { position: 2, pullRequest: member(101, { isDraft: true }) },
          { position: 3, pullRequest: member(102) },
        ],
      }),
      answer([100, 101, 102, 103]),
    ].map(nativeStackFingerprint);
    expect(new Set(fingerprints).size).toBe(fingerprints.length);
  });

  it("changes when a read becomes complete, or a rejected link becomes valid", () => {
    const more = answer([100, 101, 102], { hasNextPage: true });
    expect(readNativeStack(more, SELF).kind).toBe("stacked-partial");
    expect(readNativeStack(base, SELF).kind).toBe("stacked");
    expect(nativeStackFingerprint(more)).not.toBe(nativeStackFingerprint(base));

    const elsewhere = answer([100, 101, 102], {
      nodes: [
        { position: 1, pullRequest: member(100, { url: url(100, "other/app") }) },
        { position: 2, pullRequest: member(101) },
        { position: 3, pullRequest: member(102) },
      ],
    });
    expect(readNativeStack(elsewhere, SELF).kind).toBe("stacked-partial");
    expect(nativeStackFingerprint(elsewhere)).not.toBe(nativeStackFingerprint(base));
  });

  it("doesn't depend on the order GitHub lists entries in", () => {
    const shuffled = answer([100, 101, 102], {
      nodes: [
        { position: 3, pullRequest: member(102) },
        { position: 1, pullRequest: member(100) },
        { position: 2, pullRequest: member(101) },
      ],
    });
    expect(nativeStackFingerprint(shuffled)).toBe(nativeStackFingerprint(base));
  });

  it("is the same for the same answer, and tells unsupported from not stacked", () => {
    expect(nativeStackFingerprint(answer([100, 101, 102]))).toBe(nativeStackFingerprint(base));
    expect(nativeStackFingerprint(undefined)).not.toBe(
      nativeStackFingerprint({ stack: null, stackEntry: null }),
    );
  });
});

describe("partialNativeStack", () => {
  const SELF_REF = { ...SELF, url: url(SELF.number) };
  const partial = (input: NativeStackAnswer) => {
    const read = readNativeStack(input, SELF);
    if (read.kind !== "stacked-partial") throw new Error(read.kind);
    return partialNativeStack(read, SELF_REF, "stack/102");
  };
  const numbers = (result: ReturnType<typeof partial>) =>
    result.parse.nodes.map((node) => node.ref?.number);
  /** Entries at `positions` of a stack of `size`, this PR at `self`, numbered 47 + position. */
  const window = (positions: number[], selfAt: number, size = 60): NativeStackAnswer => ({
    stackEntry: { position: selfAt },
    stack: {
      id: "PRS_1",
      number: 7,
      size,
      baseRefName: "main",
      entries: {
        hasNextPage: true,
        nodes: positions.map((position) => ({
          position,
          pullRequest: member(position === selfAt ? SELF.number : 47 + position),
        })),
      },
    },
  });

  it("keeps GitHub's own position and size for a window in the middle of a stack", () => {
    const result = partial(window([54, 55, 56], 55));
    expect(numbers(result)).toEqual([101, 102, 103]);
    expect(result).toMatchObject({ position: 55, size: 60, below: "read", above: "read" });
    expect(result.rowNumbers).toEqual([54, 55, 56]);
    expect(result.parse.selfIndex).toBe(1);
  });

  it("tells a neighbor it didn't read from the end of the stack", () => {
    const unreadBelow = partial(window([55, 56], 55));
    expect(numbers(unreadBelow)).toEqual([102, 103]);
    expect(unreadBelow).toMatchObject({ below: "unread", above: "read", rowNumbers: [55, 56] });
    const atBase = partial(window([1, 2], 1));
    expect(atBase).toMatchObject({ below: "end", above: "read" });
    const atTop = partial(window([59, 60], 60));
    expect(atTop).toMatchObject({ below: "read", above: "end" });
  });

  it.each([
    {
      case: "an entry elsewhere contradicts its position",
      input: answer([102, 101, 103], {
        size: 3,
        nodes: [
          { position: 1, pullRequest: member(102) },
          { position: 3, pullRequest: member(103) },
        ],
      }),
      stackEntry: { position: 2 },
    },
    { case: "it names no position", input: window([54, 55, 56], 55), stackEntry: null },
    {
      case: "its position is past the stack",
      input: window([1, 2], 2, 2),
      stackEntry: { position: 3 },
    },
    { case: "its position is zero", input: window([1, 2], 1, 2), stackEntry: { position: 0 } },
    {
      case: "it's listed twice",
      input: answer([101, 102, 103], {
        nodes: [
          { position: 1, pullRequest: member(101) },
          { position: 2, pullRequest: member(102) },
          { position: 3, pullRequest: member(102) },
        ],
      }),
      stackEntry: { position: 2 },
    },
    {
      case: "another PR is at its position",
      input: answer([101, 102, 103], {
        nodes: [
          { position: 1, pullRequest: member(101) },
          { position: 2, pullRequest: member(104) },
          { position: 3, pullRequest: member(103) },
        ],
      }),
      stackEntry: { position: 2 },
    },
    {
      case: "an invisible PR is at its position",
      input: answer([101, 102, 103], {
        nodes: [
          { position: 1, pullRequest: member(101) },
          { position: 2, pullRequest: null },
          { position: 3, pullRequest: member(103) },
        ],
      }),
      stackEntry: { position: 2 },
    },
  ])("offers no neighbor, and never this PR as its own, when $case", ({ input, stackEntry }) => {
    const result = partial({ ...input, stackEntry });
    expect(numbers(result)).toEqual([102]);
    expect(result).toMatchObject({
      position: null,
      below: "unread",
      above: "unread",
      rowNumbers: [null],
    });
  });

  it("keeps a position GitHub named without its stack, with no size", () => {
    const result = partial({ stack: null, stackEntry: { position: 2 } });
    expect(numbers(result)).toEqual([102]);
    expect(result).toMatchObject({ position: 2, size: null, below: "unread", above: "unread" });
  });
});
