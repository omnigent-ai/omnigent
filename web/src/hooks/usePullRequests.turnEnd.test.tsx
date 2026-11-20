// Turn-end refresh discovers PRs without opening the panel; composer polling
// then tracks their status until all linked PRs are terminal.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: vi.fn(),
}));

import { useSessionHostOnline, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useChatStore } from "@/store/chatStore";
import { usePullRequestInfo, usePullRequestInfos } from "./usePullRequests";

const onlineMock = vi.mocked(useSessionRunnerOnline);
const hostOnlineMock = vi.mocked(useSessionHostOnline);
const chatStoreMock = vi.mocked(useChatStore);
const fetchMock = vi.fn();

type StubStatus = "idle" | "running" | "waiting" | "failed";

function stubChatStore(conversationId: string | null, sessionStatus: StubStatus) {
  chatStoreMock.mockImplementation((selector: unknown) => {
    if (typeof selector === "function") {
      return (
        selector as (s: { conversationId: string | null; sessionStatus: StubStatus }) => unknown
      )({ conversationId, sessionStatus });
    }
    return undefined;
  });
}

function pullRequestInfoResponse(info: Record<string, unknown> = {}): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => ({ object: "session.github.info", available: true, ...info }),
  } as unknown as Response;
}

function Probe({
  id,
  batch = false,
  poll,
  prUrl,
}: {
  id: string | undefined;
  batch?: boolean;
  poll?: "linked";
  prUrl?: string;
}) {
  usePullRequestInfo(id, { poll, prUrl });
  usePullRequestInfos(id, batch ? ["https://github.com/org/repo/pull/1"] : []);
  return null;
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(pullRequestInfoResponse());
  vi.stubGlobal("fetch", fetchMock);
  onlineMock.mockReturnValue(true);
  hostOnlineMock.mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe("usePullRequestInfo turn-end invalidate", () => {
  it("refreshes each PR once when the composer and linked PR rows observe the same turn end", async () => {
    stubChatStore("conv_live", "running");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const urls = Array.from(
      { length: 25 },
      (_, index) => `https://example.test/repo/pull/${index + 1}`,
    );
    const tree = () => (
      <QueryClientProvider client={client}>
        <Probe id="conv_live" />
        {urls.map((prUrl) => (
          <Probe key={prUrl} id="conv_live" prUrl={prUrl} />
        ))}
      </QueryClientProvider>
    );
    const view = render(tree());
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(fetchMock).toHaveBeenCalledTimes(urls.length + 1);

    const finish: (() => void)[] = [];
    fetchMock.mockClear();
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          finish.push(() => resolve(pullRequestInfoResponse()));
        }),
    );
    try {
      stubChatStore("conv_live", "idle");
      view.rerender(tree());
      await act(async () => {});
      expect(fetchMock).toHaveBeenCalledTimes(urls.length + 1);
    } finally {
      await act(async () => {
        for (const resolve of finish) resolve();
      });
      view.unmount();
      client.clear();
    }
  });

  it.each([false, true])("aborts superseded and unmounted PR reads (batch=%s)", async (batch) => {
    stubChatStore("conv_live", "running");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = () => (
      <QueryClientProvider client={client}>
        <Probe id="conv_live" batch={batch} />
      </QueryClientProvider>
    );
    const view = render(tree());
    await waitFor(() => expect(client.isFetching()).toBe(0));
    const requests: { signal: AbortSignal | null | undefined; finish: () => void }[] = [];
    fetchMock.mockImplementation(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((resolve, reject) => {
          const signal = init?.signal;
          const abort = () => reject(signal?.reason);
          signal?.addEventListener("abort", abort, { once: true });
          requests.push({
            signal,
            finish: () => {
              signal?.removeEventListener("abort", abort);
              resolve(pullRequestInfoResponse());
            },
          });
        }),
    );
    const perTurn = batch ? 2 : 1;
    try {
      stubChatStore("conv_live", "idle");
      view.rerender(tree());
      await act(async () => {});
      expect(requests).toHaveLength(perTurn);

      // A later turn needs a fresh snapshot, even when the last read is still slow.
      stubChatStore("conv_live", "running");
      view.rerender(tree());
      stubChatStore("conv_live", "idle");
      view.rerender(tree());
      await act(async () => {});
      expect(requests).toHaveLength(perTurn * 2);
      expect(requests.slice(0, perTurn).map(({ signal }) => signal?.aborted)).toEqual(
        Array(perTurn).fill(true),
      );
      expect(requests.slice(perTurn).map(({ signal }) => signal?.aborted)).toEqual(
        Array(perTurn).fill(false),
      );
      view.unmount();
      expect(requests.every(({ signal }) => signal?.aborted)).toBe(true);
    } finally {
      await act(async () => {
        for (const { finish } of requests) finish();
      });
      view.unmount();
      client.clear();
    }
  });

  it.each([false, true])(
    "refetches info when the focused session goes running → idle (batch=%s)",
    async (batch) => {
      stubChatStore("conv_live", "running");
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
      const { rerender } = render(
        <QueryClientProvider client={qc}>
          <Probe id="conv_live" batch={batch} />
        </QueryClientProvider>,
      );
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(batch ? 2 : 1));

      // Turn end refetches every github-info query for the session.
      stubChatStore("conv_live", "idle");
      rerender(
        <QueryClientProvider client={qc}>
          <Probe id="conv_live" batch={batch} />
        </QueryClientProvider>,
      );
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(batch ? 4 : 2));
      expect(fetchMock.mock.calls[batch ? 2 : 1][0]).toBe(
        "/v1/sessions/conv_live/resources/github",
      );
      if (batch) {
        expect(fetchMock.mock.calls[3][0]).toBe(
          "/v1/sessions/conv_live/resources/github?pr_url=https%3A%2F%2Fgithub.com%2Forg%2Frepo%2Fpull%2F1",
        );
      }
    },
  );

  it("does not refetch when status stays idle across renders", async () => {
    // Guard against a spurious refetch on every render — only the
    // active → idle transition triggers the trailing edge.
    stubChatStore("conv_live", "idle");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    rerender(
      <QueryClientProvider client={qc}>
        <Probe id="conv_live" />
      </QueryClientProvider>,
    );
    await Promise.resolve();
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(["selected", "linked"])(
    "discovers a %s PR at turn end and polls until linked PRs are terminal",
    async (source) => {
      vi.useFakeTimers();
      stubChatStore("conv_live", "running");
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const tree = (
        <QueryClientProvider client={client}>
          <Probe id="conv_live" poll="linked" />
        </QueryClientProvider>
      );
      const { rerender } = render(tree);
      await act(() => vi.advanceTimersByTimeAsync(90_000));
      expect(fetchMock).toHaveBeenCalledTimes(1);

      const pr = { url: "https://github.com/org/repo/pull/1", state: "OPEN" };
      fetchMock.mockResolvedValue(
        pullRequestInfoResponse(source === "selected" ? { pr } : { prs: [pr] }),
      );
      stubChatStore("conv_live", "idle");
      rerender(
        <QueryClientProvider client={client}>
          <Probe id="conv_live" poll="linked" />
        </QueryClientProvider>,
      );
      await act(() => vi.advanceTimersByTimeAsync(0));
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await act(() => vi.advanceTimersByTimeAsync(30_000));
      expect(fetchMock).toHaveBeenCalledTimes(3);

      fetchMock.mockResolvedValue(
        pullRequestInfoResponse({
          pr: null,
          prs: [
            { ...pr, state: "MERGED" },
            { ...pr, url: `${pr.url}2`, state: "CLOSED" },
          ],
        }),
      );
      await act(() => vi.advanceTimersByTimeAsync(30_000));
      expect(fetchMock).toHaveBeenCalledTimes(4);
      await act(() => vi.advanceTimersByTimeAsync(90_000));
      expect(fetchMock).toHaveBeenCalledTimes(4);
    },
  );
});
