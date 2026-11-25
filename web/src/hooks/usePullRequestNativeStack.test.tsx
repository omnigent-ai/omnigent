import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetReadPrioritiesForTests } from "@/hooks/pullRequestReadPriority";
import { SecondaryReadPriorityProvider } from "@/hooks/secondaryReadPriority";
import { useDesktopPullRequestReads } from "@/newPullRequestPage/hooks/usePullRequests";
import type { CodeHostReadPriority } from "@/lib/nativeBridge";
import type { NativeStackAnswer } from "@/lib/pullRequestStacks/nativeStack";
import { fetchNativeStack, nativeStackOffered } from "@/lib/pullRequestStacks/nativeStackRead";
import type * as NativeStackReadModule from "@/lib/pullRequestStacks/nativeStackRead";

import { usePullRequestNativeStack } from "./usePullRequestNativeStack";

vi.mock("@/newPullRequestPage/hooks/usePullRequests", () => ({
  useDesktopPullRequestReads: vi.fn(),
}));
vi.mock("@/lib/pullRequestStacks/nativeStackRead", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeStackReadModule>()),
  fetchNativeStack: vi.fn(),
  nativeStackOffered: vi.fn(),
}));

const PR_URL = "https://github.com/acme/app/pull/102";
const HEAD = "a".repeat(40);

const member = (number: number) => ({
  url: `https://github.com/acme/app/pull/${number}`,
  number,
  state: "OPEN",
  isDraft: false,
  headRefName: `stack/${number}`,
  details: {
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
  },
});
const answer = (title: string): NativeStackAnswer => ({
  stackEntry: { position: 2 },
  stack: {
    id: `PRS_${title}`,
    number: 1,
    size: 2,
    baseRefName: "main",
    entries: {
      hasNextPage: false,
      nodes: [
        { position: 1, pullRequest: member(101) },
        { position: 2, pullRequest: member(102) },
      ],
    },
  },
});

let scope: readonly unknown[] = ["mac", "github.com", "alice"];
let reads = true;
beforeEach(() => {
  scope = ["mac", "github.com", "alice"];
  reads = true;
  vi.mocked(useDesktopPullRequestReads).mockImplementation(
    () =>
      ({
        known: () => true,
        readsOnThisMac: () => reads,
        target: (url: string) => ({
          provider: "github",
          host: "github.com",
          repository: "acme/app",
          number: 102,
          url,
        }),
        scope: () => scope,
      }) as unknown as ReturnType<typeof useDesktopPullRequestReads>,
  );
  vi.mocked(nativeStackOffered).mockReturnValue(true);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  resetReadPrioritiesForTests();
});

function render(head: string | null = HEAD, enabled = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return renderHook(
    ({ head: current }: { head: string | null }) =>
      usePullRequestNativeStack(PR_URL, current, enabled),
    { wrapper, initialProps: { head } },
  );
}

describe("usePullRequestNativeStack's place among reads", () => {
  function renderAt(priority: CodeHostReadPriority) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let shown = priority;
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(
        QueryClientProvider,
        { client },
        createElement(SecondaryReadPriorityProvider, { value: shown }, children),
      );
    const view = renderHook(() => usePullRequestNativeStack(PR_URL, HEAD, true), { wrapper });
    return {
      ...view,
      show: (next: CodeHostReadPriority) => {
        shown = next;
        view.rerender();
      },
    };
  }
  const priorities = () =>
    vi.mocked(fetchNativeStack).mock.calls.map(([, options]) => options?.priority);

  it("reads ahead while Changes shows, and as asked for elsewhere", async () => {
    vi.mocked(fetchNativeStack).mockResolvedValue({ viewer: "alice", answer: answer("a") });
    const ahead = renderAt("prefetch");
    await waitFor(() => expect(ahead.result.current.kind).toBe("settled"));
    ahead.unmount();
    const asked = renderAt("interactive");
    await waitFor(() => expect(asked.result.current.kind).toBe("settled"));
    expect(priorities()).toEqual(["prefetch", "interactive"]);
  });

  it("reads again as asked for when the user leaves Changes while it waits", async () => {
    vi.mocked(fetchNativeStack)
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockResolvedValue({ viewer: "alice", answer: answer("a") });
    const view = renderAt("prefetch");
    await waitFor(() => expect(priorities()).toEqual(["prefetch"]));
    act(() => view.show("interactive"));
    await waitFor(() => expect(view.result.current.kind).toBe("settled"));
    expect(priorities()).toEqual(["prefetch", "interactive"]);
  });
});

describe("usePullRequestNativeStack", () => {
  it("reads GitHub's stack with the members of its kept entries, as its reader", async () => {
    vi.mocked(fetchNativeStack).mockResolvedValue({ viewer: "alice", answer: answer("a") });
    const { result } = render();
    expect(result.current.kind).toBe("loading");
    await waitFor(() => expect(result.current.kind).toBe("settled"));
    if (result.current.kind !== "settled") return;
    expect(result.current.read?.viewer).toBe("alice");
    expect(result.current.members).toEqual({
      viewer: "alice",
      members: [member(101).details, member(102).details],
      missing: [],
      unread: [],
    });
  });

  it("never shows one account's answer once the page reads as another", async () => {
    let answerBob!: (value: Awaited<ReturnType<typeof fetchNativeStack>>) => void;
    vi.mocked(fetchNativeStack)
      .mockResolvedValueOnce({ viewer: "alice", answer: answer("alice") })
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            answerBob = resolve;
          }),
      );
    const { result, rerender } = render();
    await waitFor(() => expect(result.current.kind).toBe("settled"));
    scope = ["mac", "github.com", "bob"];
    rerender({ head: HEAD });
    expect(result.current.kind).toBe("loading");
    await act(async () => answerBob({ viewer: "bob", answer: answer("bob") }));
    await waitFor(() =>
      expect(result.current.kind === "settled" && result.current.read?.viewer).toBe("bob"),
    );
  });

  it("reads again for a new head, never showing the old head's answer", async () => {
    vi.mocked(fetchNativeStack)
      .mockResolvedValueOnce({ viewer: "alice", answer: answer("old") })
      .mockImplementationOnce(() => new Promise(() => {}));
    const { result, rerender } = render();
    await waitFor(() => expect(result.current.kind).toBe("settled"));
    rerender({ head: "b".repeat(40) });
    expect(result.current.kind).toBe("loading");
    expect(fetchNativeStack).toHaveBeenCalledTimes(2);
  });

  it("is off with the flag off, for a PR read elsewhere, and on a shell without the read", () => {
    expect(render(HEAD, false).result.current.kind).toBe("off");
    reads = false;
    expect(render().result.current.kind).toBe("off");
    reads = true;
    vi.mocked(nativeStackOffered).mockReturnValue(false);
    expect(render().result.current.kind).toBe("off");
    expect(fetchNativeStack).not.toHaveBeenCalled();
  });

  it("can't say when the read fails, so the description decides", async () => {
    vi.mocked(fetchNativeStack).mockRejectedValue(new Error("rate limited"));
    const { result } = render();
    await waitFor(() => expect(result.current.kind).toBe("settled"));
    expect(result.current).toEqual({
      kind: "settled",
      read: undefined,
      members: null,
      readAt: expect.any(Number),
    });
  });
});
