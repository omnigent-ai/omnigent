import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { dropStackSnapshots } from "@/hooks/pullRequestStackSnapshots";

import { usePullRequestNativeStack } from "@/hooks/usePullRequestNativeStack";
import type * as StackMembersHooks from "@/hooks/usePullRequestStackMembers";
import {
  pullRequestStackMembersQueryKey,
  usePullRequestStackMembers,
} from "@/hooks/usePullRequestStackMembers";
import type { NativeStackAnswer } from "@/lib/pullRequestStacks/nativeStack";
import { type StackMember, stackMemberLookups } from "@/lib/pullRequestStacks/stackMembers";
import { describedStackIdentity } from "@/lib/pullRequestStacks/stackSnapshot";

import {
  PULL_REQUEST_STACKS_FLAG,
  type StackedPullRequest,
  usePullRequestStack,
  usePullRequestStackRead,
} from "./usePullRequestStack";
import { withPullRequestIntegrations, EXAMPLE_STACK_FORMAT } from "@/test/pullRequestIntegrations";

// Stack descriptions in an example format, which the page reads once a build registers it.
withPullRequestIntegrations({ stackFormats: [EXAMPLE_STACK_FORMAT] });

const readFlag = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useDesktopFeatureFlag", async () =>
  (await import("@/test/desktopFeatureFlagMock")).desktopFeatureFlagModule(readFlag),
);
vi.mock("@/hooks/usePullRequestStackMembers", async (importOriginal) => ({
  ...(await importOriginal<typeof StackMembersHooks>()),
  usePullRequestStackMembers: vi.fn(),
}));
vi.mock("@/hooks/usePullRequestNativeStack", () => ({ usePullRequestNativeStack: vi.fn() }));

let client: QueryClient;
function renderStack<Result>(hook: () => Result) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(hook, { wrapper });
}

const PR_URL = "https://github.com/acme/app/pull/101";
const BODY = [
  "## Stacked changes",
  "- [stack/100](https://github.com/acme/app/pull/100)",
  `  - [**stack/101**](${PR_URL}) ← _this PR_`,
  "",
  "---------",
  "Adds the auth API.",
].join("\n");
const PR = { url: PR_URL, body: BODY, head_sha: "a".repeat(40) };
const SELECT = () => true;

let flag = true;
beforeEach(() => {
  client = new QueryClient();
  flag = true;
  readFlag.mockImplementation(() => ({ value: flag, resolved: true }));
  vi.mocked(usePullRequestStackMembers).mockReturnValue({ kind: "unavailable" });
  vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "off" });
});
afterEach(cleanup);

describe("usePullRequestStack", () => {
  it("reads a stacked PR's stack, its members read for the PR's head and base", () => {
    const { result } = renderStack(() => usePullRequestStack(PR, SELECT));
    expect(readFlag).toHaveBeenCalledWith(PULL_REQUEST_STACKS_FLAG, false, "value");
    expect(result.current?.position).toEqual({ level: 2, levels: 2 });
    expect(result.current?.previous.kind).toBe("go");
    expect(vi.mocked(usePullRequestStackMembers).mock.lastCall).toEqual([
      "",
      PR_URL,
      result.current?.parse,
      { head_sha: PR.head_sha, base_ref: undefined, base_sha: undefined },
    ]);
  });

  it("shows no stack while the flag is off, nor for a PR that isn't stacked", () => {
    flag = false;
    expect(renderStack(() => usePullRequestStack(PR, SELECT)).result.current).toBeNull();
    flag = true;
    const plain = { ...PR, body: "Adds the auth API." };
    expect(renderStack(() => usePullRequestStack(plain, SELECT)).result.current).toBeNull();
    expect(renderStack(() => usePullRequestStack(null, SELECT)).result.current).toBeNull();
  });
});

const memberOf = (number: number): StackMember => ({
  url: `https://github.com/acme/app/pull/${number}`,
  number,
  title: `PR ${number}`,
  state: "OPEN",
  is_draft: false,
  updated_at: null,
  review_decision: null,
  head_ref: `native/${number}`,
  head_sha: String(number).padStart(40, "a"),
  base_ref: "main",
  base_sha: "b".repeat(40),
  head_repository: "acme/app",
  base_repository: "acme/app",
});

/** GitHub's answer: a stack of `numbers`, base first, of `size`, PR 101 at its place. */
function nativeAnswer(
  numbers: number[],
  { size = numbers.length, baseRefName = "main", hasNextPage = false, id = "PRS_1" } = {},
): NativeStackAnswer {
  return {
    stackEntry: { position: numbers.indexOf(101) + 1 },
    stack: {
      id,
      number: 3,
      size,
      baseRefName,
      entries: {
        hasNextPage,
        nodes: numbers.map((number, index) => {
          const details = memberOf(number);
          return {
            position: index + 1,
            pullRequest: {
              url: details.url,
              number,
              state: "OPEN",
              isDraft: false,
              headRefName: details.head_ref!,
              details,
            },
          };
        }),
      },
    },
  };
}

