import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor, cleanup } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { useAddCommentReply, useCommentReplies, useDeleteCommentReply } from "./useCommentReplies";

const fetchMock = vi.fn();
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (...args: unknown[]) => fetchMock(...args),
}));
afterEach(() => {
  cleanup();
  fetchMock.mockReset();
});
function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
it("follows encoded cursors and shares the comments invalidation prefix", async () => {
  fetchMock
    .mockResolvedValueOnce(
      response({ replies: [{ id: "first" }], next_cursor: { created_at: 1234567, id: "a/b" } }),
    )
    .mockResolvedValueOnce(response({ replies: [{ id: "last" }], next_cursor: null }));
  const { client, wrapper } = setup();
  const { result } = renderHook(() => useCommentReplies("s/a", "c/b"), { wrapper });
  await waitFor(() => expect(result.current.data).toEqual([{ id: "first" }, { id: "last" }]));
  expect(fetchMock.mock.calls[0]![0]).toBe("/v1/sessions/s%2Fa/comments/c%2Fb/replies?limit=100");
  expect(fetchMock.mock.calls[1]![0]).toContain("after_created_at=1234567&after_id=a%2Fb");
  expect(client.getQueryData(["comments", "s/a", "replies", "c/b", undefined])).toEqual(
    result.current.data,
  );
});
it("does not fetch disabled threads", () => {
  renderHook(() => useCommentReplies("s", "c", false), { wrapper: setup().wrapper });
  expect(fetchMock).not.toHaveBeenCalled();
});
it("posts a body and invalidates parent lists and threads", async () => {
  fetchMock.mockResolvedValue(response({ id: "reply" }));
  const { client, wrapper } = setup();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const { result } = renderHook(() => useAddCommentReply("s", "c"), { wrapper });
  await act(async () => {
    await result.current.mutateAsync("A reply");
  });
  expect(fetchMock).toHaveBeenCalledWith(
    "/v1/sessions/s/comments/c/replies",
    expect.objectContaining({ method: "POST", body: JSON.stringify({ body: "A reply" }) }),
  );
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["comments", "s"] });
});
it.each([400, 403, 404, 409])("preserves the server's inline error for %s", async (status) => {
  fetchMock.mockResolvedValue(
    response({ error: { code: "conflict", message: "Reply limit reached" } }, status),
  );
  const { result } = renderHook(() => useAddCommentReply("s", "c"), { wrapper: setup().wrapper });
  await act(async () => {
    await expect(result.current.mutateAsync("A reply")).rejects.toThrow("Reply limit reached");
  });
});
it("deletes the encoded reply and invalidates the comments prefix", async () => {
  fetchMock.mockResolvedValue(response({ deleted: true }));
  const { client, wrapper } = setup();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const { result } = renderHook(() => useDeleteCommentReply("s", "c"), { wrapper });
  await act(async () => {
    await result.current.mutateAsync("r/a");
  });
  expect(fetchMock).toHaveBeenCalledWith("/v1/sessions/s/comments/c/replies/r%2Fa", {
    method: "DELETE",
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["comments", "s"] });
});

it.each(["add", "delete"])(
  "keeps a successful %s after a stale initial GET completes",
  async (operation) => {
    let releaseFirst!: (value: Response) => void;
    let releaseRefresh!: (value: Response) => void;
    const first = new Promise<Response>((resolve) => {
      releaseFirst = resolve;
    });
    const refresh = new Promise<Response>((resolve) => {
      releaseRefresh = resolve;
    });
    const saved = { id: "saved" };
    const finalReplies = operation === "add" ? [saved] : [];
    let reads = 0;
    fetchMock.mockImplementation((_url, options) => {
      if (options?.method === "POST") return Promise.resolve(response(saved));
      if (options?.method === "DELETE") return Promise.resolve(response({ deleted: true }));
      return ++reads === 1 ? first : refresh;
    });
    const { client, wrapper } = setup();
    const { result } = renderHook(
      () => ({
        query: useCommentReplies("s", "c"),
        add: useAddCommentReply("s", "c"),
        remove: useDeleteCommentReply("s", "c"),
      }),
      { wrapper },
    );
    await act(async () => {
      if (operation === "add") await result.current.add.mutateAsync("New reply");
      else await result.current.remove.mutateAsync("saved");
    });
    await waitFor(() => expect(result.current.query.data).toEqual(finalReplies));
    expect(fetchMock.mock.calls[0]![1].signal.aborted).toBe(true);
    await act(async () => {
      releaseFirst(response({ replies: operation === "add" ? [] : [saved], next_cursor: null }));
    });
    expect(result.current.query.data).toEqual(finalReplies);
    await act(async () => {
      releaseRefresh(response({ replies: finalReplies, next_cursor: null }));
    });
    await waitFor(() =>
      expect(client.getQueryState(["comments", "s", "replies", "c", undefined])?.fetchStatus).toBe(
        "idle",
      ),
    );
    expect(result.current.query.data).toEqual(finalReplies);
  },
);

it("supersedes the first read when a fingerprint refresh advances the parent timestamp", async () => {
  let releaseFirst!: (value: Response) => void;
  fetchMock
    .mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        releaseFirst = resolve;
      }),
    )
    .mockResolvedValueOnce(response({ replies: [{ id: "agent-reply" }], next_cursor: null }));
  const { result, rerender } = renderHook(
    ({ updatedAt }) => useCommentReplies("s", "c", true, updatedAt),
    {
      wrapper: setup().wrapper,
      initialProps: { updatedAt: 1 },
    },
  );
  rerender({ updatedAt: 2 });
  await waitFor(() => expect(result.current.data).toEqual([{ id: "agent-reply" }]));
  expect(fetchMock.mock.calls[0]![1].signal.aborted).toBe(true);
  await act(async () => {
    releaseFirst(response({ replies: [], next_cursor: null }));
  });
  expect(result.current.data).toEqual([{ id: "agent-reply" }]);
});

it("removes a deleted reply from populated cache immediately", async () => {
  const { client, wrapper } = setup();
  const key = ["comments", "s", "replies", "c", 1];
  client.setQueryData(key, [{ id: "removed" }, { id: "kept" }]);
  fetchMock.mockResolvedValue(response({ deleted: true }));
  const { result } = renderHook(() => useDeleteCommentReply("s", "c"), { wrapper });
  await act(async () => {
    await result.current.mutateAsync("removed");
  });
  expect(client.getQueryData(key)).toEqual([{ id: "kept" }]);
});

it("refreshes a stale thread when a reply was already deleted elsewhere", async () => {
  const { client, wrapper } = setup();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  fetchMock.mockResolvedValue(
    response({ error: { code: "not_found", message: "Reply not found" } }, 404),
  );
  const { result } = renderHook(() => useDeleteCommentReply("s", "c"), { wrapper });
  await act(async () => {
    await expect(result.current.mutateAsync("removed")).rejects.toThrow("Reply not found");
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["comments", "s"] });
});
