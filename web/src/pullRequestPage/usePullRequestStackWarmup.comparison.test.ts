// Reading a description stack's comparison ahead with the real planner: it waits for the
// member's own shared members read, however long that takes, rather than deciding without it.

import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pullRequestStackMembersQueryOptions } from "@/hooks/usePullRequestStackMembers";
import { pullRequestInfoReadAheadOptions } from "@/newPullRequestPage/hooks/usePullRequests";
import type * as DesktopPullRequestsModule from "@/lib/desktopPullRequests";
import type * as DesktopReadPathModule from "@/lib/desktopReadPath";
import {
  acceptMacRead,
  beginMacRead,
  type MacReadScopeKey,
  resetMacReadScopesForTests,
} from "@/lib/macReadScope";
import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";
import type * as NativeStackReadModule from "@/lib/pullRequestStacks/nativeStackRead";
import type * as StackCompareModule from "@/lib/pullRequestStacks/stackCompare";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import type * as StackMembersModule from "@/lib/pullRequestStacks/stackMembers";
import type { StackMember } from "@/lib/pullRequestStacks/stackMembers";
import { stackPullRequestRef } from "@/lib/pullRequestStacks/stackModel";
import {
  createStackWarmupQueue,
  type StackWarmupPacing,
} from "@/lib/pullRequestStacks/stackWarmup";

import { stackWarmupReads } from "./usePullRequestStackWarmup";

// Stack descriptions in an example format, which the page reads once a build registers it.
const STACK_FORMATS = [EXAMPLE_STACK_FORMAT];
withPullRequestIntegrations({ stackFormats: STACK_FORMATS });

/** What went out to this Mac, in order, and the members reads held until released. */
const net = vi.hoisted(() => ({
  log: [] as string[],
  holdMembers: false,
  failMembers: false,
  /** A shell that reads GitHub's own stacks, which stacks #102 on #100. */
  native: false,
  /** What GitHub's members read answers now: the parent, and the child where set. */
  answer: null as null | (() => StackMember[]),
  /** The parent head each comparison asked for. */
  parentHeads: [] as string[],
  members: [] as { numbers: number[]; release: () => void }[],
}));
const numberOf = (url: string) => Number(url.split("/").pop());

vi.mock("@/lib/desktopReadPath", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopReadPathModule>()),
  macOffers: () => true,
}));
vi.mock("@/lib/pullRequestStacks/nativeStackRead", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeStackReadModule>()),
  // Without GitHub's own stacks, the description decides.
  nativeStackOffered: () => net.native,
  fetchNativeStack: async (target: { number: number }) => {
    net.log.push(`native:${target.number}`);
    const node = (position: number, member: StackMember) => ({
      position,
      pullRequest: {
        url: member.url,
        number: member.number,
        state: "OPEN",
        isDraft: false,
        headRefName: member.head_ref!,
        details: member,
      },
    });
    return {
      viewer: "alice",
      answer: {
        stackEntry: { position: 2 },
        stack: {
          id: "PRS_1",
          number: 1,
          size: 2,
          baseRefName: "main",
          entries: { hasNextPage: false, nodes: [node(1, parent()), node(2, child())] },
        },
      },
    };
  },
}));
vi.mock("@/lib/desktopPullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopPullRequestsModule>()),
  fetchDesktopPullRequestChangedFiles: async (target: { number: number }) => {
    net.log.push(`files:${target.number}`);
    return { available: true, data: [] };
  },
  fetchDesktopPullRequestDiff: async (target: { number: number }) => {
    net.log.push(`patch:${target.number}`);
    return { object: "session.github.pr_diff", patch: "" };
  },
}));
vi.mock("@/lib/pullRequestStacks/stackMembers", async (importOriginal) => ({
  ...(await importOriginal<typeof StackMembersModule>()),
  fetchStackMembers: async (_target: unknown, numbers: number[]) => {
    net.log.push(`members:${[...numbers].sort().join(",")}`);
    if (net.holdMembers) {
      await new Promise<void>((resolve) => {
        net.members.push({ numbers, release: resolve });
      });
    }
    if (net.failMembers) throw new Error("GitHub couldn't answer");
    return { viewer: "alice", members: net.answer?.() ?? [parent()], missing: [], unread: [] };
  },
}));
vi.mock("@/lib/pullRequestStacks/stackCompare", async (importOriginal) => ({
  ...(await importOriginal<typeof StackCompareModule>()),
  stackCompareOffered: () => true,
  // Real hashing settles off the fake clock; the description's digest isn't what's tested.
  descriptionDigest: async () => "digest",
  fetchStackComparison: async (target: { number: number }, request: { parentHead: string }) => {
    net.log.push(`compare:${target.number}`);
    net.parentHeads.push(request.parentHead);
    return { viewer: "alice", separable: true, reason: null, files: [] };
  },
}));