function settled(
  answer: NativeStackAnswer | undefined,
  members = answer ? [memberOf(100), memberOf(101)] : [],
  readAt = Date.now(),
) {
  vi.mocked(usePullRequestNativeStack).mockReturnValue({
    kind: "settled",
    read: answer ? { viewer: "octo", answer } : undefined,
    members: answer ? { viewer: "octo", members, missing: [], unread: [] } : null,
    readAt,
  });
}

describe("usePullRequestStack with GitHub's own stack", () => {
  it("shows GitHub's stack in place of the description's, with its own members", () => {
    // The description says 100 → 101; GitHub says 99 → 100 → 101 → 102.
    settled(nativeAnswer([99, 100, 101, 102]), [
      memberOf(99),
      memberOf(100),
      memberOf(101),
      memberOf(102),
    ]);
    const { result } = renderStack(() => usePullRequestStack(PR, SELECT));
    expect(result.current?.parse.format).toBe("github-native");
    expect(result.current?.parse.nodes.map((node) => node.ref?.number)).toEqual([
      99, 100, 101, 102,
    ]);
    expect(result.current?.position).toEqual({ level: 3, levels: 4 });
    expect(result.current?.partial).toBeNull();
    expect(result.current?.members).toMatchObject({ kind: "loaded", answer: { viewer: "octo" } });
    // The description's members aren't read for a stack GitHub answered.
    expect(vi.mocked(usePullRequestStackMembers).mock.lastCall?.[2]).toBeNull();
    // The diff's parent is GitHub's, with its head.
    const parent = result.current?.statuses.nodes[result.current.parse.selfIndex - 1];
    expect(parent).toMatchObject({ kind: "member", member: { head_sha: memberOf(100).head_sha } });
  });

  it("shows no stack while GitHub's answer is on its way, even one the description lists", () => {
    vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "loading" });
    expect(renderStack(() => usePullRequestStack(PR, SELECT)).result.current).toBeNull();
    expect(vi.mocked(usePullRequestStackMembers).mock.lastCall?.[2]).toBeNull();
    // Merging waits on it too: GitHub may yet say it merges the PR with a stack. The header
    // holds the description's stack's place meanwhile.
    expect(renderStack(() => usePullRequestStackRead(PR, SELECT)).result.current).toEqual({
      stack: null,
      waiting: true,
      arrival: null,
      shown: null,
      loading: true,
    });
    settled(undefined);
    expect(renderStack(() => usePullRequestStackRead(PR, SELECT)).result.current.waiting).toBe(
      false,
    );
  });

  it("keeps to the description where GitHub can't say, or says the PR isn't stacked", () => {
    settled(undefined);
    expect(
      renderStack(() => usePullRequestStack(PR, SELECT)).result.current?.parse.format,
    ).not.toBe("github-native");
    vi.mocked(usePullRequestNativeStack).mockReturnValue({
      kind: "settled",
      read: { viewer: "octo", answer: { stackEntry: null, stack: null } },
      members: { viewer: "octo", members: [], missing: [], unread: [] },
      readAt: Date.now(),
    });
    const { result } = renderStack(() => usePullRequestStack(PR, SELECT));
    expect(result.current?.parse.trust).toBe("verified");
    expect(result.current?.position).toEqual({ level: 2, levels: 2 });
  });

  it("offers only the neighbors it read of a stack read in part, and says how big it is", () => {
    settled(nativeAnswer([100, 101], { size: 60, hasNextPage: true }));
    const { result } = renderStack(() =>
      usePullRequestStack({ ...PR, head_ref: "native/101" }, SELECT),
    );
    expect(result.current?.partial).toEqual({ size: 60, url: PR_URL });
    expect(result.current?.parse.nodes.map((node) => [node.ref?.number, node.branch])).toEqual([
      [100, "native/100"],
      [101, "native/101"],
    ]);
    expect(result.current?.previous.kind).toBe("go");
    expect(result.current?.next.kind).toBe("unavailable");
  });
});

const urlOf = (number: number) => `https://github.com/acme/app/pull/${number}`;
/** A member as its own details give it, at the head and base the stack's reads record. */
const memberPr = (number: number, body: string | null = null): StackedPullRequest => ({
  url: urlOf(number),
  body,
  head_sha: memberOf(number).head_sha!,
  head_ref: memberOf(number).head_ref,
  base_ref: "main",
  base_sha: "b".repeat(40),
});
/** GitHub's answer as `self`'s own read gives it. */
const answerFor = (numbers: number[], self: number, options = {}): NativeStackAnswer => ({
  ...nativeAnswer(numbers, options),
  stackEntry: { position: numbers.indexOf(self) + 1 },
});

