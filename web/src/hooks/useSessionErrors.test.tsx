import type { ReactNode } from "react";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MessageItem } from "@/lib/conversationItems";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { fetchSessionItemsPage, type SessionItemsPage } from "@/lib/sessionsApi";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useSessionErrors } from "./useSessionErrors";

vi.mock("@/lib/sessionsApi", () => ({ fetchSessionItemsPage: vi.fn() }));
const fetchPage = vi.mocked(fetchSessionItemsPage);
const session = { id: "session1", updated_at: 100, status: "idle" as const };
const message = (text: string): MessageItem => ({
  id: "message1",
  response_id: "response1",
  type: "message",
  status: "completed",
  role: "assistant",
  content: [{ type: "output_text", text }],
});
const errorPage: SessionItemsPage = {
  items: [
    message(
      "API Error: Request rejected (429) · REQUEST_LIMIT_EXCEEDED: Exceeded workspace input tokens per minute rate limit.",
    ),
  ],
  hasMore: true,
};
const normalPage: SessionItemsPage = { items: [message("Recovered.")], hasMore: true };

function harness() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

function deferredPage() {
  let resolve!: (page: SessionItemsPage) => void;
  const promise = new Promise<SessionItemsPage>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  fetchPage.mockReset();
  fetchPage.mockResolvedValue(normalPage);
  conversationRegistry.clear();
});
afterEach(() => {
  cleanup();
  conversationRegistry.clear();
});

