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
  expect(client.getQueryData(["comments", "s/a", "replies", "c/b"])).toEqual(result.current.data);
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
it.each([400, 409])("preserves the server's inline error for %s", async (status) => {
  fetchMock.mockResolvedValue(response({ detail: "Reply limit reached" }, status));
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
