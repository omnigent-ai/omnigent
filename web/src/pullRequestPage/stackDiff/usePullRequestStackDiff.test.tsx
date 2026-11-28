import {
  notifyManager,
  QueryClient,
  QueryClientProvider,
  QueryObserver,
} from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as StackCompareModule from "@/lib/pullRequestStacks/stackCompare";
import { descriptionDigest, StackCompareChangedError } from "@/lib/pullRequestStacks/stackCompare";
import type { StackMember } from "@/lib/pullRequestStacks/stackMembers";
import type { StackNode, StackParse } from "@/lib/pullRequestStacks/stackModel";
import { refreshPullRequest } from "@/pullRequestPage/refreshPullRequest";
import { pullRequestStackView } from "@/pullRequestPage/usePullRequestStack";
import { stackDiffIntentMemory } from "./stackDiffIntent";
import { CHILD_HEAD, comparison, NATIVE_BASE, PARENT_HEAD } from "./stackDiffFixtures";
import {
  PULL_REQUEST_STACK_INCREMENTAL_DIFF_FLAG,
  STACK_DIFF_AUTOMATIC_POLICY_FLAG,
  type StackDiffPullRequest,
  type StackDiffSurface,
  usePullRequestStackDiff,
} from "./usePullRequestStackDiff";

const shell = vi.hoisted(() => ({
  flags: new Map<string, boolean>(),
  offers: true,
  calls: [] as unknown[][],
  answer: (): Promise<unknown> => Promise.resolve(null),
  digest: null as null | ((body: string) => Promise<string>),
  members: (): Promise<void> => Promise.resolve(),
  membersRefreshed: 0,
  invalidateMembers: true,
}));

vi.mock("@/hooks/useDesktopFeatureFlag", async () =>
  (await import("@/test/desktopFeatureFlagMock")).desktopFeatureFlagModule((name, fallback) => ({
    value: shell.flags.get(name) ?? fallback,
    resolved: true,
  })),
);
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (session: string, method: string) =>
    shell.offers && session === "s1" && method === "comparePullRequestRange",
}));
vi.mock("@/hooks/usePullRequestStackMembers", () => ({
  STACK_MEMBERS_POLL_MS: 60_000,
  usePullRequestStackMembers: () => ({ kind: "unavailable" }),
  refreshPullRequestStackMembers: (client: QueryClient) => {
    shell.membersRefreshed += 1;
    return shell.invalidateMembers
      ? Promise.all([shell.members(), client.invalidateQueries({ queryKey: ["members"] })])
      : shell.members();
  },
  // Tests that hand the hook members of their own count them as just read.
  cachedStackMembersAnswers: (client: QueryClient) => {
    const cached = client.getQueriesData({ queryKey: ["members"] });
    return cached.length > 0 ? new Set(cached.map(([, answer]) => answer)) : { has: () => true };
  },
}));
vi.mock("@/lib/pullRequestStacks/stackCompare", async (importOriginal) => {
  const original = await importOriginal<typeof StackCompareModule>();
  return {
    ...original,
    descriptionDigest: (body: string) => (shell.digest ?? original.descriptionDigest)(body),
    fetchStackComparisonOnHost: (...args: unknown[]) => {
      shell.calls.push(args);
      return shell.answer();
    },
  };
});

const URL_102 = "https://git.example.com/acme/app/pull/102";
const BODY = "Stacked on #100";

const node = (number: number, parentIndex: number | null): StackNode => ({
  depth: parentIndex === null ? 0 : 1,
  parentIndex,
  branch: `stack/${number}`,
  ref: {
    url: `https://git.example.com/acme/app/pull/${number}`,
    host: "git.example.com",
    repository: "acme/app",
    number,
  },
  external: false,
  markers: { current: false, merged: false },
});

