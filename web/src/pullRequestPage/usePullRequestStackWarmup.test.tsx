import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as UsePullRequestsModule from "@/newPullRequestPage/hooks/usePullRequests";
import type * as StackMembersHooks from "@/hooks/usePullRequestStackMembers";

import type * as StackComparisonReadModule from "./stackDiff/stackComparisonRead";
import type { StackNode, StackParse } from "@/lib/pullRequestStacks/stackModel";

import { resetReadPrioritiesForTests } from "@/hooks/pullRequestReadPriority";

import { pullRequestStackView } from "./usePullRequestStack";
import {
  resetStackWarmupQueues,
  stackWarmupQueueFor,
  usePullRequestStackWarmup,
} from "./usePullRequestStackWarmup";
import { withPullRequestIntegrations, EXAMPLE_STACK_FORMAT } from "@/test/pullRequestIntegrations";

// Stack descriptions in an example format, which the page reads once a build registers it.
withPullRequestIntegrations({ stackFormats: [EXAMPLE_STACK_FORMAT] });

const flags = vi.hoisted(() => ({ warmup: true, incremental: true, automatic: true }));
vi.mock("@/hooks/useDesktopFeatureFlag", async () =>
  (await import("@/test/desktopFeatureFlagMock")).desktopFeatureFlagModule((name, fallback) => ({
    value:
      name === "pull-request-stack-warmup"
        ? flags.warmup
        : name === "pull-request-stack-incremental-diff"
          ? flags.incremental
          : name === "pull-request-stacks"
            ? flags.automatic
            : fallback,
    resolved: true,
  })),
);

/** The members whose Changes would show a comparison, and the comparisons read. */
const comparing = vi.hoisted(() => ({ wanted: new Set<number>() }));
vi.mock("./stackDiff/stackComparisonRead", async (importOriginal) => {
  const original = await importOriginal<typeof StackComparisonReadModule>();
  const { readAtObservedPriority } = await import("@/hooks/pullRequestReadPriority");
  return {
    ...original,
    stackComparisonPlan: ({ pr }: { pr: { url: string } }) => {
      const number = Number(pr.url.split("/").pop());
      const wanted = comparing.wanted.has(number);
      return {
        parent: null,
        offered: wanted,
        baseIsParent: false,
        wanted,
        identity: wanted ? ({ number } as never) : null,
      };
    },
    stackComparisonQueryOptions: (_target: unknown, _scope: unknown, url: string) => ({
      queryKey: ["compare", url],
      queryFn: ({ client: queryClient }: { client: QueryClient }) =>
        readAtObservedPriority(queryClient, ["compare", url], async (priority) => {
          const label = `compare:${Number(url.split("/").pop())}`;
          reads.log.push(label);
          reads.priorities.push(`${label}#${priority}`);
          return { separable: true };
        }),
      staleTime: Infinity,
    }),
  };
});

/** Every read the queue made, in order, as `kind:number` or `kind:number@revision`. */
const reads = vi.hoisted(() => ({
  log: [] as string[],
  priorities: [] as string[],
  native: true,
  /** Labels whose answer waits until `release` is called. */
  held: new Set<string>(),
  waiting: [] as { label: string; release: () => void }[],
}));
/** Answer `label` now, or once the test releases it. */
const answerWhenReleased = async (label: string) => {
  const kind = label.split(":")[0]!;
  if (!reads.held.has(kind)) return;
  await new Promise<void>((resolve) => {
    reads.waiting.push({ label, release: resolve });
  });
};
const numberOf = (url: string) => Number(url.split("/").pop());

vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => {
  const original = await importOriginal<typeof UsePullRequestsModule>();
  const { readAtObservedPriority } = await import("@/hooks/pullRequestReadPriority");
  // As the real reads take it: their priority from who wants them, recorded with each.
  const read = (key: unknown[], label: string, answer: () => unknown) => ({
    queryKey: key,
    queryFn: ({ client: queryClient }: { client: QueryClient }) =>
      readAtObservedPriority(queryClient, key, async (priority) => {
        reads.log.push(label);
        reads.priorities.push(`${label}#${priority}`);
        await answerWhenReleased(label);
        return answer();
      }),
    // As the real details read gives it: a function of the query, not a number.
    staleTime: () => 30_000,
  });
  return {
    ...original,
    useDesktopPullRequestReads: () => ({
      known: () => true,
      readsOnThisMac: () => true,
      blocked: () => false,
      target: (url: string) => ({
        provider: "github",
        host: "github.com",
        repository: "acme/app",
        number: numberOf(url),
      }),
      scope: () => ["mac-read", "user", "github", "github.com"],
    }),
    pullRequestInfoReadAheadOptions: (conversationId: string, url: string) =>
      read(
        ["github-info", conversationId, url],
        `details:${numberOf(url)}@${conversationId}`,
        () => ({
          pr: {
            url,
            head_sha: `head-${numberOf(url)}`,
            base_sha: "base",
            base_ref: "main",
            body: stackBody(numberOf(url)),
          },
        }),
      ),
    pullRequestChangedFilesQueryOptions: (_c: string, url: string, revision: string) =>
      read(["files", url, revision], `files:${numberOf(url)}@${revision}`, () => ({ data: [] })),
    pullRequestDiffQueryOptions: (_c: string, url: string, revision: string) =>
      read(["patch", url, revision], `patch:${numberOf(url)}@${revision}`, () => ({ patch: "" })),
  };
});
vi.mock("@/hooks/usePullRequestNativeStack", async () => {
  const { readAtObservedPriority } = await import("@/hooks/pullRequestReadPriority");
  return {
    pullRequestNativeStackQueryOptions: (url: string, revision: string) => ({
      readable: reads.native,
      options: {
        queryKey: ["native", url, revision],
        queryFn: ({ client: queryClient }: { client: QueryClient }) =>
          readAtObservedPriority(queryClient, ["native", url, revision], async (priority) => {
            const label = `native:${numberOf(url)}@${JSON.parse(revision)[0]}`;
            reads.log.push(label);
            reads.priorities.push(`${label}#${priority}`);
            await answerWhenReleased(label);
            return { read: undefined, members: null };
          }),
        staleTime: 30_000,
      },
    }),
  };
});

const urlOf = (number: number) => `https://github.com/acme/app/pull/${number}`;

// The shared members read of each member's description, as its view would read it.
vi.mock("@/hooks/usePullRequestStackMembers", async (importOriginal) => {
  const { readAtObservedPriority } = await import("@/hooks/pullRequestReadPriority");
  return {
    ...(await importOriginal<typeof StackMembersHooks>()),
    pullRequestStackMembersQueryOptions: (url: string) => ({
      wanted: true,
      options: {
        queryKey: ["members", url],
        queryFn: ({ client: queryClient }: { client: QueryClient }) =>
          readAtObservedPriority(queryClient, ["members", url], async (priority) => {
            reads.log.push(`members:${numberOf(url)}`);
            reads.priorities.push(`members:${numberOf(url)}#${priority}`);
            return { viewer: "octo", members: [], missing: [], unread: [] };
          }),
        staleTime: 30_000,
      },
    }),
  };
});

/** A stack description of PRs 100 to 104, base first, as `self`'s. */
function stackBody(self: number) {
  return [
    "## Stacked changes",
    ...[100, 101, 102, 103, 104].map((number, depth) => {
      const label = number === self ? `**stack/${number}**` : `stack/${number}`;
      const url = `https://github.com/acme/app/pull/${number}`;
      return `${"  ".repeat(depth)}- [${label}](${url})${number === self ? " ← _this PR_" : ""}`;
    }),
    "",
    "---------",
    "The change.",
  ].join("\n");
}

/** A straight stack of `numbers`, base first, as `self`'s view shows it. */
function viewOf(numbers: number[], self: number) {
  const nodes: StackNode[] = numbers.map((number, index) => ({
    depth: index,
    parentIndex: index === 0 ? null : index - 1,
    branch: `stack/${number}`,
    ref: { url: urlOf(number), host: "github.com", repository: "acme/app", number },
    external: false,
    markers: { current: number === self, merged: false },
  }));
  const parse = {
    format: "example-stack",
    nodes,
    dependency: null,
    selfIndex: numbers.indexOf(self),
    trust: "tolerated",
    tolerances: [],
  } as StackParse;
  return pullRequestStackView(parse, { kind: "unavailable" }, () => true);
}

let client: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>{children}</QueryClientProvider>
);
let visibility: DocumentVisibilityState = "visible";

