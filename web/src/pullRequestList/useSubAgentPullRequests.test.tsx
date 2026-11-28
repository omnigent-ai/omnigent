import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { invalidateSessionDescendants } from "@/hooks/useSessionDescendants";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import { sessionPullRequestsKey } from "@/newPullRequestPage/hooks/useSessionPullRequests";
import { authenticatedFetch } from "@/lib/identity";

import { useSubAgentPullRequests } from "./useSubAgentPullRequests";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const PARENT = "conv_parent";

interface DescendantRow {
  id: string;
  parent_session_id: string;
  depth: number;
  title?: string | null;
  task_summary?: string | null;
}

/** A fake server: the sub-agent tree, each session's stored PR list, and per-session failures. */
const server = {
  descendants: [] as DescendantRow[],
  lists: new Map<string, PullRequestAssociation[]>(),
  /** An HTTP status each session's PR read answers with, instead of its list. */
  failing: new Map<string, number>(),
  /** Sessions whose PR read fails before any answer, as a dropped connection does. */
  unreachable: new Set<string>(),
  /** The HTTP status the tree read answers with, when set. */
  descendantsStatus: undefined as number | undefined,
};

function pr(number: number, first_seen_at = 100): PullRequestAssociation {
  return {
    url: `https://github.com/acme/app/pull/${number}`,
    host: "github.com",
    repository: "acme/app",
    number,
    title: `PR ${number}`,
    relationship: "worked_on",
    first_seen_at,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installServer() {
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    const url = new URL(String(input), "http://localhost");
    const sessionId = decodeURIComponent(url.pathname.split("/")[3]!);
    if (url.pathname.endsWith("/child_sessions")) {
      if (server.descendantsStatus !== undefined) {
        return json({ error: { message: "nope" } }, server.descendantsStatus);
      }
      return json({ object: "list", data: server.descendants, has_more: false, last_id: null });
    }
    if (server.unreachable.has(sessionId)) throw new TypeError("Failed to fetch");
    const failure = server.failing.get(sessionId);
    if (failure !== undefined) return json({ error: { message: "nope" } }, failure);
    return json({
      object: "list",
      data: server.lists.get(sessionId) ?? [],
      synced: true,
      has_more: false,
      last_id: null,
    });
  });
}

function renderJoined(own: PullRequestAssociation[] = [pr(1)], listVisible = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(() => useSubAgentPullRequests(PARENT, own, listVisible), { wrapper });
  return { client, ...hook };
}

const urls = (view: ReturnType<typeof useSubAgentPullRequests>) =>
  (view?.merge.prs ?? []).map((row) => row.number).sort((a, b) => a - b);

/** The text the console capture saved for each failed read, in order. */
function capturedFailures(warn: { mock: { calls: unknown[][] } }): string[] {
  return warn.mock.calls.map((call) => String(call[0]));
}

beforeEach(() => {
  server.descendants = [];
  server.lists = new Map();
  server.failing = new Map();
  server.unreachable = new Set();
  server.descendantsStatus = undefined;
  installServer();
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.mocked(authenticatedFetch).mockReset();
  vi.restoreAllMocks();
});