function stackOf({
  trust = "verified",
  parentNumber = 100,
  parent = {},
}: {
  trust?: "verified" | "tolerated" | "native";
  parentNumber?: number;
  parent?: Partial<StackMember>;
} = {}) {
  const parse = {
    format: "test",
    trust,
    span: { start: 0, end: 0 },
    tolerances: [],
    nodes: [node(parentNumber, null), node(102, 0)],
    dependency: null,
    selfIndex: 1,
  } as unknown as StackParse;
  const member: StackMember = {
    url: `https://git.example.com/acme/app/pull/${parentNumber}`,
    number: parentNumber,
    title: `PR ${parentNumber}`,
    state: "OPEN",
    is_draft: false,
    updated_at: null,
    review_decision: null,
    head_ref: `stack/${parentNumber}`,
    head_sha: PARENT_HEAD,
    base_ref: "main",
    base_sha: NATIVE_BASE,
    head_repository: "acme/app",
    base_repository: "acme/app",
    ...parent,
  };
  return pullRequestStackView(
    parse,
    { kind: "loaded", answer: { viewer: "alice", members: [member], missing: [], unread: [] } },
    () => false,
  );
}

const PR: StackDiffPullRequest = {
  url: URL_102,
  body: BODY,
  head_sha: CHILD_HEAD,
  base_sha: NATIVE_BASE,
  base_ref: "main",
};

interface Props {
  pr?: StackDiffPullRequest;
  stack?: ReturnType<typeof stackOf>;
  surface?: StackDiffSurface;
  diagnose?: boolean;
}

function renderStackDiff(
  initial: Props = {},
  reloadPullRequest?: (shown: StackDiffPullRequest) => Promise<StackDiffPullRequest | null>,
  prepare?: (client: QueryClient) => void,
) {
  // Reload reads the PR again, as the page shows it unless a test says otherwise.
  const shown = { pr: initial.pr ?? PR };
  const reload = (): Promise<StackDiffPullRequest | null> =>
    reloadPullRequest ? reloadPullRequest(shown.pr) : Promise.resolve(shown.pr);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  prepare?.(client);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  // Every value the hook rendered, so one that showed for a moment is seen too.
  const renders: ReturnType<typeof usePullRequestStackDiff>[] = [];
  const rendered = renderHook(
    ({ pr = PR, stack = stackOf(), surface = "changes", diagnose }: Props) => {
      shown.pr = pr;
      const diff = usePullRequestStackDiff({
        conversationId: "s1",
        pr,
        stack,
        surface,
        diagnose,
        reloadPullRequest: reload,
      });
      renders.push(diff);
      return diff;
    },
    { wrapper, initialProps: initial },
  );
  return { ...rendered, client, renders };
}

/** Long enough for a read the hook wrongly started to have been asked. */
const settle = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 20);
  });

/** A promise the test settles, and how. */
function held<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const answerWith = (data: unknown) => {
  shell.answer = () => Promise.resolve(data);
};
const refuseWith = (code: "pull_request_changed" | "query_failed") => {
  shell.answer = () =>
    Promise.reject(
      code === "pull_request_changed" ? new StackCompareChangedError() : new Error("Refused"),
    );
};

beforeEach(() => {
  shell.flags = new Map([
    [PULL_REQUEST_STACK_INCREMENTAL_DIFF_FLAG, true],
    [STACK_DIFF_AUTOMATIC_POLICY_FLAG, true],
  ]);
  shell.offers = true;
  shell.calls = [];
  shell.digest = null;
  shell.members = () => Promise.resolve();
  shell.membersRefreshed = 0;
  shell.invalidateMembers = true;
  answerWith(comparison({ viewer: "" }));
});
afterEach(async () => {
  cleanup();
  notifyManager.setScheduler((notify) => {
    setTimeout(notify, 0);
  });
  // A read a test left in flight lands before the next test counts its own.
  await settle();
  window.localStorage.clear();
  stackDiffIntentMemory.resetUnsaved();
});