beforeEach(() => {
  vi.useFakeTimers();
  client = new QueryClient();
  reads.log = [];
  reads.priorities = [];
  resetReadPrioritiesForTests();
  reads.native = true;
  reads.held = new Set();
  reads.waiting = [];
  comparing.wanted = new Set();
  flags.incremental = true;
  flags.automatic = true;
  resetStackWarmupQueues();
  flags.warmup = true;
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** Let the queue run for `ms`, settling each read as it goes. */
async function run(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

const warm = (
  props: Partial<Parameters<typeof usePullRequestStackWarmup>[0]> = {},
  numbers = [100, 101, 102],
  self = 101,
) =>
  renderHook(
    (current: Partial<Parameters<typeof usePullRequestStackWarmup>[0]>) =>
      usePullRequestStackWarmup({
        stack: viewOf(numbers, self),
        selfUrl: urlOf(self),
        active: true,
        sessionFor: () => "pull-requests-page",
        ...current,
      }),
    { wrapper, initialProps: props },
  );

describe.each([
  ["GitHub's own stack", true],
  ["a description's stack", false],
])("usePullRequestStackWarmup, %s", (_name, native) => {
  beforeEach(() => {
    reads.native = native;
  });

  it("reads every member's details and stack first, nearest first, then their changes", async () => {
    warm();
    await run(10_000);
    const details = native
      ? [
          "details:100@pull-requests-page",
          "native:100@head-100",
          "details:102@pull-requests-page",
          "native:102@head-102",
        ]
      : ["details:100@pull-requests-page", "details:102@pull-requests-page"];
    expect(reads.log).toEqual([
      ...details,
      "files:100@base:head-100",
      "patch:100@base:head-100",
      "files:102@base:head-102",
      "patch:102@base:head-102",
    ]);
  });

  it("reads each member where the page would read it", async () => {
    warm({ sessionFor: (url) => (url === urlOf(100) ? "session-1" : "pull-requests-page") });
    await run(10_000);
    expect(reads.log).toContain("details:100@session-1");
    expect(reads.log).toContain("details:102@pull-requests-page");
  });

  it("reads nothing already fresh again, as when coming back to the stack", async () => {
    const firstView = warm();
    await run(10_000);
    firstView.unmount();
    const first = reads.log.length;
    warm({}, [100, 101, 102], 102);
    await run(10_000);
    // Only PR 101, the one open before, is new; the rest were read moments ago.
    expect(reads.log.slice(first).every((entry) => entry.includes(":101"))).toBe(true);
  });
});

/** Answer every read of `label` that's waiting. */
async function release(label: string) {
  for (const waiting of reads.waiting.filter((each) => each.label.startsWith(label))) {
    waiting.release();
  }
  reads.waiting = reads.waiting.filter((each) => !each.label.startsWith(label));
  await run(0);
}

describe.each([
  ["GitHub's own stack", true],
  ["a description's stack", false],
])("usePullRequestStackWarmup with details that answer late, %s", (_name, native) => {
  beforeEach(() => {
    reads.native = native;
  });

  it("still reads the late member's changes once its details answer", async () => {
    reads.held.add("details");
    warm({}, [100, 101, 102], 101);
    await run(2_000);
    await release("details:100");
    // PR 102's details are still out while PR 100's own reads go on.
    await run(10_000);
    expect(reads.log).toContain("files:100@base:head-100");
    expect(reads.log).not.toContain("files:102@base:head-102");
    await release("details:102");
    await run(10_000);
    expect(reads.log).toContain("files:102@base:head-102");
    expect(reads.log).toContain("patch:102@base:head-102");
    if (native) expect(reads.log).toContain("native:102@head-102");
  });
});

describe.each([
  ["GitHub's own stack", true],
  ["a description's stack", false],
])("usePullRequestStackWarmup reading comparisons, %s", (_name, native) => {
  beforeEach(() => {
    reads.native = native;
  });

  it("reads the comparison only of a member whose Changes would show one, after its stack", async () => {
    comparing.wanted.add(102);
    warm({ comparesStacks: true });
    await run(20_000);
    expect(reads.log).toContain("compare:102");
    expect(reads.log).not.toContain("compare:100");
    // GitHub's answer first where it's read, then the description's members, then the comparison.
    const before = [
      ...(native ? ["native:102@head-102"] : []),
      "details:102@pull-requests-page",
      "members:102",
    ];
    for (const entry of before) {
      expect(reads.log.indexOf("compare:102")).toBeGreaterThan(reads.log.indexOf(entry));
    }
  });

  it("reads none where Changes can't show a PR's own changes, or with that flag off", async () => {
    comparing.wanted.add(102);
    warm({ comparesStacks: false });
    await run(20_000);
    expect(reads.log.some((entry) => entry.startsWith("compare"))).toBe(false);
    cleanup();
    reads.log = [];
    resetStackWarmupQueues();
    client = new QueryClient();
    flags.incremental = false;
    warm({ comparesStacks: true });
    await run(20_000);
    expect(reads.log.some((entry) => entry.startsWith("compare"))).toBe(false);
  });

  it("reads no comparison already read for those exact inputs", async () => {
    comparing.wanted.add(102);
    client.setQueryData(["compare", urlOf(102)], { separable: true });
    warm({ comparesStacks: true });
    await run(20_000);
    expect(reads.log.some((entry) => entry.startsWith("compare"))).toBe(false);
  });
});

describe("usePullRequestStackWarmup with members read moments ago", () => {
  const seedDetails = (numbers: number[], updatedAt = Date.now()) => {
    for (const number of numbers) {
      client.setQueryData(
        ["github-info", "pull-requests-page", urlOf(number)],
        {
          pr: {
            url: urlOf(number),
            head_sha: `head-${number}`,
            base_sha: "base",
            base_ref: "main",
          },
        },
        { updatedAt },
      );
    }
  };

  it("spends no paced slot on fresh details, so the first change read goes at once", async () => {
    reads.native = false;
    seedDetails([100, 102, 103]);
    warm({}, [100, 101, 102, 103], 101);
    await run(100);
    expect(reads.log).toEqual(["files:100@base:head-100"]);
    await run(10_000);
    expect(reads.log.filter((entry) => entry.startsWith("details"))).toEqual([]);
  });

  it("reads details again once they're older than their staleness, or invalidated", async () => {
    reads.native = false;
    seedDetails([100], Date.now() - 31_000);
    seedDetails([102]);
    await client.invalidateQueries({ queryKey: ["github-info", "pull-requests-page", urlOf(102)] });
    warm();
    await run(10_000);
    expect(reads.log).toContain("details:100@pull-requests-page");
    expect(reads.log).toContain("details:102@pull-requests-page");
  });
});

describe("usePullRequestStackWarmup across views of one reader", () => {
  it("counts reads still under way when moving to another member", async () => {
    reads.held.add("details");
    const first = warm({}, [100, 101, 102, 103, 104], 100);
    await run(600);
    expect(reads.log).toHaveLength(2);
    // Moving within the stack: this view goes, the next one starts with both still out.
    first.unmount();
    warm({}, [100, 101, 102, 103, 104], 104);
    await run(10_000);
    expect(reads.log).toHaveLength(2);
    await release("details:101");
    await run(400);
    expect(reads.log).toHaveLength(3);
  });
});

describe("usePullRequestStackWarmup, when it reads", () => {
  it("stops starting reads when the window hides, and starts again when it shows", async () => {
    warm({}, [100, 101, 102, 103, 104, 105], 100);
    await run(600);
    const beforeHide = reads.log.length;
    expect(beforeHide).toBeGreaterThan(0);
    visibility = "hidden";
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await run(10_000);
    expect(reads.log.length - beforeHide).toBeLessThanOrEqual(2);
    const hiddenCount = reads.log.length;
    visibility = "visible";
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await run(20_000);
    expect(reads.log.length).toBeGreaterThan(hiddenCount);
  });

  it("stops when the PR leaves the stack, or its view goes inactive", async () => {
    const view = warm({}, [100, 101, 102, 103, 104, 105], 100);
    await run(600);
    view.rerender({ stack: null });
    const left = reads.log.length;
    await run(10_000);
    expect(reads.log.length - left).toBeLessThanOrEqual(2);
    cleanup();
    reads.log = [];
    warm({ active: false });
    await run(10_000);
    expect(reads.log).toEqual([]);
  });

  it("reads nothing with the rollback switch off", async () => {
    flags.warmup = false;
    warm();
    await run(10_000);
    expect(reads.log).toEqual([]);
  });
});

describe("usePullRequestStackWarmup, moving a member of interest ahead", () => {
  const STACK = [100, 101, 102, 103, 104, 105];
  const onTab = (subtab: "changes" | "summary") => ({ ...viewOf(STACK, 100), subtab });
  const at = (label: string) => reads.log.findIndex((entry) => entry.startsWith(label));

  it("reads the farthest member's Changes before the others' details, on Changes", async () => {
    const view = warm({ stack: onTab("changes") }, STACK, 100);
    expect(view.result.current(urlOf(105))).not.toBeNull();
    await run(30_000);
    expect(at("patch:105")).toBeGreaterThan(-1);
    expect(at("patch:105")).toBeLessThan(at("details:104"));
    expect(at("files:105")).toBeLessThan(at("details:104"));
  });

  it("moves only the member's details ahead where members open on their Overview", async () => {
    const view = warm({ stack: onTab("summary") }, STACK, 100);
    view.result.current(urlOf(105));
    await run(30_000);
    expect(at("details:105")).toBe(0);
    expect(at("patch:105")).toBeGreaterThan(at("patch:104"));
  });

  it("reads the member's comparison ahead too, where Changes would show one", async () => {
    // The members' descriptions list PRs 100 to 104.
    const described = [100, 101, 102, 103, 104];
    comparing.wanted = new Set([104]);
    const stack = { ...viewOf(described, 100), subtab: "changes" as const };
    const view = warm({ stack, comparesStacks: true }, described, 100);
    view.result.current(urlOf(104));
    await run(30_000);
    expect(at("compare:104")).toBeGreaterThan(-1);
    expect(at("compare:104")).toBeLessThan(at("details:103"));
  });

  it("makes the same reads as without interest, superseded or repeated", async () => {
    warm({ stack: onTab("changes") }, STACK, 100);
    await run(60_000);
    const without = [...reads.log].sort();
    cleanup();
    client = new QueryClient();
    resetStackWarmupQueues();
    reads.log = [];
    const view = warm({ stack: onTab("changes") }, STACK, 100);
    view.result.current(urlOf(105))?.();
    view.result.current(urlOf(103));
    view.result.current(urlOf(105));
    await run(60_000);
    expect([...reads.log].sort()).toEqual(without);
  });

  it("moves nothing for this PR or one outside the stack, or while nothing reads ahead", async () => {
    const view = warm({ stack: onTab("changes") }, STACK, 100);
    expect(view.result.current(urlOf(100))).toBeNull();
    expect(view.result.current(urlOf(999))).toBeNull();
    view.rerender({ stack: onTab("changes"), active: false });
    expect(view.result.current(urlOf(105))).toBeNull();
    cleanup();
    flags.warmup = false;
    const off = warm({ stack: onTab("changes") }, STACK, 100);
    expect(off.result.current(urlOf(105))).toBeNull();
    await run(10_000);
    expect(reads.log).toEqual([]);
  });
});

describe("usePullRequestStackWarmup's place among the shell's reads", () => {
  it.each([
    ["GitHub's own stack", true],
    ["the description's members", false],
  ])("reads every member ahead as prefetch, %s and comparisons too", async (_name, native) => {
    reads.native = native;
    comparing.wanted = new Set([100, 102]);
    warm({ comparesStacks: true });
    await run(20_000);
    const kinds = new Set(reads.priorities.map((each) => each.split(":")[0]));
    // GitHub's answer doesn't stack them here, so the description's members are read too.
    expect(kinds).toEqual(
      new Set(["details", ...(native ? ["native"] : []), "members", "files", "patch", "compare"]),
    );
    expect(reads.priorities.filter((each) => !each.endsWith("#prefetch"))).toEqual([]);
  });

  it("keeps counting a read ahead whose query was cancelled until the shell answers it", async () => {
    reads.native = false;
    reads.held = new Set(["details"]);
    warm();
    await run(1_000);
    const queue = stackWarmupQueueFor(null);
    expect(queue.inFlight()).toBeGreaterThan(0);
    const before = queue.inFlight();
    await act(async () => {
      await client.cancelQueries();
    });
    await run(1_000);
    // The shell hasn't answered: the slot stays taken, and nothing new goes out in its place.
    expect(queue.inFlight()).toBe(before);
    const sent = reads.log.length;
    await run(5_000);
    expect(reads.log.length).toBe(sent);
    for (const waiting of reads.waiting.splice(0)) waiting.release();
    reads.held = new Set();
    await run(1_000);
    expect(queue.inFlight()).toBeLessThan(before);
  });
});
