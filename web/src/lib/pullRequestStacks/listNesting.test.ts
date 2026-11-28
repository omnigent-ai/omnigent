import { describe, expect, it } from "vitest";

import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";
import {
  type ListStackGroup,
  type ListStackRead,
  nestStackGroups,
  withoutCollapsedStacks,
} from "@/lib/pullRequestStacks/listNesting";
import { type NativeStackAnswer, readNativeStack } from "@/lib/pullRequestStacks/nativeStack";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import { stackPullRequestRef } from "@/lib/pullRequestStacks/stackModel";

// Stack descriptions in an example format, which the page reads once a build registers it.
const STACK_FORMATS = [EXAMPLE_STACK_FORMAT];
withPullRequestIntegrations({ stackFormats: STACK_FORMATS });

const url = (number: number, repository = "acme/app") =>
  `https://github.com/${repository}/pull/${number}`;

/** An entry of a stack section: a PR number, or an unpushed branch, at an indent level. */
type Entry = [number | string, number] | [number, number, string];

/** `self`'s description, listing `entries` as a stack tool writes them. */
function body(
  self: number,
  entries: Entry[],
  { repository = "acme/app", footer = "---------" } = {},
) {
  const lines = entries.map(([entry, level, entryRepository]) => {
    const indent = "  ".repeat(level);
    if (typeof entry === "string") return `${indent}- ${entry}`;
    const link = url(entry, entryRepository ?? repository);
    return entry === self && !entryRepository
      ? `${indent}- [**stack/${entry}**](${link}) ← _this PR_`
      : `${indent}- [stack/${entry}](${link})`;
  });
  return ["## Stacked changes", ...lines, "", footer, "", "The change.", ""].join("\n");
}

/** Reads from descriptions by row URL; a URL with no entry wasn't read. */
function reads(bodies: Record<string, string | null>) {
  return (rowUrl: string): ListStackRead => {
    if (!(rowUrl in bodies)) return { kind: "unread" };
    const text = bodies[rowUrl];
    const self = stackPullRequestRef(rowUrl)!;
    return { kind: "read", parse: text === null ? null : parseStack(STACK_FORMATS, text, self) };
  };
}

const group = (urls: string[], id = "open", title = "Open"): ListStackGroup => ({
  id,
  title,
  urls,
});

const shape = (rows: ReturnType<typeof nestStackGroups>[number]) =>
  rows.map((row) => [Number(row.url.split("/").pop()), row.depth]);

