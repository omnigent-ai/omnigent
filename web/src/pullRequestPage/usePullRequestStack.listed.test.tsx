// The stack the page's list nests a PR in, shown while GitHub's own answer through the session's
// host is out, and handed over to that answer once it comes, agreeing or not.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ListedStack } from "@/lib/pullRequestStacks/listedStack";
import type { NativeStackAnswer } from "@/lib/pullRequestStacks/nativeStack";
import {
  fetchNativeStackOnHost,
  type NativeStackReadAnswer,
} from "@/lib/pullRequestStacks/nativeStackHost";
import type * as NativeStackHostModule from "@/lib/pullRequestStacks/nativeStackHost";
import type * as StackMembersModule from "@/lib/pullRequestStacks/stackMembers";
import {
  fetchStackMembersOnHost,
  type StackMember,
  type StackMembersAnswer,
} from "@/lib/pullRequestStacks/stackMembers";

import { type StackedPullRequest, usePullRequestStackRead } from "./usePullRequestStack";
import { withPullRequestIntegrations, EXAMPLE_STACK_FORMAT } from "@/test/pullRequestIntegrations";

// Stack descriptions in an example format, which the page reads once a build registers it.
withPullRequestIntegrations({ stackFormats: [EXAMPLE_STACK_FORMAT] });

vi.mock("@/hooks/useDesktopFeatureFlag", async () =>
  (await import("@/test/desktopFeatureFlagMock")).desktopFeatureFlagModule(() => ({
    value: true,
    resolved: true,
  })),
);
// The session's host serves the stack members walk and, standing in for it, GitHub's own stack.
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (_session: string, method: string) =>
    method === "getPullRequestStack",
  usePullRequestCapabilityServed: () => true,
}));
vi.mock("@/lib/pullRequestStacks/stackMembers", async (importOriginal) => ({
  ...(await importOriginal<typeof StackMembersModule>()),
  fetchStackMembersOnHost: vi.fn(),
}));
vi.mock("@/lib/pullRequestStacks/nativeStackHost", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeStackHostModule>()),
  fetchNativeStackOnHost: vi.fn(),
}));

const url = (number: number) => `https://github.com/acme/app/pull/${number}`;
const PR: StackedPullRequest = {
  url: url(102),
  body: "Adds the auth API.",
  head_sha: "a".repeat(40),
  head_ref: "stack/102",
  base_ref: "stack/101",
  base_sha: "b".repeat(40),
};

