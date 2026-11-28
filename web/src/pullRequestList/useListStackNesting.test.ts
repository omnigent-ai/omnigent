import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PullRequest } from "@/newPullRequestPage/hooks/usePullRequests";
import type { NativeStackAnswer } from "@/lib/pullRequestStacks/nativeStack";
import type { PullRequestListEntry } from "@/shell/PullRequestListRows";

import {
  STACK_SETTLE_LIMIT_MS,
  rowStackBody,
  type StackNestingGroup,
  useListStackNesting,
} from "./useListStackNesting";
import { withPullRequestIntegrations, EXAMPLE_STACK_FORMAT } from "@/test/pullRequestIntegrations";

// Stack descriptions in an example format, which the page reads once a build registers it.
withPullRequestIntegrations({ stackFormats: [EXAMPLE_STACK_FORMAT] });

const url = (number: number) => `https://github.com/acme/app/pull/${number}`;

function body(self: number, stack: number[]) {
  const lines = stack.map((number, level) =>
    number === self
      ? `${"  ".repeat(level)}- [**stack/${number}**](${url(number)}) ← _this PR_`
      : `${"  ".repeat(level)}- [stack/${number}](${url(number)})`,
  );
  return ["## Stacked changes", ...lines, "", "---------", "", "The change.", ""].join("\n");
}

type Read = { body: string | null } | { status: "loading" } | { status: "error" };

/** A row's entry as the list's reads leave it. */
function entry(rowUrl: string, read: Read): PullRequestListEntry {
  const pr =
    "body" in read ? ({ url: rowUrl, body: read.body } as unknown as PullRequest) : undefined;
  return {
    association: { url: rowUrl } as PullRequestListEntry["association"],
    row: { url: rowUrl } as PullRequestListEntry["row"],
    pr,
    offline: false,
    status: "body" in read ? "ready" : read.status,
    error: undefined,
    mergeHead: undefined,
    mergeHint: undefined,
    mergeUnavailable: true,
  };
}

const STACK = [100, 101];
const group = (numbers: number[]): StackNestingGroup[] => [
  { id: "open", title: "Open", rows: numbers.map((number) => ({ url: url(number) })) },
];

function render(initial: {
  enabled?: boolean;
  scope: string;
  groups: StackNestingGroup[];
  reads: Record<number, Read>;
  nativeStackOf?: (url: string) => NativeStackAnswer | undefined;
}) {
  const props = (input: typeof initial) => ({
    enabled: input.enabled ?? true,
    scope: input.scope,
    groups: input.groups,
    nativeStackOf: input.nativeStackOf,
    entriesByUrl: new Map(
      Object.entries(input.reads).map(([number, read]) => [
        url(Number(number)),
        entry(url(Number(number)), read),
      ]),
    ),
  });
  const hook = renderHook((input: ReturnType<typeof props>) => useListStackNesting(input), {
    initialProps: props(initial),
  });
  return {
    shape: () =>
      hook.result.current
        ?.get("open")
        ?.map((placement) => [Number(placement.url.split("/").pop()), placement.depth]) ?? null,
    rerender: (input: typeof initial) => hook.rerender(props(input)),
  };
}

const stacked = { 100: { body: body(100, STACK) }, 101: { body: body(101, STACK) } };

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("rowStackBody", () => {
  it("counts a read with no usable description as unread, not as no stack", () => {
    expect(rowStackBody(entry(url(1), { body: null }))).toEqual({ kind: "unread" });
    expect(rowStackBody(entry(url(1), { status: "error" }))).toEqual({ kind: "unread" });
    expect(rowStackBody(entry(url(1), { status: "loading" }))).toEqual({ kind: "waiting" });
    expect(rowStackBody(undefined)).toEqual({ kind: "unread" });
  });
});