describe("useSessionErrors", () => {
  it("flags an unopened idle session from its latest native message and reuses the cache", async () => {
    fetchPage.mockResolvedValue(errorPage);
    const { wrapper } = harness();
    const first = renderHook(() => useSessionErrors([session]), { wrapper });
    await waitFor(() => expect(first.result.current).toEqual([true]));
    expect(fetchPage).toHaveBeenCalledWith(session.id, {
      limit: 1,
      signal: expect.any(AbortSignal),
    });
    first.unmount();

    const reopened = renderHook(() => useSessionErrors([session]), { wrapper });
    expect(reopened.result.current).toEqual([true]);
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it("refetches and clears the error when the existing updated_at signal advances", async () => {
    fetchPage.mockResolvedValueOnce(errorPage).mockResolvedValueOnce(normalPage);
    const { wrapper } = harness();
    const hook = renderHook(({ updated_at }) => useSessionErrors([{ ...session, updated_at }]), {
      wrapper,
      initialProps: { updated_at: 100 },
    });
    await waitFor(() => expect(hook.result.current).toEqual([true]));
    hook.rerender({ updated_at: 101 });
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(hook.result.current).toEqual([false]));
  });

  it("does not let an older tail request resurrect an error after a newer message", async () => {
    const old = deferredPage();
    fetchPage.mockReturnValueOnce(old.promise).mockResolvedValueOnce(normalPage);
    const { wrapper, client } = harness();
    const hook = renderHook(({ updated_at }) => useSessionErrors([{ ...session, updated_at }]), {
      wrapper,
      initialProps: { updated_at: 100 },
    });
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(1));
    hook.rerender({ updated_at: 101 });
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(2));
    await act(async () => old.resolve(errorPage));
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(hook.result.current).toEqual([false]);
  });

  it("shares one request between a row and its collapsed project marker", async () => {
    fetchPage.mockResolvedValue(errorPage);
    const { wrapper } = harness();
    const hook = renderHook(() => [useSessionErrors([session]), useSessionErrors([session])], {
      wrapper,
    });
    await waitFor(() => expect(hook.result.current).toEqual([[true], [true]]));
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it("reads and subscribes to an already-loaded transcript without extra HTTP or streams", () => {
    const entry = conversationRegistry.acquire(session.id);
    entry.setState({
      blocks: itemsToBlocks(errorPage.items),
      loadingConversation: false,
      abortController: new AbortController(),
    });
    const { wrapper } = harness();
    const hook = renderHook(() => useSessionErrors([session]), { wrapper });
    expect(hook.result.current).toEqual([true]);
    act(() => entry.setState({ blocks: itemsToBlocks(normalPage.items) }));
    expect(hook.result.current).toEqual([false]);
    act(() => entry.setState({ blocks: itemsToBlocks(errorPage.items) }));
    expect(hook.result.current).toEqual([true]);
    act(() => entry.setState({ status: "streaming" }));
    expect(hook.result.current).toEqual([false]);
    expect(fetchPage).not.toHaveBeenCalled();
  });

  it("waits for an in-flight history load instead of requesting a redundant tail", async () => {
    const entry = conversationRegistry.acquire(session.id);
    entry.setState({ loadingConversation: true });
    const { wrapper } = harness();
    const hook = renderHook(() => useSessionErrors([session]), { wrapper });
    await act(async () => {});
    expect(fetchPage).not.toHaveBeenCalled();

    await act(async () => entry.setState({ abortController: new AbortController() }));
    expect(fetchPage).not.toHaveBeenCalled();

    await act(async () =>
      entry.setState({ blocks: itemsToBlocks(errorPage.items), loadingConversation: false }),
    );
    expect(hook.result.current).toEqual([true]);
    expect(fetchPage).not.toHaveBeenCalled();
  });

  it("falls back to a tail if the in-flight history load fails", async () => {
    const entry = conversationRegistry.acquire(session.id);
    entry.setState({ loadingConversation: true, abortController: new AbortController() });
    fetchPage.mockResolvedValue(errorPage);
    const { wrapper } = harness();
    const hook = renderHook(() => useSessionErrors([session]), { wrapper });
    await act(async () => {});
    expect(fetchPage).not.toHaveBeenCalled();

    act(() =>
      entry.setState({
        loadingConversation: false,
        conversationLoadError: new Error("history unavailable"),
      }),
    );
    await waitFor(() => expect(hook.result.current).toEqual([true]));
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it("falls back to the latest item after a live transcript is evicted", async () => {
    const entry = conversationRegistry.acquire(session.id);
    entry.setState({
      blocks: itemsToBlocks(errorPage.items),
      loadingConversation: false,
      abortController: new AbortController(),
    });
    const { wrapper, client } = harness();
    const hook = renderHook(() => useSessionErrors([session]), { wrapper });
    expect(hook.result.current).toEqual([true]);
    act(() => conversationRegistry.release(session.id));
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(hook.result.current).toEqual([false]);
  });

  it("does not treat a dead stream's retained transcript as current", async () => {
    const controller = new AbortController();
    controller.abort();
    conversationRegistry.acquire(session.id).setState({
      blocks: itemsToBlocks(normalPage.items),
      loadingConversation: false,
      abortController: controller,
    });
    fetchPage.mockResolvedValue(errorPage);
    const { wrapper } = harness();
    const hook = renderHook(() => useSessionErrors([session]), { wrapper });
    await waitFor(() => expect(hook.result.current).toEqual([true]));
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it("revalidates a superseded tail cache when the live transcript is evicted", async () => {
    fetchPage.mockResolvedValueOnce(errorPage).mockResolvedValueOnce(normalPage);
    const { wrapper, client } = harness();
    const hook = renderHook(() => useSessionErrors([session]), { wrapper });
    await waitFor(() => expect(hook.result.current).toEqual([true]));
    act(() =>
      conversationRegistry.acquire(session.id).setState({
        blocks: itemsToBlocks(normalPage.items),
        loadingConversation: false,
        abortController: new AbortController(),
      }),
    );
    expect(hook.result.current).toEqual([false]);
    act(() => conversationRegistry.release(session.id));
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(hook.result.current).toEqual([false]);
  });

  it("does not request tails for running, failed, awaiting, or provisional rows", async () => {
    const { wrapper } = harness();
    renderHook(
      () =>
        useSessionErrors([
          { ...session, id: "running", status: "running" },
          { ...session, id: "failed", status: "failed" },
          { ...session, id: "awaiting", pending_elicitations_count: 1 },
          { ...session, id: "temp:pending" },
          { ...session, id: "provisional", provisional: true },
        ]),
      { wrapper },
    );
    await act(async () => {});
    expect(fetchPage).not.toHaveBeenCalled();
  });

  it("does not turn an HTTP failure into a conversation error", async () => {
    fetchPage.mockRejectedValue(new Error("offline"));
    const { wrapper, client } = harness();
    const hook = renderHook(() => useSessionErrors([session]), { wrapper });
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(hook.result.current).toEqual([false]);
  });

  it("skips trailing hidden metadata with a bounded fallback", async () => {
    fetchPage
      .mockResolvedValueOnce({
        items: [{ ...message("Hidden context"), is_meta: true }],
        hasMore: true,
      })
      .mockResolvedValueOnce(errorPage);
    const { wrapper } = harness();
    const hook = renderHook(() => useSessionErrors([session]), { wrapper });
    await waitFor(() => expect(hook.result.current).toEqual([true]));
    expect(fetchPage).toHaveBeenLastCalledWith(session.id, {
      olderThan: "message1",
      limit: 8,
      signal: expect.any(AbortSignal),
    });
  });

  it("limits simultaneous unopened-session reads to two", async () => {
    const pages = Array.from({ length: 5 }, () => deferredPage());
    pages.forEach((page) => fetchPage.mockReturnValueOnce(page.promise));
    const { wrapper, client } = harness();
    renderHook(() => useSessionErrors(pages.map((_, i) => ({ ...session, id: `session${i}` }))), {
      wrapper,
    });
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(2));
    await act(async () => {
      pages[0].resolve(normalPage);
      pages[1].resolve(normalPage);
    });
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(4));
    await act(async () => {
      pages[2].resolve(normalPage);
      pages[3].resolve(normalPage);
    });
    await waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(5));
    await act(async () => pages[4].resolve(normalPage));
    await waitFor(() => expect(client.isFetching()).toBe(0));
  });
});
