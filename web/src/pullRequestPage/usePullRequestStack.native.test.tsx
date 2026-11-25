import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useDesktopPullRequestReads } from "@/newPullRequestPage/hooks/usePullRequests";
import type { NativeStackAnswer } from "@/lib/pullRequestStacks/nativeStack";
import {
  fetchNativeStack,
  type NativeStackReadAnswer,
} from "@/lib/pullRequestStacks/nativeStackRead";
import type * as NativeStackReadModule from "@/lib/pullRequestStacks/nativeStackRead";

import { type StackedPullRequest, usePullRequestStack } from "./usePullRequestStack";

vi.mock("@/hooks/useDesktopFeatureFlag", async () =>
  (await import("@/test/desktopFeatureFlagMock")).desktopFeatureFlagModule(() => ({
    value: true,
    resolved: true,
  })),
);
vi.mock("@/newPullRequestPage/hooks/usePullRequests", () => ({
  useDesktopPullRequestReads: vi.fn(),
}));
vi.mock("@/lib/pullRequestStacks/nativeStackRead", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeStackReadModule>()),
  fetchNativeStack: vi.fn(),
  nativeStackOffered: () => true,
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
/** The same head and description, retargeted onto another parent. */
const RETARGETED: StackedPullRequest = { ...PR, base_ref: "stack/99", base_sha: "9".repeat(40) };

/** GitHub's stack with this PR stacked directly on `parent`. */
function stackedOn(parent: number): NativeStackReadAnswer {
  const member = (number: number) => ({
    url: url(number),
    number,
    state: "OPEN",
    isDraft: false,
    headRefName: `stack/${number}`,
  });
  const answer: NativeStackAnswer = {
    stackEntry: { position: 2 },
    stack: {
      id: "PRS_1",
      number: 1,
      size: 2,
      baseRefName: "main",
      entries: {
        hasNextPage: false,
        nodes: [
          { position: 1, pullRequest: member(parent) },
          { position: 2, pullRequest: member(102) },
        ],
      },
    },
  };
  return { viewer: "alice", answer };
}

function held<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const previousNumber = (stack: ReturnType<typeof usePullRequestStack>) =>
  stack?.previous.kind === "go" ? stack.previous.destination.node.ref?.number : null;

function renderStack() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return renderHook(({ pr }: { pr: StackedPullRequest }) => usePullRequestStack(pr, () => true), {
    wrapper,
    initialProps: { pr: PR },
  });
}

beforeEach(() => {
  vi.mocked(useDesktopPullRequestReads).mockImplementation(
    () =>
      ({
        known: () => true,
        readsOnThisMac: () => true,
        target: (target: string) => ({
          provider: "github",
          host: "github.com",
          repository: "acme/app",
          number: 102,
          url: target,
        }),
        scope: () => ["mac", "github.com", "alice"],
      }) as unknown as ReturnType<typeof useDesktopPullRequestReads>,
  );
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("usePullRequestStack with GitHub's stack", () => {
  it("reads GitHub's stack again when the PR is retargeted on the same head", async () => {
    const retargeted = held<NativeStackReadAnswer>();
    vi.mocked(fetchNativeStack)
      .mockResolvedValueOnce(stackedOn(101))
      .mockImplementationOnce(() => retargeted.promise);
    const { result, rerender } = renderStack();
    await waitFor(() => expect(previousNumber(result.current)).toBe(101));
    rerender({ pr: RETARGETED });
    // The old parent never shows for the new base while its stack is read.
    expect(result.current).toBeNull();
    expect(fetchNativeStack).toHaveBeenCalledTimes(2);
    await act(async () => retargeted.resolve(stackedOn(99)));
    await waitFor(() => expect(previousNumber(result.current)).toBe(99));
  });

  it("never shows an answer that lands late for the revision before", async () => {
    const before = held<NativeStackReadAnswer>();
    const after = held<NativeStackReadAnswer>();
    vi.mocked(fetchNativeStack)
      .mockImplementationOnce(() => before.promise)
      .mockImplementationOnce(() => after.promise);
    const { result, rerender } = renderStack();
    rerender({ pr: RETARGETED });
    await act(async () => after.resolve(stackedOn(99)));
    await waitFor(() => expect(previousNumber(result.current)).toBe(99));
    await act(async () => before.resolve(stackedOn(101)));
    expect(previousNumber(result.current)).toBe(99);
  });
});
