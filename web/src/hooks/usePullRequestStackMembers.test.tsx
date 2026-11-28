import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  refreshPullRequestStackMembers,
  STACK_MEMBERS_POLL_MS,
  stackMembersPollInterval,
  usePullRequestStackMembers,
} from "@/hooks/usePullRequestStackMembers";
import type * as StackMembersModule from "@/lib/pullRequestStacks/stackMembers";
import type { StackNode, StackParse } from "@/lib/pullRequestStacks/stackModel";
import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Stack descriptions in an example format, which the page reads once a build registers it.
withPullRequestIntegrations({ stackFormats: [EXAMPLE_STACK_FORMAT] });

// A plain function rather than a mock, so a failed read settles only where the query awaits it.
const reads = vi.hoisted(() => ({
  serves: true,
  calls: [] as unknown[][],
  answer: (): Promise<unknown> => Promise.resolve(null),
}));

vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (session: string, method: string) =>
    reads.serves && session !== "" && method === "getPullRequestStack",
}));
vi.mock("@/lib/pullRequestStacks/stackMembers", async (importOriginal) => ({
  ...(await importOriginal<typeof StackMembersModule>()),
  fetchStackMembersOnHost: (...args: unknown[]) => {
    reads.calls.push(args);
    return reads.answer();
  },
}));

const URL_102 = "https://git.example.com/acme/app/pull/102";
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

function useShell({ serves = true }: { serves?: boolean }) {
  reads.serves = serves;
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
  it("reads the stack through the session's host where it serves the stack", async () => {
    useShell({});
    answerWith(ANSWER);
    const { result } = renderHook(
      () => usePullRequestStackMembers("s1", URL_102, STACK, REVISION),
      {
        wrapper: wrapper(),
      },
    );
    expect(result.current.kind).toBe("loading");
    await waitFor(() => expect(result.current).toEqual({ kind: "loaded", answer: ANSWER }));
    expect(reads.calls).toEqual([["s1", URL_102, "acme/app", [102, 100]]]);
  });

  it("reads nothing, and shows no statuses, where the host doesn't serve the stack", () => {
    useShell({ serves: false });
    const { result } = renderHook(
      () => usePullRequestStackMembers("s1", URL_102, STACK, REVISION),
      {
        wrapper: wrapper(),
      },
    );
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
    const { result } = renderHook(
      () => usePullRequestStackMembers("s1", URL_102, verified, REVISION),
      {
        wrapper: wrapper(),
      },
    );
    await waitFor(() => expect(result.current).toEqual({ kind: "loaded", answer: ANSWER }));
    expect(reads.calls).toEqual([["s1", URL_102, "acme/app", [102]]]);
  });

  it("doesn't read a stack of one, or no stack", () => {
    useShell({});
    renderHook(() => usePullRequestStackMembers("s1", URL_102, parseOf([102], 0), REVISION), {
      wrapper: wrapper(),
    });
    renderHook(() => usePullRequestStackMembers("s1", URL_102, null, REVISION), {
      wrapper: wrapper(),
    });
    expect(reads.calls).toEqual([]);
  });

  it("reports a failed read", async () => {
    useShell({});
    reads.answer = () => Promise.reject(new Error("offline"));
    const { result } = renderHook(
      () => usePullRequestStackMembers("s1", URL_102, STACK, REVISION),
      {
        wrapper: wrapper(),
      },
    );
    await waitFor(() => expect(result.current.kind).toBe("failed"));
  });

  it("shares one read among the stack's members, whichever member asks", async () => {
    useShell({});
    answerWith(ANSWER);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const first = renderHook(() => usePullRequestStackMembers("s1", URL_102, STACK, REVISION), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(first.result.current.kind).toBe("loaded"));
    // The parent, read as itself, lists the same stack.
    const parent = renderHook(
      () =>
        usePullRequestStackMembers(
          "s1",
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
      () => usePullRequestStackMembers("s1", URL_102, STACK, revision),
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
    const { result } = renderHook(
      () => usePullRequestStackMembers("s1", URL_102, STACK, REVISION),
      {
        wrapper: wrapper(client),
      },
    );
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
    const { result } = renderHook(
      () => usePullRequestStackMembers("s1", URL_102, STACK, REVISION),
      {
        wrapper: wrapper(client),
      },
    );
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