describe("useListStackNesting", () => {
  it("nests once the group's reads settle, whatever the rows that won't be read", () => {
    // #7 isn't read on this Mac, and #8's read failed: neither holds the stack back.
    const view = render({
      scope: "a",
      groups: group([100, 101, 7, 8]),
      reads: { ...stacked, 8: { status: "error" } },
    });
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 1],
      [7, 0],
      [8, 0],
    ]);
  });

  it("nests by GitHub's own stack where the rows' descriptions list none", () => {
    const member = (number: number) => ({
      url: url(number),
      number,
      state: "OPEN",
      isDraft: false,
      headRefName: `stack/${number}`,
    });
    const answer = (position: number): NativeStackAnswer => ({
      stackEntry: { position },
      stack: {
        id: "PRS_1",
        number: 1,
        size: 2,
        baseRefName: "main",
        entries: {
          hasNextPage: false,
          nodes: [
            { position: 1, pullRequest: member(100) },
            { position: 2, pullRequest: member(101) },
          ],
        },
      },
    });
    const view = render({
      scope: "a",
      groups: group([101, 7, 100]),
      reads: { 100: { body: "Adds the API." }, 101: { body: "Adds the UI." }, 7: { body: "" } },
      nativeStackOf: (rowUrl) =>
        rowUrl === url(100) ? answer(1) : rowUrl === url(101) ? answer(2) : undefined,
    });
    expect(view.shape()).toEqual([
      [7, 0],
      [100, 0],
      [101, 1],
    ]);
  });

  it("never nests under a row GitHub's own read says isn't in a stack", () => {
    const view = render({
      scope: "a",
      groups: group([100, 101]),
      reads: { 100: { body: "Adds the API." }, 101: { body: "Adds the UI." } },
      nativeStackOf: (rowUrl) =>
        rowUrl === url(100)
          ? { stackEntry: null, stack: null }
          : {
              stackEntry: { position: 2 },
              stack: {
                id: "PRS_1",
                number: 1,
                size: 2,
                baseRefName: "main",
                entries: {
                  hasNextPage: false,
                  nodes: [100, 101].map((number, index) => ({
                    position: index + 1,
                    pullRequest: {
                      url: url(number),
                      number,
                      state: "OPEN",
                      isDraft: false,
                      headRefName: `stack/${number}`,
                    },
                  })),
                },
              },
            },
    });
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 0],
    ]);
  });

  it("shows a group flat while a read is under way, then nests it without the slow row", () => {
    const view = render({
      scope: "a",
      groups: group([100, 101, 7]),
      reads: { ...stacked, 7: { status: "loading" } },
    });
    expect(view.shape()).toBeNull();
    act(() => vi.advanceTimersByTime(STACK_SETTLE_LIMIT_MS));
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 1],
      [7, 0],
    ]);
  });

  it("nests GitHub's stack at once while descriptions load, leaving the rows they place flat", () => {
    const answer = (self: number): NativeStackAnswer => ({
      stackEntry: { position: [200, 201].indexOf(self) + 1 },
      stack: {
        id: "PRS_2",
        number: 2,
        size: 2,
        baseRefName: "main",
        entries: {
          hasNextPage: false,
          nodes: [200, 201].map((number, index) => ({
            position: index + 1,
            pullRequest: {
              url: url(number),
              number,
              state: "OPEN",
              isDraft: false,
              headRefName: `stack/${number}`,
            },
          })),
        },
      },
    });
    const nativeStackOf = (rowUrl: string) =>
      rowUrl === url(200) ? answer(200) : rowUrl === url(201) ? answer(201) : undefined;
    const loading = { status: "loading" } as const;
    // Every row's description is still loading, including an unrelated one and a described stack.
    const view = render({
      scope: "a",
      groups: group([201, 7, 101, 200, 100]),
      reads: { 200: loading, 201: loading, 7: loading, 100: loading, 101: loading },
      nativeStackOf,
    });
    expect(view.shape()).toEqual([
      [7, 0],
      [101, 0],
      [200, 0],
      [201, 1],
      [100, 0],
    ]);
    // The descriptions arrive: the described stack nests too, and GitHub's stays as it was.
    view.rerender({
      scope: "a",
      groups: group([201, 7, 101, 200, 100]),
      reads: { 200: loading, 201: loading, 7: { body: "" }, ...stacked },
      nativeStackOf,
    });
    expect(view.shape()).toEqual([
      [7, 0],
      [200, 0],
      [201, 1],
      [100, 0],
      [101, 1],
    ]);
  });

  it("nests a GitHub stack whose descriptions are all still loading, without waiting", () => {
    const answer = (position: number): NativeStackAnswer => ({
      stackEntry: { position },
      stack: {
        id: "PRS_1",
        number: 1,
        size: 2,
        baseRefName: "main",
        entries: {
          hasNextPage: false,
          nodes: STACK.map((number, index) => ({
            position: index + 1,
            pullRequest: {
              url: url(number),
              number,
              state: "OPEN",
              isDraft: false,
              headRefName: `stack/${number}`,
            },
          })),
        },
      },
    });
    const view = render({
      scope: "a",
      groups: group(STACK),
      reads: { 100: { status: "loading" }, 101: { status: "loading" } },
      nativeStackOf: (rowUrl) => answer(STACK.indexOf(Number(rowUrl.split("/").pop())) + 1),
    });
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 1],
    ]);
  });

  it("waits for descriptions once it's on, though the group showed while it was off", () => {
    const loading = { status: "loading" } as const;
    const off = { scope: "a", groups: group(STACK), reads: { 100: loading, 101: loading } };
    const view = render({ ...off, enabled: false });
    expect(view.shape()).toBeNull();
    // On, with the descriptions still loading: flat, then nested once they come.
    view.rerender({ ...off, enabled: true });
    expect(view.shape()).toBeNull();
    view.rerender({ ...off, enabled: true, reads: stacked });
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 1],
    ]);
  });

  it("starts the settle limit once it's on, not while it was off", () => {
    const loading = { status: "loading" } as const;
    const off = { scope: "a", groups: group(STACK), reads: { 100: loading, 101: loading } };
    const view = render({ ...off, enabled: false });
    act(() => vi.advanceTimersByTime(STACK_SETTLE_LIMIT_MS));
    view.rerender({ ...off, enabled: true });
    expect(view.shape()).toBeNull();
    act(() => vi.advanceTimersByTime(STACK_SETTLE_LIMIT_MS));
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 0],
    ]);
  });

  it("starts a group's settle limit when it first shows, not when the list mounted", () => {
    // The table waits on its first answer for longer than the limit, with no groups yet.
    const view = render({ scope: "a", groups: [], reads: {} });
    act(() => vi.advanceTimersByTime(STACK_SETTLE_LIMIT_MS + 1));
    view.rerender({
      scope: "a",
      groups: group([100, 101, 7]),
      reads: { ...stacked, 7: { status: "loading" } },
    });
    expect(view.shape()).toBeNull();
    act(() => vi.advanceTimersByTime(STACK_SETTLE_LIMIT_MS - 1));
    expect(view.shape()).toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 1],
      [7, 0],
    ]);
  });

  it("keeps a nested group as it is through polls and new rows", () => {
    const view = render({ scope: "a", groups: group([100, 101]), reads: stacked });
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 1],
    ]);
    // A poll brings the same descriptions, and a PR that joins the list starts loading.
    view.rerender({
      scope: "a",
      groups: group([100, 101, 9]),
      reads: { ...stacked, 9: { status: "loading" } },
    });
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 1],
      [9, 0],
    ]);
  });

  it("starts over, flat, for another account, keeping nothing of the last one's stacks", () => {
    const view = render({ scope: "a", groups: group([100, 101]), reads: stacked });
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 1],
    ]);
    // The new account's reads are under way: nothing of the old tree stays.
    view.rerender({
      scope: "b",
      groups: group([100, 101]),
      reads: { 100: { status: "loading" }, 101: { status: "loading" } },
    });
    expect(view.shape()).toBeNull();
    // Its own descriptions list no stack, so nothing nests.
    view.rerender({
      scope: "b",
      groups: group([100, 101]),
      reads: { 100: { body: "Plain." }, 101: { body: "Plain." } },
    });
    expect(view.shape()).toEqual([
      [100, 0],
      [101, 0],
    ]);
  });
});