describe("nestStackGroups", () => {
  it("nests a child under its parent when both descriptions agree on the edge", () => {
    const stack: Entry[] = [
      [100, 0],
      [101, 1],
      [102, 2],
    ];
    const [rows] = nestStackGroups(
      [group([url(102), url(7), url(100), url(101)])],
      reads({
        [url(100)]: body(100, stack),
        [url(101)]: body(101, stack),
        [url(102)]: body(102, stack),
      }),
    );
    expect(shape(rows!)).toEqual([
      [7, 0],
      [100, 0],
      [101, 1],
      [102, 2],
    ]);
    expect(rows![1]).toMatchObject({ descendants: 2, parentUrl: null, hint: null });
    expect(rows![3]).toMatchObject({ parentUrl: url(101), connector: "elbow" });
  });

  it("doesn't nest a row under a parent whose description puts it somewhere else", () => {
    // #100 lists 100 → 101 → 102, but #102 says it sits right on #100; #101 is filtered out.
    const [rows] = nestStackGroups(
      [group([url(100), url(102)])],
      reads({
        [url(100)]: body(100, [
          [100, 0],
          [101, 1],
          [102, 2],
        ]),
        [url(102)]: body(102, [
          [100, 0],
          [102, 1],
        ]),
      }),
    );
    expect(shape(rows!)).toEqual([
      [100, 0],
      [102, 0],
    ]);
    expect(rows![1]!.hint).toMatchObject({ kind: "conflict", parent: { number: 100 } });
  });

  it("never skips an unpushed branch between a row and the PR below it", () => {
    const stack: Entry[] = [
      [100, 0],
      ["stack/unpushed", 1],
      [102, 2],
    ];
    const [rows] = nestStackGroups(
      [group([url(100), url(102)])],
      reads({ [url(100)]: body(100, stack), [url(102)]: body(102, stack) }),
    );
    expect(shape(rows!)).toEqual([
      [100, 0],
      [102, 0],
    ]);
    expect(rows![1]!.hint).toEqual({ kind: "unpushed", branch: "stack/unpushed" });
  });

  it("names why each stacked row isn't nested", () => {
    const onto = (parent: number, child: number): Entry[] => [
      [parent, 0],
      [child, 1],
    ];
    const groups = [
      group([url(201), url(301), url(401), url(500), url(501)]),
      group([url(200)], "merged", "Merged"),
    ];
    const [open] = nestStackGroups(
      groups,
      reads({
        [url(200)]: body(200, onto(200, 201)),
        [url(201)]: body(201, onto(200, 201)),
        // #300 isn't a row here.
        [url(301)]: body(301, onto(300, 301)),
        // #400 lives in another repository.
        [url(401)]: body(401, [
          [400, 0, "other/lib"],
          [401, 1],
        ]),
        // #500 is a row, but its description wasn't read.
        [url(501)]: body(501, onto(500, 501)),
      }),
    );
    const hints = Object.fromEntries(
      open!.map((row) => [Number(row.url.split("/").pop()), row.hint?.kind ?? null]),
    );
    expect(hints).toEqual({
      201: "other-group",
      301: "absent",
      401: "external",
      500: null,
      501: "unconfirmed",
    });
    expect(open![0]!.hint).toMatchObject({ groupTitle: "Merged" });
  });

  it("says the stack info differs when the parent's description lists no stack", () => {
    const [rows] = nestStackGroups(
      [group([url(100), url(101)])],
      reads({
        [url(100)]: null,
        [url(101)]: body(101, [
          [100, 0],
          [101, 1],
        ]),
      }),
    );
    expect(rows![1]!.hint?.kind).toBe("conflict");
  });

  it("never nests PRs from two repositories that use the same branch names", () => {
    // Both forks push stack/100 → stack/101; each #101 names its own repository's #100.
    const stack = (repository: string): Entry[] => [
      [100, 0, repository],
      [101, 1, repository],
    ];
    const other = "fork/app";
    const [rows] = nestStackGroups(
      [group([url(100), url(101, other)])],
      reads({
        [url(100)]: body(100, [
          [100, 0],
          [101, 1],
        ]),
        [url(101, other)]: body(
          101,
          stack(other).map(([n, l]) => [n, l] as Entry),
          {
            repository: other,
          },
        ),
      }),
    );
    expect(shape(rows!)).toEqual([
      [100, 0],
      [101, 0],
    ]);
    expect(rows![1]!.hint).toMatchObject({ kind: "absent", parent: { repository: other } });
  });

  it("orders siblings as their parent's description lists them", () => {
    const stack: Entry[] = [
      [100, 0],
      [102, 1],
      [101, 1],
    ];
    const [rows] = nestStackGroups(
      [group([url(101), url(100), url(102)])],
      reads({
        [url(100)]: body(100, stack),
        [url(101)]: body(101, stack),
        [url(102)]: body(102, stack),
      }),
    );
    expect(shape(rows!)).toEqual([
      [100, 0],
      [102, 1],
      [101, 1],
    ]);
    expect(rows!.map((row) => row.connector)).toEqual([null, "tee", "elbow"]);
  });

  it("never joins two rows that each name the other as their parent", () => {
    // Each lists the other first as its parent, so neither lists the other directly below it.
    const [rows] = nestStackGroups(
      [group([url(101), url(100)])],
      reads({
        [url(100)]: body(100, [
          [101, 0],
          [100, 1],
        ]),
        [url(101)]: body(101, [
          [100, 0],
          [101, 1],
        ]),
      }),
    );
    expect(rows!.every((row) => row.depth === 0)).toBe(true);
  });

  it("breaks a loop three descriptions agree on at the row shown first", () => {
    // #100 sits on #102, #101 on #100 and #102 on #101, and each lists its child below it.
    const [rows] = nestStackGroups(
      [group([url(100), url(101), url(102)])],
      reads({
        [url(100)]: body(100, [
          [102, 0],
          [100, 1],
          [101, 2],
        ]),
        [url(101)]: body(101, [
          [100, 0],
          [101, 1],
          [102, 2],
        ]),
        [url(102)]: body(102, [
          [101, 0],
          [102, 1],
          [100, 2],
        ]),
      }),
    );
    expect(shape(rows!)).toEqual([
      [100, 0],
      [101, 1],
      [102, 2],
    ]);
    expect(rows![0]!.hint).toMatchObject({ kind: "conflict", parent: { number: 102 } });
  });

  it("indents deep stacks four levels and names the level past them", () => {
    const stack: Entry[] = [100, 101, 102, 103, 104, 105].map((number, level) => [number, level]);
    const bodies = Object.fromEntries(
      stack.map(([number]) => [url(number as number), body(number as number, stack)]),
    );
    const [rows] = nestStackGroups(
      [group(stack.map(([number]) => url(number as number)))],
      reads(bodies),
    );
    expect(rows!.map((row) => row.deepLevel)).toEqual([null, null, null, null, null, 6]);
    expect(rows!.at(-1)!.guides).toHaveLength(3);
  });

  it("notes stack info that may be out of date on a nested row and on its parent", () => {
    const stack: Entry[] = [
      [100, 0],
      [101, 1],
    ];
    const [rows] = nestStackGroups(
      [group([url(100), url(101), url(7)])],
      reads({
        [url(100)]: body(100, stack, { footer: "---" }),
        [url(101)]: body(101, stack, { footer: "---" }),
        [url(7)]: body(7, [[7, 0]], { footer: "---" }),
      }),
    );
    expect(rows!.map((row) => [row.depth, row.tolerated])).toEqual([
      [0, true],
      [1, true],
      [0, false],
    ]);
  });
});