const SCOPE: MacReadScopeKey = ["mac-read", "user-1", "github", "github.com"];
const urlOf = (number: number) => `https://github.com/acme/app/pull/${number}`;
const child = (): StackMember => ({
  ...parent(),
  url: urlOf(102),
  number: 102,
  title: "PR 102",
  head_ref: "stack/102",
  head_sha: "c".repeat(40),
});
const parent = (): StackMember => ({
  url: urlOf(100),
  number: 100,
  title: "PR 100",
  state: "OPEN",
  is_draft: false,
  updated_at: null,
  review_decision: null,
  head_ref: "stack/100",
  head_sha: "a".repeat(40),
  base_ref: "main",
  base_sha: "d".repeat(40),
  head_repository: "acme/app",
  base_repository: "acme/app",
});

/** A stack description listing `numbers` base first, as `self`'s. */
const bodyOf = (self: number, numbers: number[]) =>
  [
    "## Stacked changes",
    ...numbers.map((number, depth) => {
      const label = number === self ? `**stack/${number}**` : `stack/${number}`;
      return `${"  ".repeat(depth)}- [${label}](${urlOf(number)})${number === self ? " ← _this PR_" : ""}`;
    }),
    "",
    "---------",
    "The change.",
  ].join("\n");

const reads = {
  known: () => true,
  readsOnThisMac: () => true,
  blocked: () => false,
  target: (url: string | null | undefined) => ({
    provider: "github" as const,
    host: "github.com",
    repository: "acme/app",
    number: numberOf(url ?? ""),
    url: url ?? "",
  }),
  scope: () => SCOPE,
} as unknown as Parameters<typeof stackWarmupReads>[1]["reads"];

/** The paced queue on fake timers, every idle moment at once. */
const pacing: StackWarmupPacing = {
  budget: () => ({ pace: "normal" }),
  cooldownUntil: () => null,
  onBudgetEased: () => () => {},
  now: () => Date.now(),
  whenIdle: (work) => {
    const handle = setTimeout(work, 0);
    return () => clearTimeout(handle);
  },
  setTimer: (work, ms) => {
    const handle = setTimeout(work, ms);
    return () => clearTimeout(handle);
  },
};

let client: QueryClient;
beforeEach(() => {
  vi.useFakeTimers();
  client = new QueryClient();
  net.log = [];
  net.holdMembers = false;
  net.failMembers = false;
  net.members = [];
  net.native = false;
  net.answer = null;
  net.parentHeads = [];
  acceptMacRead(beginMacRead(SCOPE), "alice");
});
afterEach(() => {
  vi.useRealTimers();
  resetMacReadScopesForTests();
});

/** PR 102's details, read moments ago, its description listing `numbers`. */
function seedDetails(numbers: number[]) {
  const options = pullRequestInfoReadAheadOptions("pull-requests-page", urlOf(102), reads, true)!;
  client.setQueryData(options.queryKey, {
    pr: {
      url: urlOf(102),
      body: bodyOf(102, numbers),
      head_sha: "c".repeat(40),
      base_sha: "d".repeat(40),
      base_ref: "main",
    },
  } as never);
}

function readAhead() {
  const queue = createStackWarmupQueue(pacing);
  queue.submit(
    {},
    stackWarmupReads([urlOf(102)], {
      queryClient: client,
      reads,
      sessionFor: () => "pull-requests-page",
      sessionless: () => true,
      comparisons: { enabled: true, automaticPolicy: true },
    }),
  );
}

const advance = (ms: number) => vi.advanceTimersByTimeAsync(ms);

