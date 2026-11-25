import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useDesktopPullRequestReads } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  pullRequestStackMembersQueryKey,
  refreshPullRequestStackMembers,
  STACK_MEMBERS_POLL_MS,
  stackMembersPollInterval,
  usePullRequestStackMembers,
} from "@/hooks/usePullRequestStackMembers";
import { keyHasScope } from "@/lib/macReadScopeQueries";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import type * as DesktopReadPathModule from "@/lib/desktopReadPath";
import type * as StackMembersModule from "@/lib/pullRequestStacks/stackMembers";
import { macOffers } from "@/lib/desktopReadPath";
import type { StackNode, StackParse } from "@/lib/pullRequestStacks/stackModel";
import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Stack descriptions in an example format, which the page reads once a build registers it.
withPullRequestIntegrations({ stackFormats: [EXAMPLE_STACK_FORMAT] });

vi.mock("@/newPullRequestPage/hooks/usePullRequests", () => ({
  useDesktopPullRequestReads: vi.fn(),
}));
vi.mock("@/lib/desktopReadPath", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopReadPathModule>()),
  macOffers: vi.fn(),
}));
// A plain function rather than a mock, so a failed read settles only where the query awaits it.
const reads = vi.hoisted(() => ({
  calls: [] as unknown[][],
  answer: (): Promise<unknown> => Promise.resolve(null),
}));

vi.mock("@/lib/pullRequestStacks/stackMembers", async (importOriginal) => ({
  ...(await importOriginal<typeof StackMembersModule>()),
  fetchStackMembers: (...args: unknown[]) => {
    reads.calls.push(args);
    return reads.answer();
  },
}));

const URL_102 = "https://git.example.com/acme/app/pull/102";
const TARGET: CodeHostPullRequest = {
  provider: "github",
  host: "git.example.com",
  repository: "acme/app",
  number: 102,
  url: URL_102,
};
const ANSWER = { viewer: "octo", members: [], missing: [], unread: [] };