/** GitHub's stack, base first, with PR 102 in it, as the list's lighter read gives it. */
function nativeStack(numbers: number[]): NativeStackAnswer {
  return {
    stackEntry: { position: numbers.indexOf(102) + 1 },
    stack: {
      id: "PRS_1",
      number: 1,
      size: numbers.length,
      baseRefName: "main",
      entries: {
        hasNextPage: false,
        nodes: numbers.map((number, index) => ({
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
  };
}

const answered = (numbers: number[]): NativeStackReadAnswer => ({
  viewer: "alice",
  answer: nativeStack(numbers),
});
const NOT_STACKED: NativeStackReadAnswer = {
  viewer: "alice",
  answer: { stackEntry: null, stack: null },
};

function listedMember(number: number): StackMember {
  return {
    url: url(number),
    number,
    title: `Listed PR ${number}`,
    state: "OPEN",
    is_draft: false,
    updated_at: null,
    review_decision: null,
    head_ref: null,
    head_sha: null,
    base_ref: null,
    base_sha: null,
    head_repository: null,
    base_repository: null,
  };
}

/** The list's word: GitHub's stack as its read gave it, and every member as a row. */
const listedAs = (native: NativeStackAnswer | undefined): ListedStack => ({
  ...(native ? { native } : {}),
  member: (target) => {
    const number = Number(target.split("/").pop());
    return target === url(number) ? listedMember(number) : null;
  },
  viewer: "alice",
});

function held<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function renderRead(pr: StackedPullRequest, listed: ListedStack | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return renderHook(
    () => usePullRequestStackRead(pr, () => true, { listed, conversationId: "s1" }),
    { wrapper },
  );
}

const numbers = (view: { rows: { node: { ref: { number: number } | null } }[] } | null) =>
  view?.rows.map((row) => row.node.ref?.number) ?? null;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("usePullRequestStackRead with the list's stack", () => {
  it("shows the stack the list nests the PR in while GitHub's own answer is out", () => {
    vi.mocked(fetchNativeStackOnHost).mockImplementation(
      () => held<NativeStackReadAnswer>().promise,
    );
    const { result } = renderRead(PR, listedAs(nativeStack([101, 102, 103])));
    expect(result.current.waiting).toBe(true);
    // Never the stack merging, comparing or editing use: only the arrival shows it.
    expect(result.current.stack).toBeNull();
    const arrival = result.current.arrival;
    expect(numbers(arrival)).toEqual([101, 102, 103]);
    expect(arrival?.statuses.nodes.map((status) => status.kind)).toEqual([
      "member",
      "member",
      "member",
    ]);
  });

  it("hands over to GitHub's answer when it agrees, with the same rows", async () => {
    const answer = held<NativeStackReadAnswer>();
    vi.mocked(fetchNativeStackOnHost).mockImplementation(() => answer.promise);
    const { result } = renderRead(PR, listedAs(nativeStack([101, 102, 103])));
    const listedRows = numbers(result.current.arrival);
    await act(async () => answer.resolve(answered([101, 102, 103])));
    await waitFor(() => expect(result.current.waiting).toBe(false));
    expect(result.current.arrival).toBeNull();
    expect(numbers(result.current.stack)).toEqual(listedRows);
  });

  it("takes GitHub's answer where it disagrees with the list", async () => {
    const answer = held<NativeStackReadAnswer>();
    vi.mocked(fetchNativeStackOnHost).mockImplementation(() => answer.promise);
    const { result } = renderRead(PR, listedAs(nativeStack([101, 102, 103])));
    await act(async () => answer.resolve(answered([101, 102])));
    await waitFor(() => expect(result.current.waiting).toBe(false));
    expect(numbers(result.current.stack)).toEqual([101, 102]);
  });

  it("shows no stack once GitHub says the PR isn't in one, whatever the list said", async () => {
    const answer = held<NativeStackReadAnswer>();
    vi.mocked(fetchNativeStackOnHost).mockImplementation(() => answer.promise);
    const { result } = renderRead(PR, listedAs(nativeStack([101, 102])));
    expect(result.current.arrival).not.toBeNull();
    await act(async () => answer.resolve(NOT_STACKED));
    await waitFor(() => expect(result.current.waiting).toBe(false));
    expect(result.current.stack).toBeNull();
    expect(result.current.arrival).toBeNull();
  });

  it("shows the stack the PR's own description lists, its members as the list shows them", () => {
    vi.mocked(fetchNativeStackOnHost).mockImplementation(
      () => held<NativeStackReadAnswer>().promise,
    );
    const body = [
      "## Stacked changes",
      `- [stack/101](${url(101)})`,
      `  - [**stack/102**](${url(102)}) ← _this PR_`,
      "",
      "---------",
      "Adds the auth API.",
    ].join("\n");
    const { result } = renderRead({ ...PR, body }, listedAs(undefined));
    expect(numbers(result.current.arrival)).toEqual([101, 102]);
    const statuses = result.current.arrival?.statuses.nodes ?? [];
    expect(statuses[0]).toMatchObject({ kind: "member", member: { title: "Listed PR 101" } });
  });

  it("follows the PR shown, never keeping another PR's listed stack", () => {
    vi.mocked(fetchNativeStackOnHost).mockImplementation(
      () => held<NativeStackReadAnswer>().promise,
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const other: StackedPullRequest = { ...PR, url: url(103), head_ref: "stack/103" };
    const otherListed: ListedStack = {
      ...listedAs(undefined),
      native: { ...nativeStack([102, 103]), stackEntry: { position: 2 } },
    };
    const { result, rerender } = renderHook(
      ({ pr, listed }: { pr: StackedPullRequest; listed: ListedStack | undefined }) =>
        usePullRequestStackRead(pr, () => true, { listed, conversationId: "s1" }),
      {
        wrapper,
        initialProps: {
          pr: PR,
          listed: listedAs(nativeStack([101, 102, 103])) as ListedStack | undefined,
        },
      },
    );
    expect(numbers(result.current.arrival)).toEqual([101, 102, 103]);
    // Another PR, still reading, with no stack the list knows: nothing of the last one shows.
    rerender({ pr: other, listed: undefined });
    expect(result.current.arrival).toBeNull();
    rerender({ pr: other, listed: otherListed });
    expect(numbers(result.current.arrival)).toEqual([102, 103]);
  });

  it("keeps the list's stack shown while the PR's own description stack reads its members", async () => {
    vi.mocked(fetchNativeStackOnHost).mockResolvedValue(NOT_STACKED);
    const members = held<StackMembersAnswer>();
    vi.mocked(fetchStackMembersOnHost).mockImplementation(() => members.promise);
    const body = [
      "## Stacked changes",
      `- [stack/101](${url(101)})`,
      `  - [**stack/102**](${url(102)}) ← _this PR_`,
      "",
      "---------",
      "Adds the auth API.",
    ].join("\n");
    const { result } = renderRead({ ...PR, body }, listedAs(undefined));
    await waitFor(() => expect(result.current.waiting).toBe(false));
    // The PR's own stack, what merging and editing use, still reads its members...
    expect(result.current.stack?.members.kind).toBe("loading");
    // ...while the card keeps the list's statuses, so it doesn't change size meanwhile.
    expect(result.current.shown?.statuses.nodes[0]).toMatchObject({
      kind: "member",
      member: { title: "Listed PR 101" },
    });
    await act(async () =>
      members.resolve({
        viewer: "alice",
        members: [{ ...listedMember(101), title: "Read PR 101" }],
        missing: [],
        unread: [],
      }),
    );
    await waitFor(() =>
      expect(result.current.shown?.statuses.nodes[0]).toMatchObject({
        member: { title: "Read PR 101" },
      }),
    );
    expect(result.current.shown).toBe(result.current.stack);
  });

  it("says the rest is loading while the list holds only part of GitHub's stack", async () => {
    const answer = held<NativeStackReadAnswer>();
    vi.mocked(fetchNativeStackOnHost).mockImplementation(() => answer.promise);
    const whole = nativeStack([101, 102, 103, 104]);
    // The list's read stops after two entries of four.
    const part: NativeStackAnswer = {
      ...whole,
      stack: {
        ...whole.stack!,
        entries: { hasNextPage: true, nodes: whole.stack!.entries.nodes.slice(0, 2) },
      },
    };
    const { result } = renderRead(PR, listedAs(part));
    expect(result.current.shown?.partial).not.toBeNull();
    expect(result.current.shown?.completing).toBe(true);
    await act(async () => answer.resolve(answered([101, 102, 103, 104])));
    await waitFor(() => expect(result.current.waiting).toBe(false));
    expect(numbers(result.current.shown)).toEqual([101, 102, 103, 104]);
    expect(result.current.shown?.completing).toBeUndefined();
  });

  it("holds the stack's place while a description lists one and GitHub's answer is out", async () => {
    const answer = held<NativeStackReadAnswer>();
    vi.mocked(fetchNativeStackOnHost).mockImplementation(() => answer.promise);
    const body = [
      "## Stacked changes",
      `- [stack/101](${url(101)})`,
      `  - [**stack/102**](${url(102)}) ← _this PR_`,
    ].join("\n");
    const { result } = renderRead({ ...PR, body }, undefined);
    expect(result.current.shown).toBeNull();
    expect(result.current.loading).toBe(true);
    await act(async () => answer.resolve(NOT_STACKED));
    await waitFor(() => expect(result.current.waiting).toBe(false));
    expect(result.current.loading).toBe(false);
    // A description that lists no stack holds no place.
    const plain = renderRead(PR, undefined);
    expect(plain.result.current.loading).toBe(false);
  });

  describe("a stack longer than GitHub's own read lists", () => {
    const all = Array.from({ length: 60 }, (_, index) => 101 + index);
    const whole = nativeStack(all);
    /** GitHub's answer for the PR at `number`, cut off after its first 50 entries. */
    function cappedFor(number: number, swap?: { at: number; number: number }): NativeStackAnswer {
      const nodes = whole.stack!.entries.nodes.slice(0, 50).map((node) =>
        swap && node.position === swap.at
          ? {
              ...node,
              pullRequest: { ...node.pullRequest!, url: url(swap.number), number: swap.number },
            }
          : node,
      );
      return {
        stackEntry: { position: all.indexOf(number) + 1 },
        stack: { ...whole.stack!, entries: { hasNextPage: true, nodes } },
      };
    }
    const listedFor = (number: number): NativeStackAnswer => ({
      ...whole,
      stackEntry: { position: all.indexOf(number) + 1 },
    });

    it.each([
      ["inside the cut-off", 102],
      ["beyond it", 155],
    ])(
      "keeps all 60 listed rows once GitHub's agreeing answer stops at 50, the PR %s",
      async (_case, number) => {
        const answer = held<NativeStackReadAnswer>();
        vi.mocked(fetchNativeStackOnHost).mockImplementation(() => answer.promise);
        const { result } = renderRead({ ...PR, url: url(number) }, listedAs(listedFor(number)));
        expect(numbers(result.current.shown)).toEqual(all);
        await act(async () => answer.resolve({ viewer: "alice", answer: cappedFor(number) }));
        await waitFor(() => expect(result.current.waiting).toBe(false));
        expect(numbers(result.current.shown)).toEqual(all);
        // Merging, comparing and editing still go by the PR's own read.
        expect(result.current.stack?.partial).not.toBeNull();
      },
    );

    it("shows GitHub's answer where it places another PR than the list does", async () => {
      const answer = held<NativeStackReadAnswer>();
      vi.mocked(fetchNativeStackOnHost).mockImplementation(() => answer.promise);
      const { result } = renderRead(PR, listedAs(listedFor(102)));
      await act(async () =>
        answer.resolve({ viewer: "alice", answer: cappedFor(102, { at: 3, number: 999 }) }),
      );
      await waitFor(() => expect(result.current.waiting).toBe(false));
      expect(numbers(result.current.shown)).not.toEqual(all);
      expect(result.current.shown?.partial).not.toBeNull();
    });
  });

  it("shows nothing at once where the list doesn't stack the PR", () => {
    vi.mocked(fetchNativeStackOnHost).mockImplementation(
      () => held<NativeStackReadAnswer>().promise,
    );
    const { result } = renderRead(PR, listedAs({ stackEntry: null, stack: null }));
    expect(result.current.arrival).toBeNull();
    const without = renderRead(PR, undefined);
    expect(without.result.current.arrival).toBeNull();
  });
});