/** PR `source` read GitHub's stack of `numbers` and showed it. */
/** PR `source` read GitHub's stack of `numbers`, its answer from `readAt`, and showed it. */
function openNative(
  numbers: number[],
  source: number,
  options: { size?: number; hasNextPage?: boolean; id?: string } = {},
  readAt = Date.now(),
) {
  settled(answerFor(numbers, source, options), numbers.map(memberOf), readAt);
  return renderStack(() => usePullRequestStackRead(memberPr(source), SELECT));
}

/** Whether `number`, opened while GitHub reads its stack, shows one from another member. */
function arrivesWithStack(number: number) {
  vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "loading" });
  return renderStack(() => usePullRequestStackRead(memberPr(number), SELECT)).result.current
    .arrival;
}

describe("usePullRequestStackRead arriving at another member", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows GitHub's stack at once from the member read before, then its own read", () => {
    openNative([100, 101, 102], 100);
    vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "loading" });
    const { result, rerender } = renderStack(() =>
      usePullRequestStackRead(memberPr(102), SELECT, { subtab: "changes" }),
    );
    expect(result.current.stack).toBeNull();
    expect(result.current.waiting).toBe(true);
    expect(result.current.arrival?.position).toEqual({ level: 3, levels: 3 });
    expect(result.current.arrival?.statuses.nodes.map((status) => status.kind)).toEqual([
      "member",
      "member",
      "member",
    ]);
    expect(result.current.arrival?.subtab).toBe("changes");
    const arrival = result.current.arrival;
    // Its own read answers the same: the same layout, now its own.
    settled(answerFor([100, 101, 102], 102), [100, 101, 102].map(memberOf));
    rerender();
    expect(result.current.arrival).toBeNull();
    expect(result.current.stack?.position).toEqual(arrival?.position);
    expect(result.current.stack?.rows).toEqual(arrival?.rows);
    expect(result.current.stack?.statuses).toEqual(arrival?.statuses);
  });

  it("keeps GitHub's size and gaps for a stack read in part", () => {
    openNative([100, 101], 100, { size: 60, hasNextPage: true });
    vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "loading" });
    const { result } = renderStack(() => usePullRequestStackRead(memberPr(101), SELECT));
    expect(result.current.arrival?.partial).toEqual({ size: 60, url: urlOf(101) });
    expect(result.current.arrival?.position).toEqual({ level: 2, levels: 60 });
  });

  it("shows nothing for a member pushed or retargeted since, or one not in the stack", () => {
    openNative([100, 101, 102], 100);
    vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "loading" });
    const pushed = { ...memberPr(101), head_sha: "f".repeat(40) };
    const retargeted = { ...memberPr(101), base_ref: "release" };
    for (const pr of [pushed, retargeted, memberPr(103)]) {
      expect(renderStack(() => usePullRequestStackRead(pr, SELECT)).result.current.arrival).toBe(
        null,
      );
    }
  });

  it("shows nothing from a record too old, from another reader, or after Refresh", () => {
    const start = Date.now();
    openNative([100, 101], 100);
    vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "loading" });
    const arrive = () =>
      renderStack(() => usePullRequestStackRead(memberPr(101), SELECT)).result.current.arrival;
    expect(arrive()).not.toBeNull();
    vi.spyOn(Date, "now").mockReturnValue(start + 10 * 60_000);
    expect(arrive()).toBeNull();
    vi.restoreAllMocks();
    // Another session's host never opens with this one's record.
    expect(
      renderStack(() => usePullRequestStackRead(memberPr(101), SELECT, { conversationId: "other" }))
        .result.current.arrival,
    ).toBeNull();
    dropStackSnapshots(client);
    expect(arrive()).toBeNull();
  });

  it("never makes an old read young again by showing it again", () => {
    const start = Date.now();
    const source = openNative([100, 101], 100, {}, start);
    vi.spyOn(Date, "now").mockReturnValue(start + 6 * 60_000);
    expect(arrivesWithStack(101)).toBeNull();
    // The member that read it shows again, with the same answer, nothing read since.
    settled(answerFor([100, 101], 100), [100, 101].map(memberOf), start);
    source.rerender();
    renderStack(() => usePullRequestStackRead(memberPr(100), SELECT));
    expect(arrivesWithStack(101)).toBeNull();
  });

  it("keeps a member out once its own newer read left, whatever an older one says again", () => {
    const start = Date.now();
    const source = openNative([100, 101], 100, {}, start);
    settled({ stackEntry: null, stack: null }, [], start + 1_000);
    renderStack(() =>
      usePullRequestStackRead(memberPr(101), SELECT, { detailsReadAt: start + 1_000 }),
    );
    // The base shows again with its older answer, which still lists the member.
    settled(answerFor([100, 101], 100), [100, 101].map(memberOf), start);
    source.rerender();
    expect(arrivesWithStack(101)).toBeNull();
  });

  it("follows a member to the stack its newer read moved it to", () => {
    const start = Date.now();
    const source = openNative([100, 101, 102], 100, {}, start);
    openNative([101, 103], 101, { id: "PRS_2" }, start + 1_000);
    settled(answerFor([100, 101, 102], 100), [100, 101, 102].map(memberOf), start);
    source.rerender();
    const arrival = arrivesWithStack(101);
    expect(arrival?.parse.nodes.map((node) => node.ref?.number)).toEqual([101, 103]);
  });

  it("retires a member a newer read of the same stack no longer lists", () => {
    const start = Date.now();
    openNative([100, 101, 102], 100, {}, start);
    expect(arrivesWithStack(102)).not.toBeNull();
    openNative([100, 101], 100, {}, start + 1_000);
    expect(arrivesWithStack(102)).toBeNull();
    expect(arrivesWithStack(101)).not.toBeNull();
  });

  it("forgets a member whose own read says it left the stack", () => {
    const start = Date.now();
    openNative([100, 101], 100, {}, start);
    settled({ stackEntry: null, stack: null }, [], start + 1_000);
    renderStack(() =>
      usePullRequestStackRead(memberPr(101), SELECT, { detailsReadAt: start + 1_000 }),
    );
    vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "loading" });
    expect(
      renderStack(() => usePullRequestStackRead(memberPr(101), SELECT)).result.current.arrival,
    ).toBeNull();
  });

  describe("a description's stack", () => {
    const body = (self: number, numbers = [100, 101]) =>
      [
        "## Stacked changes",
        ...numbers.map(
          (number, depth) =>
            `${"  ".repeat(depth)}- [${number === self ? `**stack/${number}**` : `stack/${number}`}](${urlOf(number)})${number === self ? " ← _this PR_" : ""}`,
        ),
        "",
        "---------",
        "Adds the auth API.",
      ].join("\n");

    /**
     * PR `source` showed its description's stack of `numbers`, its details read at `readAt`,
     * and the stack's shared members read answered. Returns its view, to show again.
     */
    function openDescribed(source = 100, numbers = [100, 101], readAt = Date.now()) {
      vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "off" });
      const view = renderStack(() =>
        usePullRequestStackRead(memberPr(source, body(source, numbers)), SELECT, {
          detailsReadAt: readAt,
        }),
      );
      const parse = view.result.current.stack!.parse;
      client.setQueryData(
        pullRequestStackMembersQueryKey(
          "",
          describedStackIdentity(parse),
          stackMemberLookups(parse),
        ),
        { viewer: "octo", members: numbers.map(memberOf), missing: [], unread: [] },
      );
      return view;
    }

    /** Whether `number`, its description listing `numbers`, opens with a stack from another. */
    const arrivesDescribed = (number: number, numbers = [100, 101]) => {
      vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "loading" });
      return renderStack(() =>
        usePullRequestStackRead(memberPr(number, body(number, numbers)), SELECT),
      ).result.current.arrival;
    };

    it("keeps a description read long ago old, however its cached details change since", () => {
      const start = Date.now();
      const source = openDescribed(100, [100, 101], start);
      vi.spyOn(Date, "now").mockReturnValue(start + 6 * 60_000);
      // A comment patched into the base's cached details shows it again; nothing was read.
      source.rerender();
      expect(arrivesDescribed(101)).toBeNull();
    });

    it("keeps a member in the stack its newer description moved it to", () => {
      const start = Date.now();
      const source = openDescribed(100, [100, 101], start);
      openDescribed(101, [103, 101], start + 1_000);
      source.rerender();
      expect(
        arrivesDescribed(101, [103, 101])?.parse.nodes.map((node) => node.ref?.number),
      ).toEqual([103, 101]);
    });

    it("shows the member's own description, with the shared statuses, while GitHub reads", () => {
      openDescribed();
      vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "loading" });
      const { result } = renderStack(() =>
        usePullRequestStackRead(memberPr(101, body(101)), SELECT),
      );
      const { arrival } = result.current;
      expect(arrival?.parse.trust).toBe("verified");
      // The span is this member's own description's, never the source's.
      expect(arrival?.parse.trust === "verified" && arrival.parse.span.start).toBe(0);
      expect(arrival?.position).toEqual({ level: 2, levels: 2 });
      expect(arrival?.statuses.complete).toBe(true);
    });

    it("shows nothing where the member's description lists another stack", () => {
      openDescribed();
      vi.mocked(usePullRequestNativeStack).mockReturnValue({ kind: "loading" });
      const stale = memberPr(101, body(101, [99, 101]));
      expect(renderStack(() => usePullRequestStackRead(stale, SELECT)).result.current.arrival).toBe(
        null,
      );
    });
  });
});
