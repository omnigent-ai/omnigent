import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { childSessionsQueryKey } from "./useChildSessions";
import { usePromoteSession } from "./usePromoteSession";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const authenticatedFetchMock = vi.mocked(authenticatedFetch);

function wrapperWith(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children);
  };
}

describe("usePromoteSession", () => {
  beforeEach(() => authenticatedFetchMock.mockReset());

  it("posts the promotion and invalidates only affected topology caches", async () => {
    const promoted = { id: "conv_b", parentSessionId: null, rootConversationId: "conv_b" };
    authenticatedFetchMock.mockResolvedValue(
      new Response(JSON.stringify(promoted), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => usePromoteSession(), { wrapper: wrapperWith(client) });

    await act(async () => {
      await result.current.mutateAsync({ sessionId: "conv_b", previousParentId: "conv_a" });
    });

    expect(authenticatedFetchMock).toHaveBeenCalledWith("/v1/sessions/conv_b/promote", {
      method: "POST",
    });
    expect(client.getQueryData(["session", "conv_b"])).toEqual(promoted);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: childSessionsQueryKey("conv_a") });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["session", "conv_b"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["rootSessionId"] });
    expect(invalidate).toHaveBeenCalledTimes(4);
  });

  it("turns a forbidden response into concise UI copy", async () => {
    authenticatedFetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: { message: "internal permission detail" } }), {
        status: 403,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderHook(() => usePromoteSession(), { wrapper: wrapperWith(client) });

    await expect(
      result.current.mutateAsync({ sessionId: "conv_b", previousParentId: "conv_a" }),
    ).rejects.toThrow("You don't have permission to promote this agent.");
  });
});