describe("useSubAgentPullRequests freshness", () => {
  it("shows a PR a background sub-agent adds after one poll, with no child panel mounted", async () => {
    server.descendants = [{ id: "child-a", parent_session_id: PARENT, depth: 1, title: "x:a" }];
    server.lists.set("child-a", []);
    const { result } = renderJoined();
    await waitFor(() => expect(urls(result.current)).toEqual([1]));

    server.lists.set("child-a", [pr(7)]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    await waitFor(() => expect(urls(result.current)).toEqual([1, 7]));
  });

  it("drops a PR as soon as its sub-agent leaves the tree, though its PR list is still cached", async () => {
    server.descendants = [
      { id: "child-a", parent_session_id: PARENT, depth: 1, title: "x:a" },
      { id: "child-b", parent_session_id: PARENT, depth: 1, title: "x:b" },
    ];
    server.lists.set("child-a", [pr(7)]);
    server.lists.set("child-b", [pr(8)]);
    const { client, result } = renderJoined();
    await waitFor(() => expect(urls(result.current)).toEqual([1, 7, 8]));

    // The sub-agent is archived: the tree drops it, while its PR list stays cached.
    server.descendants = [server.descendants[1]!];
    await act(async () => {
      await invalidateSessionDescendants(client);
    });
    await waitFor(() => expect(urls(result.current)).toEqual([1, 8]));
    expect(client.getQueryData(sessionPullRequestsKey("child-a"))).toBeDefined();
  });

  it("hides a whole archived subtree, grandchildren included", async () => {
    server.descendants = [
      { id: "child-a", parent_session_id: PARENT, depth: 1, title: "x:a" },
      { id: "grand-a", parent_session_id: "child-a", depth: 2, title: "x:g" },
    ];
    server.lists.set("child-a", [pr(7)]);
    server.lists.set("grand-a", [pr(9)]);
    const { client, result } = renderJoined();
    await waitFor(() => expect(urls(result.current)).toEqual([1, 7, 9]));

    // An archived ancestor hides its subtree: the tree returns nothing below the parent.
    server.descendants = [];
    await act(async () => {
      await invalidateSessionDescendants(client);
    });
    await waitFor(() => expect(urls(result.current)).toEqual([1]));
  });

  it("leaves out a sub-agent whose refresh is forbidden, and brings it back when the read recovers", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    server.descendants = [{ id: "child-a", parent_session_id: PARENT, depth: 1, title: "x:a" }];
    server.lists.set("child-a", [pr(7)]);
    const { client, result } = renderJoined();
    await waitFor(() => expect(urls(result.current)).toEqual([1, 7]));

    server.failing.set("child-a", 403);
    await act(async () => {
      await client.invalidateQueries({ queryKey: sessionPullRequestsKey("child-a") });
    });
    expect(client.getQueryState(sessionPullRequestsKey("child-a"))?.status).toBe("error");
    // The cached list is still there; the failed read keeps it out of the joined view.
    expect(client.getQueryData(sessionPullRequestsKey("child-a"))).toBeDefined();
    await waitFor(() => expect(urls(result.current)).toEqual([1]));

    server.failing.delete("child-a");
    await act(async () => {
      await client.invalidateQueries({ queryKey: sessionPullRequestsKey("child-a") });
    });
    await waitFor(() => expect(urls(result.current)).toEqual([1, 7]));
  });

  it("does not read the tree or any sub-agent while the list is off screen", async () => {
    server.descendants = [{ id: "child-a", parent_session_id: PARENT, depth: 1, title: "x:a" }];
    server.lists.set("child-a", [pr(7)]);
    const { result } = renderJoined([pr(1)], false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(result.current).toBeUndefined();
    expect(vi.mocked(authenticatedFetch)).not.toHaveBeenCalled();
  });
});

describe("useSubAgentPullRequests failed reads", () => {
  it("records a failed sub-agent read as captured text, with its trigger, reason and status", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    server.descendants = [
      { id: "child-a", parent_session_id: PARENT, depth: 1, title: "x:a" },
      { id: "child-b", parent_session_id: PARENT, depth: 1, title: "x:b" },
    ];
    server.lists.set("child-b", [pr(8)]);
    server.failing.set("child-a", 403);
    const { result } = renderJoined();
    await waitFor(() => expect(urls(result.current)).toEqual([1, 8]));
    expect(capturedFailures(warn)).toEqual([
      `[omnigent] sub-agent pull requests read failed stage=member_pull_requests trigger=initial reason=http parent=${PARENT} child=child-a status=403`,
    ]);
  });

  it("records a read that never got an answer as a network failure with no status", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    server.descendants = [{ id: "child-a", parent_session_id: PARENT, depth: 1, title: "x:a" }];
    server.unreachable.add("child-a");
    const { result } = renderJoined();
    await waitFor(() => expect(warn).toHaveBeenCalled());
    expect(urls(result.current)).toEqual([1]);
    expect(capturedFailures(warn)).toEqual([
      `[omnigent] sub-agent pull requests read failed stage=member_pull_requests trigger=initial reason=network parent=${PARENT} child=child-a`,
    ]);
  });

  it("records a failed tree read as its own stage, with the refresh trigger after a good read", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    server.descendants = [{ id: "child-a", parent_session_id: PARENT, depth: 1, title: "x:a" }];
    server.lists.set("child-a", [pr(7)]);
    const { client, result } = renderJoined();
    await waitFor(() => expect(urls(result.current)).toEqual([1, 7]));

    server.descendantsStatus = 500;
    await act(async () => {
      await invalidateSessionDescendants(client);
    });
    await waitFor(() =>
      expect(capturedFailures(warn)).toEqual([
        `[omnigent] sub-agent pull requests read failed stage=descendants trigger=refresh reason=http parent=${PARENT} status=500`,
      ]),
    );
  });

  it("names a sub-agent the way the Agents rail does, never as untitled:<id>", async () => {
    server.descendants = [
      { id: "child-a", parent_session_id: PARENT, depth: 1, title: "untitled:child-a" },
      {
        id: "child-b",
        parent_session_id: PARENT,
        depth: 1,
        title: "researcher:auth",
        task_summary: "Check login",
      },
    ];
    server.lists.set("child-a", [pr(7)]);
    server.lists.set("child-b", [pr(8)]);
    const { result } = renderJoined();
    await waitFor(() => expect(urls(result.current)).toEqual([1, 7, 8]));
    const names = [...(result.current?.merge.sources.values() ?? [])].map(
      (source) => source.session?.name,
    );
    expect(names).toEqual(expect.arrayContaining(["child-a", "Check login"]));
    expect(names.some((name) => name?.startsWith("untitled:"))).toBe(false);
  });

  it("is undefined while the list is off", () => {
    const { result } = renderHook(() => useSubAgentPullRequests(PARENT, undefined, true), {
      wrapper: ({ children }: PropsWithChildren) => (
        <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
      ),
    });
    expect(result.current).toBeUndefined();
  });
});

