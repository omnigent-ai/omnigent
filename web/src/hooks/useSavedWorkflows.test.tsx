import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";

import { useDeleteWorkflow, useSavedWorkflows } from "./useSavedWorkflows";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ workflows: true }) }));

function renderWithClient<T>(hook: () => T, client = new QueryClient()) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, ...renderHook(hook, { wrapper }) };
}

describe("useSavedWorkflows", () => {
  afterEach(cleanup);

  it("reports a failed load", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(null, { status: 500, statusText: "Server Error" }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderWithClient(() => useSavedWorkflows(), client);

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.isLoading).toBe(false);
    expect(result.current.workflows).toEqual([]);
  });
});

describe("useDeleteWorkflow", () => {
  afterEach(cleanup);

  it("deletes the workflow by ID, then reloads the list", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response(null, { status: 204 }));
    const { client, result } = renderWithClient(() => useDeleteWorkflow());
    const invalidate = vi.spyOn(client, "invalidateQueries");

    await act(() => result.current.mutateAsync("0123abcd"));

    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/workflows/0123abcd", {
      method: "DELETE",
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["saved-workflows"] });
  });

  it("fails on an error status", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(null, { status: 404, statusText: "Not Found" }),
    );
    const { result } = renderWithClient(() => useDeleteWorkflow());

    await act(async () => {
      await expect(result.current.mutateAsync("missing")).rejects.toThrow("404 Not Found");
    });
  });
});
