// GitHub's own stack reads only through a session host that serves the read, with a test-only
// host answer standing in for the host.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { NativeStackAnswer } from "@/lib/pullRequestStacks/nativeStack";
import {
  NATIVE_STACK_HOST_METHOD,
  setNativeStackHostReadForTests,
} from "@/lib/pullRequestStacks/nativeStackHost";

const host = vi.hoisted(() => ({ served: true }));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestCapabilityServed: (_sessionId: string, capability: string) =>
    host.served && capability === "GetPullRequestNativeStack",
}));

import { refreshPullRequestStackMembers } from "./usePullRequestStackMembers";
import { usePullRequestNativeStack } from "./usePullRequestNativeStack";

const SESSION = "s1";
const PR_URL = "https://github.com/acme/app/pull/102";
const HEAD = "a".repeat(40);

const details = (number: number) => ({
  url: `https://github.com/acme/app/pull/${number}`,
  number,
  title: `PR ${number}`,
  state: "OPEN" as const,
  is_draft: false,
  updated_at: null,
  review_decision: null,
  head_ref: `stack/${number}`,
  head_sha: String(number).padStart(40, "c"),
  base_ref: "main",
  base_sha: null,
  head_repository: "acme/app",
  base_repository: "acme/app",
  base_default_branch: null,
});
/** The host's wire answer: GitHub's stack of 101 and 102, read as `viewer`. */
const wire = (viewer: string, id = "PRS_1") => ({
  supported: true,
  viewer,
  native_stack: {
    stackEntry: { position: 2 },
    stack: {
      id,
      number: 1,
      size: 2,
      baseRefName: "main",
      entries: {
        hasNextPage: false,
        nodes: [
          { position: 1, pullRequest: details(101) },
          { position: 2, pullRequest: details(102) },
        ],
      },
    },
  } satisfies Omit<NativeStackAnswer, "stack"> & { stack: unknown },
});

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

function render(head: string | null = HEAD, enabled = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  const view = renderHook(
    ({ head: current }: { head: string | null }) =>
      usePullRequestNativeStack(SESSION, PR_URL, current, enabled),
    { wrapper, initialProps: { head } },
  );
  return Object.assign(view, { client });
}

describe("usePullRequestNativeStack", () => {
  it("names the host method it waits on", () => {
    expect(NATIVE_STACK_HOST_METHOD).toBe("GetPullRequestNativeStack");
  });

  it("reads GitHub's stack through the session's host, with its kept entries' members", async () => {
    hostRead.mockResolvedValue(wire("alice"));
    const { result } = render();
    expect(result.current.kind).toBe("loading");
    await waitFor(() => expect(result.current.kind).toBe("settled"));
    expect(hostRead).toHaveBeenCalledWith(SESSION, PR_URL, expect.anything());
    if (result.current.kind !== "settled") return;
    expect(result.current.read?.viewer).toBe("alice");
    expect(result.current.members).toMatchObject({
      viewer: "alice",
      members: [details(101), details(102)],
      missing: [],
      unread: [],
    });
  });

  it("reads an open member's stack again only on Refresh, never on a timer or focus", async () => {
    // Real time still runs; the polling and staleness timers can be skipped ahead.
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "setInterval", "Date"] });
    try {
      hostRead.mockResolvedValue(wire("alice"));
      const { result, client } = render();
      await waitFor(() => expect(result.current.kind).toBe("settled"));
      expect(hostRead).toHaveBeenCalledTimes(1);

      await act(async () => {
        vi.advanceTimersByTime(10 * 60_000);
        window.dispatchEvent(new Event("visibilitychange"));
        window.dispatchEvent(new Event("focus"));
        window.dispatchEvent(new Event("online"));
      });
      expect(hostRead).toHaveBeenCalledTimes(1);

      await act(() => refreshPullRequestStackMembers(client));
      expect(hostRead).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reads again for a new head, never showing the old head's answer", async () => {
    hostRead
      .mockResolvedValueOnce(wire("alice"))
      .mockImplementationOnce(() => new Promise(() => {}));
    const { result, rerender } = render();
    await waitFor(() => expect(result.current.kind).toBe("settled"));
    rerender({ head: "b".repeat(40) });
    expect(result.current.kind).toBe("loading");
    expect(hostRead).toHaveBeenCalledTimes(2);
  });

  it("is off, asking nothing, where the host doesn't serve the read or the flag is off", () => {
    host.served = false;
    expect(render().result.current.kind).toBe("off");
    host.served = true;
    expect(render(HEAD, false).result.current.kind).toBe("off");
    expect(hostRead).not.toHaveBeenCalled();
  });

  it("can't say when the read fails or answers another shape, so the description decides", async () => {
    hostRead.mockRejectedValueOnce(new Error("rate limited"));
    const failed = render();
    await waitFor(() => expect(failed.result.current.kind).toBe("settled"));
    expect(failed.result.current).toEqual({
      kind: "settled",
      read: undefined,
      members: null,
      readAt: expect.any(Number),
    });
    hostRead.mockResolvedValueOnce({ supported: false });
    const unsupported = render("c".repeat(40));
    await waitFor(() => expect(unsupported.result.current.kind).toBe("settled"));
    expect(unsupported.result.current).toMatchObject({ read: undefined, members: null });
  });
});