describe("withoutCollapsedStacks", () => {
  it("folds a collapsed row's whole stack away", () => {
    const stack: Entry[] = [
      [100, 0],
      [101, 1],
      [102, 2],
    ];
    const [rows] = nestStackGroups(
      [group([url(100), url(101), url(102), url(7)])],
      reads({
        [url(100)]: body(100, stack),
        [url(101)]: body(101, stack),
        [url(102)]: body(102, stack),
      }),
    );
    expect(shape(withoutCollapsedStacks(rows!, (rowUrl) => rowUrl === url(101)))).toEqual([
      [100, 0],
      [101, 1],
      [7, 0],
    ]);
    expect(shape(withoutCollapsedStacks(rows!, (rowUrl) => rowUrl === url(100)))).toEqual([
      [100, 0],
      [7, 0],
    ]);
  });
});

describe("nestStackGroups with GitHub's own stacks", () => {
  /** A GitHub stack: its PR numbers base first, with `listed` positions read with their PR. */
  interface NativeSpec {
    numbers: number[];
    id?: string;
    repository?: string;
    host?: string;
    size?: number;
    listed?: number[];
    /** Each row's read also lists its own entry, as the list's read adds it past the first ones. */
    withOwn?: boolean;
  }
  const prUrl = (
    number: number,
    { repository = "acme/app", host = "github.com" }: Partial<NativeSpec> = {},
  ) => `https://${host}/${repository}/pull/${number}`;
  /** GitHub's answer for `self` in `spec`, or for a self it names no position for. */
  function answerFor(spec: NativeSpec, self: number, position?: number | null): NativeStackAnswer {
    const { numbers, id = "PRS_1", size = numbers.length } = spec;
    const own = numbers.indexOf(self) + 1;
    const firstListed = spec.listed ?? numbers.map((_, index) => index + 1);
    const listed =
      spec.withOwn && !firstListed.includes(own)
        ? [...firstListed, own].sort((left, right) => left - right)
        : firstListed;
    return {
      stackEntry: position === null ? null : { position: position ?? own },
      stack: {
        id,
        number: 7,
        size,
        baseRefName: "main",
        entries: {
          hasNextPage: listed.length < size,
          nodes: listed.map((at) => ({
            position: at,
            pullRequest: {
              url: prUrl(numbers[at - 1]!, spec),
              number: numbers[at - 1]!,
              state: "OPEN",
              isDraft: false,
              headRefName: `stack/${numbers[at - 1]}`,
            },
          })),
        },
      },
    };
  }
  /** Reads rows from GitHub's stacks first, then from `bodies`, as the list does. */
  function nativeReads(
    stacks: { spec: NativeSpec; rows: number[]; position?: number | null }[],
    bodies: Record<string, string | null> = {},
  ) {
    const described = reads(bodies);
    return (rowUrl: string): ListStackRead => {
      for (const { spec, rows, position } of stacks) {
        const number = Number(rowUrl.split("/").pop());
        if (!rows.includes(number) || prUrl(number, spec) !== rowUrl) continue;
        const read = readNativeStack(
          answerFor(spec, number, position),
          stackPullRequestRef(rowUrl)!,
        );
        if (read.kind === "stacked" || read.kind === "stacked-partial") {
          return { kind: "native", stack: read };
        }
      }
      return described(rowUrl);
    };
  }
  const hintOf = (rows: ReturnType<typeof nestStackGroups>[number], number: number) =>
    rows.find((row) => row.url.endsWith(`/${number}`))?.hint ?? null;

  it("nests consecutive rows by GitHub's positions, whatever order the list shows them in", () => {
    const spec = { numbers: [100, 101, 102] };
    const [rows] = nestStackGroups(
      [group([url(102), url(7), url(100), url(101)])],
      nativeReads([{ spec, rows: [100, 101, 102] }]),
    );
    // Roots keep the list's order; each stack follows its base.
    expect(shape(rows!)).toEqual([
      [7, 0],
      [100, 0],
      [101, 1],
      [102, 2],
    ]);
  });

  it("names the PR below where it isn't a row here, and nests the rest under each other", () => {
    const spec = { numbers: [100, 101, 102] };
    const [rows] = nestStackGroups(
      [group([url(101), url(102)])],
      nativeReads([{ spec, rows: [101, 102] }]),
    );
    expect(shape(rows!)).toEqual([
      [101, 0],
      [102, 1],
    ]);
    expect(hintOf(rows!, 101)).toMatchObject({ kind: "absent", parent: { number: 100 } });
  });

  it("says where a row sits when GitHub's read didn't show the PR below it", () => {
    const spec = { numbers: [98, 99, 102, 103, 104], listed: [3, 4] };
    const [rows] = nestStackGroups(
      [group([url(102), url(103)])],
      nativeReads([{ spec, rows: [102, 103] }]),
    );
    expect(shape(rows!)).toEqual([
      [102, 0],
      [103, 1],
    ]);
    expect(hintOf(rows!, 102)).toEqual({ kind: "native-gap", position: 3, size: 5 });
  });

  it("nests a long stack's rows past the entries each read lists, by the rows' own positions", () => {
    // 47 PRs, the first 20 merged and gone from the list; each read lists entries 1-20 and its own.
    const numbers = Array.from({ length: 47 }, (_, index) => 1001 + index);
    const spec = {
      numbers,
      listed: numbers.slice(0, 20).map((_, index) => index + 1),
      withOwn: true,
    };
    const open = numbers.slice(20);
    const [rows] = nestStackGroups(
      [group(open.map((number) => url(number)))],
      nativeReads([{ spec, rows: open }]),
    );
    expect(shape(rows!)).toEqual(open.map((number, index) => [number, index]));
    expect(hintOf(rows!, 1021)).toMatchObject({ kind: "absent", parent: { number: 1020 } });
    expect(open.slice(1).map((number) => hintOf(rows!, number))).toEqual(
      open.slice(1).map(() => null),
    );
  });

  it("still says where a long stack's row sits when the row below it isn't listed here", () => {
    const numbers = Array.from({ length: 30 }, (_, index) => 1001 + index);
    const spec = {
      numbers,
      listed: numbers.slice(0, 20).map((_, index) => index + 1),
      withOwn: true,
    };
    // Position 25 is filtered out, so position 26 can't be nested.
    const shown = [1024, 1026];
    const [rows] = nestStackGroups(
      [group(shown.map((number) => url(number)))],
      nativeReads([{ spec, rows: shown }]),
    );
    expect(shape(rows!)).toEqual([
      [1024, 0],
      [1026, 0],
    ]);
    expect(hintOf(rows!, 1026)).toEqual({ kind: "native-gap", position: 26, size: 30 });
  });

  it("doesn't nest a long stack's row under a position two different PRs claim", () => {
    const numbers = Array.from({ length: 30 }, (_, index) => 1001 + index);
    const listed = numbers.slice(0, 20).map((_, index) => index + 1);
    const spec = { numbers, listed, withOwn: true };
    // Another read of the same stack puts #2024 at position 24, where #1024 says it is.
    const other = {
      numbers: numbers.map((number) => (number === 1024 ? 2024 : number)),
      listed,
      withOwn: true,
    };
    const [rows] = nestStackGroups(
      [group([url(2024), url(1024), url(1025)])],
      nativeReads([
        { spec: other, rows: [2024] },
        { spec, rows: [1024, 1025] },
      ]),
    );
    expect(rows!.find((row) => row.url === url(1025))).toMatchObject({ depth: 0, parentUrl: null });
    expect(hintOf(rows!, 1025)).toEqual({ kind: "native-gap", position: 25, size: 30 });
  });

  it("nests a very long chain in time linear in its rows", () => {
    const count = 4000;
    const numbers = Array.from({ length: count + 20 }, (_, index) => 10_001 + index);
    const spec = {
      numbers,
      listed: numbers.slice(0, 20).map((_, index) => index + 1),
      withOwn: true,
    };
    const open = numbers.slice(20);
    const read = nativeReads([{ spec, rows: open }]);
    const readByUrl = new Map(open.map((number) => [url(number), read(url(number))]));
    const started = performance.now();
    const [rows] = nestStackGroups([group(open.map((number) => url(number)))], (rowUrl) =>
      readByUrl.get(rowUrl)!,
    );
    const elapsed = performance.now() - started;
    expect(rows!.at(-1)!.depth).toBe(count - 1);
    // Quadratic work over 4,000 rows takes seconds; linear work takes a few milliseconds.
    expect(elapsed).toBeLessThan(500);
  });

  it("names the group a long stack's row below is in, rather than nesting across groups", () => {
    const numbers = Array.from({ length: 30 }, (_, index) => 1001 + index);
    const spec = {
      numbers,
      listed: numbers.slice(0, 20).map((_, index) => index + 1),
      withOwn: true,
    };
    const nested = nestStackGroups(
      [group([url(1024)], "mine", "Mine"), group([url(1025)], "theirs", "Theirs")],
      nativeReads([{ spec, rows: [1024, 1025] }]),
    );
    expect(hintOf(nested[1]!, 1025)).toMatchObject({
      kind: "other-group",
      parent: { number: 1024 },
      groupTitle: "Mine",
    });
  });

  it("says a row is in a stack where GitHub names no position for it", () => {
    const spec = { numbers: [100, 101] };
    const [rows] = nestStackGroups(
      [group([url(101)])],
      nativeReads([{ spec, rows: [101], position: null }]),
    );
    expect(hintOf(rows!, 101)).toEqual({ kind: "native-gap", position: null, size: 2 });
  });

  it("names the group the PR below is in, rather than nesting across groups", () => {
    const spec = { numbers: [100, 101] };
    const nested = nestStackGroups(
      [group([url(100)], "mine", "Mine"), group([url(101)], "theirs", "Theirs")],
      nativeReads([{ spec, rows: [100, 101] }]),
    );
    expect(hintOf(nested[1]!, 101)).toMatchObject({
      kind: "other-group",
      parent: { number: 100 },
      groupTitle: "Mine",
    });
  });

  it("keeps stacks with the same number in other repositories and on other hosts apart", () => {
    const app = { numbers: [1, 2] };
    const web = { numbers: [1, 2], repository: "acme/web" };
    const enterprise = { numbers: [1, 2], host: "ghes.example.com" };
    const rows = [app, web, enterprise].flatMap((spec) => [prUrl(1, spec), prUrl(2, spec)]);
    const [nested] = nestStackGroups(
      [group(rows)],
      nativeReads([
        { spec: app, rows: [1, 2] },
        { spec: web, rows: [1, 2] },
        { spec: enterprise, rows: [1, 2] },
      ]),
    );
    expect(nested!.map((row) => [row.url, row.depth, row.parentUrl])).toEqual([
      [prUrl(1, app), 0, null],
      [prUrl(2, app), 1, prUrl(1, app)],
      [prUrl(1, web), 0, null],
      [prUrl(2, web), 1, prUrl(1, web)],
      [prUrl(1, enterprise), 0, null],
      [prUrl(2, enterprise), 1, prUrl(1, enterprise)],
    ]);
  });

  it("nests two stacks in one repository apart, and never across their keys", () => {
    const first = { numbers: [1, 2], id: "PRS_a" };
    const second = { numbers: [3, 4], id: "PRS_b" };
    const [rows] = nestStackGroups(
      [group([url(1), url(2), url(3), url(4)])],
      nativeReads([
        { spec: first, rows: [1, 2] },
        { spec: second, rows: [3, 4] },
      ]),
    );
    expect(shape(rows!)).toEqual([
      [1, 0],
      [2, 1],
      [3, 0],
      [4, 1],
    ]);
    // A row GitHub stacks on #1, where #1's own read puts it at the base of another stack.
    const [conflicted] = nestStackGroups(
      [group([url(1), url(2)])],
      nativeReads([
        { spec: { numbers: [1], id: "PRS_c" }, rows: [1] },
        { spec: first, rows: [2] },
      ]),
    );
    expect(hintOf(conflicted!, 2)).toMatchObject({
      kind: "conflict",
      parent: { number: 1 },
      source: "github",
    });
  });

  it("follows GitHub's stack over a row's description that says otherwise", () => {
    const spec = { numbers: [100, 101, 102] };
    const described: Entry[] = [
      [99, 0],
      [102, 1],
    ];
    const [rows] = nestStackGroups(
      [group([url(99), url(100), url(101), url(102)])],
      nativeReads([{ spec, rows: [100, 101, 102] }], {
        [url(99)]: body(99, described),
        [url(102)]: body(102, described),
      }),
    );
    expect(shape(rows!)).toEqual([
      [99, 0],
      [100, 0],
      [101, 1],
      [102, 2],
    ]);
  });

  it("nests a row GitHub stacks only where the parent row's own read of GitHub confirms it", () => {
    const spec = { numbers: [100, 101] };
    const childOnly = nativeReads([{ spec, rows: [101] }], { [url(100)]: "Adds the API." });
    const place = (readOf: (rowUrl: string) => ListStackRead) => {
      const [rows] = nestStackGroups([group([url(100), url(101)])], readOf);
      return { depth: rows!.find((row) => row.url === url(101))!.depth, hint: hintOf(rows!, 101) };
    };
    // The parent's description lists no stack, and GitHub's read of it can't say.
    expect(place(childOnly)).toEqual({
      depth: 0,
      hint: {
        kind: "unconfirmed",
        parent: expect.objectContaining({ number: 100 }),
        source: "github",
      },
    });
    // The parent wasn't read at all.
    expect(place(nativeReads([{ spec, rows: [101] }])).hint).toMatchObject({
      kind: "unconfirmed",
      source: "github",
    });
    // GitHub's own read of the parent says it isn't in a stack.
    const unstacked = (rowUrl: string): ListStackRead =>
      rowUrl === url(100)
        ? { kind: "read", parse: null, githubUnstacked: true }
        : childOnly(rowUrl);
    expect(place(unstacked)).toEqual({
      depth: 0,
      hint: {
        kind: "conflict",
        parent: expect.objectContaining({ number: 100 }),
        source: "github",
      },
    });
  });

  it("never nests on two reads of the same stack that disagree about what's between them", () => {
    // The child's read: #1 then #2. The parent's own read: #1, then #3, then #2.
    const childRead = { numbers: [1, 2] };
    const parentRead = { numbers: [1, 3, 2] };
    const [rows] = nestStackGroups(
      [group([url(1), url(2)])],
      nativeReads([
        { spec: parentRead, rows: [1] },
        { spec: childRead, rows: [2] },
      ]),
    );
    expect(shape(rows!)).toEqual([
      [1, 0],
      [2, 0],
    ]);
    expect(hintOf(rows!, 2)).toMatchObject({
      kind: "conflict",
      parent: { number: 1 },
      source: "github",
    });
  });

  it("never nests where the parent's partial read puts the row at another position", () => {
    const childRead = { numbers: [1, 2] };
    const place = (parentRead: NativeSpec) => {
      const [rows] = nestStackGroups(
        [group([url(1), url(2)])],
        nativeReads([
          { spec: parentRead, rows: [1] },
          { spec: childRead, rows: [2] },
        ]),
      );
      return { shape: shape(rows!), hint: hintOf(rows!, 2) };
    };
    // The parent's read can't see position 2, but lists #2 at position 3.
    expect(place({ numbers: [1, 3, 2], listed: [1, 3] })).toEqual({
      shape: [
        [1, 0],
        [2, 0],
      ],
      hint: { kind: "conflict", parent: expect.objectContaining({ number: 1 }), source: "github" },
    });
    // The parent's read shows only itself: nothing contradicts the child's own read.
    expect(place({ numbers: [1, 2], listed: [1] })).toEqual({
      shape: [
        [1, 0],
        [2, 1],
      ],
      hint: null,
    });
  });

  it("nests a description's PR on GitHub's stack only where GitHub lists it just above", () => {
    const spec = { numbers: [100, 101, 103] };
    const onTop: Entry[] = [
      [101, 0],
      [103, 1],
    ];
    const [agreed] = nestStackGroups(
      [group([url(101), url(103)])],
      nativeReads([{ spec, rows: [101] }], { [url(103)]: body(103, onTop) }),
    );
    expect(shape(agreed!)).toEqual([
      [101, 0],
      [103, 1],
    ]);
    const elsewhere: Entry[] = [
      [101, 0],
      [104, 1],
    ];
    const [disagreed] = nestStackGroups(
      [group([url(101), url(104)])],
      nativeReads([{ spec, rows: [101] }], { [url(104)]: body(104, elsewhere) }),
    );
    expect(hintOf(disagreed!, 104)).toMatchObject({
      kind: "conflict",
      parent: { number: 101 },
      source: "mixed",
    });
  });
});