describe("usePullRequestStackDiff", () => {
  it("compares with the parent the members read found, and shows only this PR's changes", async () => {
    const { result } = renderStackDiff();
    expect(result.current?.effective.kind).toBe("comparing");
    await waitFor(() => expect(result.current?.effective.kind).toBe("this-pr"));
    expect(result.current?.parent).toEqual({
      number: 100,
      label: "#100",
      headRepository: "acme/app",
    });
    expect(shell.calls).toEqual([
      [
        "s1",
        URL_102,
        {
          parentNumber: 100,
          parentRepository: "acme/app",
          parentOpen: true,
          childHead: CHILD_HEAD,
          nativeBase: NATIVE_BASE,
          parentHead: PARENT_HEAD,
          childBodyDigest: await descriptionDigest(BODY),
        },
      ],
    ]);
  });

  it("offers and asks nothing where the session's host can't compare", async () => {
    shell.offers = false;
    expect(renderStackDiff().result.current).toBeNull();
    await settle();
    expect(shell.calls).toEqual([]);
  });

  it("offers, reads and shows nothing with the flag off, and keeps what was chosen", async () => {
    const { result: before, unmount } = renderStackDiff();
    await waitFor(() => expect(before.current?.effective.kind).toBe("this-pr"));
    act(() => before.current?.choose("this-pr"));
    unmount();
    shell.calls = [];
    shell.flags.set(PULL_REQUEST_STACK_INCREMENTAL_DIFF_FLAG, false);
    expect(renderStackDiff().result.current).toBeNull();
    await settle();
    expect(shell.calls).toEqual([]);
    shell.flags.set(PULL_REQUEST_STACK_INCREMENTAL_DIFF_FLAG, true);
    expect(renderStackDiff().result.current?.intent).toBe("this-pr");
  });

  it("moves Automatic with the live policy, and keeps a manual choice", async () => {
    const { result, rerender } = renderStackDiff();
    await waitFor(() => expect(result.current?.effective.kind).toBe("this-pr"));
    shell.flags.set(STACK_DIFF_AUTOMATIC_POLICY_FLAG, false);
    rerender({});
    expect(result.current?.effective).toEqual({ kind: "all", fallback: null });
    act(() => result.current?.choose("this-pr"));
    expect(result.current?.effective.kind).toBe("this-pr");
  });
});

describe("usePullRequestStackDiff's failed reads", () => {
  it("never asks again on its own after a failure", async () => {
    refuseWith("query_failed");
    const { result, rerender } = renderStackDiff();
    await waitFor(() => expect(result.current?.effective.kind).toBe("all"));
    rerender({});
    await settle();
    expect(shell.calls).toHaveLength(1);
  });
});

describe("usePullRequestStackDiff on a refresh of the pull request", () => {
  const NEW_HEAD = "e".repeat(40);
  const SESSION = "conv_1";
  let stopObserving: (() => void) | null = null;

  /** The page's overview of the PR, which a refresh reads again, answering `next()`. */
  function overviewReads(
    client: QueryClient,
    next: () => StackDiffPullRequest | Promise<StackDiffPullRequest>,
  ) {
    const observer = new QueryObserver(client, {
      queryKey: ["pr-page-github-info", SESSION, URL_102],
      queryFn: async () => ({ pr: await next() }),
      initialData: { pr: PR },
      staleTime: Infinity,
    });
    stopObserving = observer.subscribe(() => {});
  }

  afterEach(() => {
    stopObserving?.();
    stopObserving = null;
  });

  async function pendingAfter(promise: Promise<unknown>, ms: number) {
    let done = false;
    void promise.then(() => (done = true));
    await act(
      () =>
        new Promise((resolve) => {
          setTimeout(resolve, ms);
        }),
    );
    return !done;
  }

  it("compares the refreshed head only once the page shows it, and settles on that comparison", async () => {
    const { result, rerender, client } = renderStackDiff();
    await waitFor(() => expect(result.current?.effective.kind).toBe("this-pr"));
    overviewReads(client, () => ({ ...PR, head_sha: NEW_HEAD }));
    const fresh = held<unknown>();
    shell.answer = () => fresh.promise;

    const refresh = refreshPullRequest(client, { conversationId: SESSION, prUrl: URL_102 });
    // The page still shows the old head: nothing is reset or compared for the old inputs.
    expect(await pendingAfter(refresh, 100)).toBe(true);
    expect(shell.calls).toHaveLength(1);
    expect(result.current?.effective.kind).toBe("this-pr");

    // React renders the refreshed head later; only then does the comparison of it start.
    rerender({ pr: { ...PR, head_sha: NEW_HEAD } });
    await waitFor(() => expect(shell.calls).toHaveLength(2));
    expect((shell.calls[1][2] as { childHead: string }).childHead).toBe(NEW_HEAD);
    expect(await pendingAfter(refresh, 50)).toBe(true);
    await act(async () => fresh.resolve(comparison({ childHead: NEW_HEAD })));
    expect(await refresh).toEqual({ kind: "ok" });
  });
});