describe("reading a description stack's comparison ahead", () => {
  it("reads it once the member's shared members read answers, however late", async () => {
    net.holdMembers = true;
    seedDetails([100, 102]);
    readAhead();
    await advance(5_000);
    expect(net.log).toEqual(["members:100,102", "files:102", "patch:102"]);
    net.members.forEach((held) => held.release());
    await advance(5_000);
    expect(net.log).toEqual(["members:100,102", "files:102", "patch:102", "compare:102"]);
  });

  it("reads the members its own description lists, never read before, then compares", async () => {
    // Another description's members are cached; this member's lists a stack of its own.
    const other = parseStack(
      STACK_FORMATS,
      bodyOf(101, [100, 101, 102]),
      stackPullRequestRef(urlOf(101))!,
    );
    client.setQueryData(
      pullRequestStackMembersQueryOptions(urlOf(101), other, reads).options.queryKey,
      {
        viewer: "alice",
        members: [parent()],
        missing: [],
        unread: [],
      } as never,
    );
    seedDetails([100, 102]);
    readAhead();
    await advance(5_000);
    expect(net.log).toContain("members:100,102");
    expect(net.log.indexOf("compare:102")).toBeGreaterThan(net.log.indexOf("members:100,102"));
  });

  it("compares a PR GitHub stacks once GitHub answers, with no members read", async () => {
    net.native = true;
    seedDetails([]);
    readAhead();
    await advance(5_000);
    expect(net.log).toContain("compare:102");
    expect(net.log.indexOf("compare:102")).toBeGreaterThan(net.log.indexOf("native:102"));
    expect(net.log.some((entry) => entry.startsWith("members"))).toBe(false);
  });

  describe("with a members answer read moments ago", () => {
    /** The shared answer cached now, recording the child as `recorded` says and the parent at `a`. */
    const seedMembers = (recorded: Partial<StackMember>) => {
      const self = stackPullRequestRef(urlOf(102))!;
      const parse = parseStack(STACK_FORMATS, bodyOf(102, [100, 102]), self)!;
      client.setQueryData(
        pullRequestStackMembersQueryOptions(urlOf(102), parse, reads).options.queryKey,
        {
          viewer: "alice",
          members: [parent(), { ...child(), ...recorded }],
          missing: [],
          unread: [],
        } as never,
      );
    };
    const pushedParent = () => ({ ...parent(), head_sha: "b".repeat(40) });

    it.each([
      ["a head the child has since moved on from", { head_sha: "e".repeat(40) }],
      ["a base it has since been retargeted from", { base_ref: "release" }],
      ["a base commit it has since moved from", { base_sha: "f".repeat(40) }],
    ])("reads it again first when it records the child at %s", async (_case, recorded) => {
      seedMembers(recorded);
      // Since then the parent pushed too: only a fresh read knows its new head.
      net.answer = () => [pushedParent(), child()];
      seedDetails([100, 102]);
      readAhead();
      await advance(5_000);
      expect(net.log.filter((entry) => entry.startsWith("members"))).toEqual(["members:100,102"]);
      expect(net.log.indexOf("compare:102")).toBeGreaterThan(net.log.indexOf("members:100,102"));
      expect(net.parentHeads).toEqual(["b".repeat(40)]);
    });

    it("relies on it as it is when it records the child as its details do", async () => {
      seedMembers({});
      seedDetails([100, 102]);
      readAhead();
      await advance(5_000);
      expect(net.log.some((entry) => entry.startsWith("members"))).toBe(false);
      expect(net.parentHeads).toEqual(["a".repeat(40)]);
    });

    it("reads it again only once while GitHub still answers with the old head", async () => {
      seedMembers({ head_sha: "e".repeat(40) });
      net.answer = () => [parent(), { ...child(), head_sha: "e".repeat(40) }];
      seedDetails([100, 102]);
      readAhead();
      await advance(10_000);
      expect(net.log.filter((entry) => entry.startsWith("members"))).toHaveLength(1);
    });
  });

  it("reads no comparison when the members read fails, rather than deciding without it", async () => {
    net.failMembers = true;
    seedDetails([100, 102]);
    readAhead();
    await advance(10_000);
    expect(net.log).toContain("members:100,102");
    expect(net.log).not.toContain("compare:102");
  });
});
