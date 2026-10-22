import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePublicSharingMaxLevel, useSetSharing } from "./useSharing";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const fetchMock = vi.mocked(authenticatedFetch);

function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  fetchMock.mockReset();
});
afterEach(cleanup);

describe("public sharing ceiling", () => {
  it.each([undefined, null, "manage", 2, true])(
    "treats an invalid or absent %s capability as Read",
    async (value) => {
      fetchMock.mockResolvedValue(
        new Response(JSON.stringify({ public_sharing_max_level: value })),
      );
      const { result } = renderHook(() => usePublicSharingMaxLevel(true), { wrapper: wrapper() });
      await waitFor(() => expect(result.current.data).toBe("read"));
    },
  );

  it("fails closed on a network error", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => usePublicSharingMaxLevel(true), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toBe("read"));
  });

  it("updates the visible ceiling when the admin changes it", async () => {
    let ceiling = "read";
    fetchMock.mockImplementation(async (_url, init) => {
      if (init?.method === "PUT") ceiling = "edit";
      return new Response(JSON.stringify({ public_sharing_max_level: ceiling }));
    });
    const { result } = renderHook(
      () => ({
        ceiling: usePublicSharingMaxLevel(true),
        update: useSetSharing(),
      }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.ceiling.data).toBe("read"));
    await result.current.update.mutateAsync({ public_sharing_max_level: "edit" });
    await waitFor(() => expect(result.current.ceiling.data).toBe("edit"));
  });
});
