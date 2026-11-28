// The page shows GitHub's own stack only where the session's host serves its read; a test-only
// host answer stands in for the method until the host adds it.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setNativeStackHostReadForTests } from "@/lib/pullRequestStacks/nativeStackHost";

vi.mock("@/hooks/useDesktopFeatureFlag", async () =>
  (await import("@/test/desktopFeatureFlagMock")).desktopFeatureFlagModule(() => ({
    value: true,
    resolved: true,
  })),
);
const host = vi.hoisted(() => ({ served: true }));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestCapabilityServed: () => host.served,
  usePullRequestMethodServed: () => false,
}));

import {
  type PullRequestStackView,
  type StackedPullRequest,
  usePullRequestStackRead,
} from "./usePullRequestStack";

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

/** The host's answer: GitHub's stack with this PR stacked directly on `parent`. */
function stackedOn(parent: number) {
  const member = (number: number) => ({
    url: url(number),
    number,
    title: `PR ${number}`,
    state: "OPEN",
    is_draft: false,
    head_ref: `stack/${number}`,
  });
  return {
    supported: true,
    viewer: "alice",
    native_stack: {
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
    },
  };
}

function held<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const previousNumber = (stack: PullRequestStackView | null) =>
  stack?.previous.kind === "go" ? stack.previous.destination.node.ref?.number : null;

function renderStack() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return renderHook(
    ({ pr }: { pr: StackedPullRequest }) =>
      usePullRequestStackRead(pr, () => true, { conversationId: "s1" }).stack,
    { wrapper, initialProps: { pr: PR } },
  );
}

const hostRead = vi.fn<(sessionId: string, prUrl: string) => Promise<unknown>>();
beforeEach(() => {
  host.served = true;
  hostRead.mockReset();
  setNativeStackHostReadForTests(hostRead);
});
afterEach(() => {
  cleanup();
  setNativeStackHostReadForTests(null);
});

describe("usePullRequestStackRead with GitHub's stack", () => {
  it("shows GitHub's stack where the session's host serves its read", async () => {
    hostRead.mockResolvedValue(stackedOn(101));
    const { result } = renderStack();
    await waitFor(() => expect(previousNumber(result.current)).toBe(101));
    expect(hostRead).toHaveBeenCalledWith("s1", url(102), expect.anything());
  });

  it("shows no native stack, and asks nothing, where the host doesn't serve the read", async () => {
    host.served = false;
    hostRead.mockResolvedValue(stackedOn(101));
    const { result } = renderStack();
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });
    expect(result.current).toBeNull();
    expect(hostRead).not.toHaveBeenCalled();
  });

  it("reads GitHub's stack again when the PR is retargeted on the same head", async () => {
    const retargeted = held<unknown>();
    hostRead.mockResolvedValueOnce(stackedOn(101)).mockImplementationOnce(() => retargeted.promise);
    const { result, rerender } = renderStack();
    await waitFor(() => expect(previousNumber(result.current)).toBe(101));
    rerender({ pr: RETARGETED });
    // The old parent never shows for the new base while its stack is read.
    expect(result.current).toBeNull();
    expect(hostRead).toHaveBeenCalledTimes(2);
    await act(async () => retargeted.resolve(stackedOn(99)));
    await waitFor(() => expect(previousNumber(result.current)).toBe(99));
  });

  it("never shows an answer that lands late for the revision before", async () => {
    const before = held<unknown>();
    const after = held<unknown>();
    hostRead
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