describe("useSubAgentPullRequests abandoned reads", () => {
  it("stops a queued sub-agent read once the list unmounts", async () => {
    server.descendants = Array.from({ length: 5 }, (_, index) => ({
      id: `child-${index}`,
      parent_session_id: PARENT,
      depth: 1,
    }));
    const started: string[] = [];
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const answer = vi.mocked(authenticatedFetch).getMockImplementation()!;
    vi.mocked(authenticatedFetch).mockImplementation(async (...args) => {
      if (String(args[0]).includes("/pull-requests")) {
        started.push(String(args[0]));
        await gate;
      }
      return answer(...args);
    });
    const { unmount } = renderJoined();
    await waitFor(() => expect(started).toHaveLength(4));
    unmount();
    await act(async () => {
      release();
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(started).toHaveLength(4);
  });

  it("stops a queued sub-agent read when the list is switched off", async () => {
    server.descendants = Array.from({ length: 5 }, (_, index) => ({
      id: `child-${index}`,
      parent_session_id: PARENT,
      depth: 1,
    }));
    const started: string[] = [];
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const answer = vi.mocked(authenticatedFetch).getMockImplementation()!;
    vi.mocked(authenticatedFetch).mockImplementation(async (...args) => {
      if (String(args[0]).includes("/pull-requests")) {
        started.push(String(args[0]));
        await gate;
      }
      return answer(...args);
    });
    const { rerender } = renderHook(
      ({ listVisible }: { listVisible: boolean }) =>
        useSubAgentPullRequests(PARENT, [pr(1)], listVisible),
      {
        initialProps: { listVisible: true },
        wrapper: ({ children }: PropsWithChildren) => (
          <QueryClientProvider
            client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
          >
            {children}
          </QueryClientProvider>
        ),
      },
    );
    await waitFor(() => expect(started).toHaveLength(4));
    rerender({ listVisible: false });
    await act(async () => {
      release();
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(started).toHaveLength(4);
  });
});