const node = (number: number): StackNode => ({
  depth: 0,
  parentIndex: null,
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
const parseOf = (numbers: number[], selfIndex: number): StackParse => ({
  format: "test",
  trust: "tolerated",
  tolerances: [],
  nodes: numbers.map(node),
  dependency: null,
  selfIndex,
});
const STACK = parseOf([100, 102], 1);
const REVISION = { head_sha: "head-1", base_ref: "main", base_sha: "base-1" };

interface Shell {
  readsOnThisMac?: boolean;
  offers?: boolean;
  scope?: readonly unknown[];
}

function useShell({
  readsOnThisMac = true,
  offers = true,
  scope = ["mac-read", "user", "github", "git.example.com"],
}: Shell) {
  vi.mocked(useDesktopPullRequestReads).mockReturnValue({
    known: () => true,
    readsOnThisMac: () => readsOnThisMac,
    target: () => (readsOnThisMac ? TARGET : null),
    scope: () => scope,
    blocked: () => false,
  });
  vi.mocked(macOffers).mockReturnValue(offers);
}

function wrapper(client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

/** Reads that settle only when the test says, in the order they were sent. */
function heldReads() {
  const pending: ((answer: unknown) => void)[] = [];
  reads.answer = () =>
    new Promise((resolve) => {
      pending.push(resolve);
    });
  return pending;
}

const memberAt = (number: number, state: "OPEN" | "MERGED") => ({
  url: `https://git.example.com/acme/app/pull/${number}`,
  number,
  title: `PR ${number}`,
  state,
  is_draft: false,
  updated_at: null,
  review_decision: null,
  head_ref: `stack/${number}`,
  head_sha: null,
  base_ref: "main",
  base_sha: null,
  head_repository: "acme/app",
  base_repository: "acme/app",
});
const answerWithParent = (state: "OPEN" | "MERGED") => ({
  ...ANSWER,
  members: [memberAt(100, state)],
});

const answerWith = (answer: unknown) => {
  reads.answer = () => Promise.resolve(answer);
};

beforeEach(() => {
  reads.calls = [];
});
afterEach(cleanup);

describe("usePullRequestStackMembers", () => {
  it("reads the stack on a shell that offers the read, as the PR reads", async () => {
    useShell({});
    answerWith(ANSWER);
    const { result } = renderHook(() => usePullRequestStackMembers(URL_102, STACK, REVISION), {
      wrapper: wrapper(),
    });
    expect(result.current.kind).toBe("loading");
    await waitFor(() => expect(result.current).toEqual({ kind: "loaded", answer: ANSWER }));
    // The view's own read: one the user asked for, let go of with its query.
    expect(reads.calls).toEqual([
      [TARGET, [102, 100], { priority: "interactive", signal: expect.any(AbortSignal) }],
    ]);
  });

  it("doesn't read on an older shell without the read", () => {
    useShell({ offers: false });
    const { result } = renderHook(() => usePullRequestStackMembers(URL_102, STACK, REVISION), {
      wrapper: wrapper(),
    });
    expect(result.current.kind).toBe("unavailable");
    expect(reads.calls).toEqual([]);
  });

  it("doesn't read a PR this Mac doesn't read", () => {
    useShell({ readsOnThisMac: false });
    const { result } = renderHook(() => usePullRequestStackMembers(URL_102, STACK, REVISION), {
      wrapper: wrapper(),
    });
    expect(result.current.kind).toBe("unavailable");
    expect(reads.calls).toEqual([]);
  });

  it("reads a stack of one for its own base, which landing advice needs", async () => {
    useShell({});
    answerWith(ANSWER);
    const verified = {
      ...parseOf([102], 0),
      format: "example-stack",
      trust: "verified",
      span: { start: 0, end: 0 },
    } as unknown as StackParse;
    const { result } = renderHook(() => usePullRequestStackMembers(URL_102, verified, REVISION), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current).toEqual({ kind: "loaded", answer: ANSWER }));
    expect(reads.calls).toEqual([
      [TARGET, [102], { priority: "interactive", signal: expect.any(AbortSignal) }],
    ]);
  });

  it("doesn't read a stack of one, or no stack", () => {
    useShell({});
    renderHook(() => usePullRequestStackMembers(URL_102, parseOf([102], 0), REVISION), {
      wrapper: wrapper(),
    });
    renderHook(() => usePullRequestStackMembers(URL_102, null, REVISION), { wrapper: wrapper() });
    expect(reads.calls).toEqual([]);
  });

  it("shows the stack without statuses when a partial shell refuses the read", async () => {
    useShell({});
    answerWith(null);
    const { result } = renderHook(() => usePullRequestStackMembers(URL_102, STACK, REVISION), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.kind).toBe("unavailable"));
  });

  it("reports a failed read", async () => {
    useShell({});
    reads.answer = () => Promise.reject(new Error("offline"));
    const { result } = renderHook(() => usePullRequestStackMembers(URL_102, STACK, REVISION), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.kind).toBe("failed"));
  });

  it("reads again under another reader's scope rather than showing the first one's answer", async () => {
    useShell({ scope: ["mac-read-as", "user", "github", "git.example.com", "alice"] });
    answerWith(ANSWER);
    const { result, rerender } = renderHook(
      () => usePullRequestStackMembers(URL_102, STACK, REVISION),
      {
        wrapper: wrapper(),
      },
    );
    await waitFor(() => expect(result.current.kind).toBe("loaded"));
    useShell({ scope: ["mac-read-as", "user", "github", "git.example.com", "bob"] });
    let settle: (answer: unknown) => void = () => {};
    reads.answer = () =>
      new Promise((resolve) => {
        settle = resolve;
      });
    rerender();
    expect(result.current.kind).toBe("loading");
    expect(reads.calls).toHaveLength(2);
    settle(ANSWER);
    await waitFor(() => expect(result.current.kind).toBe("loaded"));
  });

  it("caches under the whole read scope, so a change of reader drops it", () => {
    const scope = ["mac-read-as", "user", "github", "git.example.com", "alice"];
    const key = pullRequestStackMembersQueryKey(scope, "described:example-stack:1", [102, 100]);
    expect(keyHasScope(key, scope)).toBe(true);
    expect(keyHasScope(key, [...scope.slice(0, -1), "bob"])).toBe(false);
  });

  it("shares one read among the stack's members, whichever member asks", async () => {
    useShell({});
    answerWith(ANSWER);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const first = renderHook(() => usePullRequestStackMembers(URL_102, STACK, REVISION), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(first.result.current.kind).toBe("loaded"));
    // The parent, read as itself, lists the same stack.
    const parent = renderHook(
      () =>
        usePullRequestStackMembers(
          "https://git.example.com/acme/app/pull/100",
          parseOf([100, 102], 0),
          REVISION,
        ),
      { wrapper: wrapper(client) },
    );
    expect(parent.result.current).toEqual({ kind: "loaded", answer: ANSWER });
    expect(reads.calls).toHaveLength(1);
  });

  it("reads again once when the PR's own details show a head the shared read doesn't", async () => {
    useShell({});
    const self = { ...memberAt(102, "OPEN"), head_sha: "head-1", base_sha: "base-1" };
    answerWith({ ...ANSWER, members: [self] });
    let revision = REVISION;
    const { result, rerender } = renderHook(
      () => usePullRequestStackMembers(URL_102, STACK, revision),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.kind).toBe("loaded"));
    expect(reads.calls).toHaveLength(1);
    // Pushed: the old read still says head-1, and GitHub keeps saying so for a moment.
    revision = { ...REVISION, head_sha: "head-2" };
    rerender();
    await waitFor(() => expect(reads.calls).toHaveLength(2));
    rerender();
    expect(reads.calls).toHaveLength(2);
    // A retarget alone, head unchanged, reads again too.
    answerWith({ ...ANSWER, members: [{ ...self, head_sha: "head-2" }] });
    revision = { ...revision, base_ref: "release" };
    rerender();
    await waitFor(() => expect(reads.calls).toHaveLength(3));
  });

  it("shows a parent's new state after a refresh, with the same members", async () => {
    useShell({});
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    answerWith(answerWithParent("OPEN"));
    const { result } = renderHook(() => usePullRequestStackMembers(URL_102, STACK, REVISION), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.kind).toBe("loaded"));
    answerWith(answerWithParent("MERGED"));
    await act(() => refreshPullRequestStackMembers(client));
    await waitFor(() =>
      expect(result.current).toEqual({ kind: "loaded", answer: answerWithParent("MERGED") }),
    );
    expect(reads.calls).toHaveLength(2);
  });

  it("never lets an older read that lands late replace a newer one", async () => {
    useShell({});
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    answerWith(answerWithParent("OPEN"));
    const { result } = renderHook(() => usePullRequestStackMembers(URL_102, STACK, REVISION), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(result.current.kind).toBe("loaded"));
    const pending = heldReads();
    // A read in the background, then a Refresh that overtakes it.
    void refreshPullRequestStackMembers(client);
    await waitFor(() => expect(pending).toHaveLength(1));
    void refreshPullRequestStackMembers(client);
    await waitFor(() => expect(pending).toHaveLength(2));
    await act(async () => pending[1]?.(answerWithParent("MERGED")));
    await waitFor(() =>
      expect(result.current).toEqual({ kind: "loaded", answer: answerWithParent("MERGED") }),
    );
    await act(async () => pending[0]?.({ ...answerWithParent("OPEN"), viewer: "stale" }));
    // Long enough for a late answer's notification to land, were it kept.
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(result.current).toEqual({ kind: "loaded", answer: answerWithParent("MERGED") });
  });

  it("polls only while a member is open", () => {
    expect(stackMembersPollInterval(answerWithParent("OPEN"))).toBe(STACK_MEMBERS_POLL_MS);
    expect(stackMembersPollInterval(answerWithParent("MERGED"))).toBe(false);
    expect(stackMembersPollInterval(null)).toBe(false);
  });
});
