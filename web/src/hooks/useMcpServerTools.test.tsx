import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { useMcpServerTools } from "./useMcpServerTools";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it.each([false, true])("probes lazily and retries an uncached busy response: %s", async (busy) => {
  vi.mocked(authenticatedFetch).mockImplementation(async () =>
    Response.json({
      tools: [{ name: "read", description: null }],
      connection: "connected",
      truncated: false,
    }),
  );
  if (busy)
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response(null, { status: 503 }));
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, rerender } = renderHook(
    ({ enabled }) =>
      useMcpServerTools("host id", "claude", "odd/server", "toolkit", {
        enabled,
        sourceId: "source-id",
      }),
    { wrapper, initialProps: { enabled: false } },
  );
  expect(authenticatedFetch).not.toHaveBeenCalled();
  rerender({ enabled: true });
  if (busy) {
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
    rerender({ enabled: false });
    rerender({ enabled: true });
  }
  await waitFor(() => expect(result.current.data?.connection).toBe("connected"));
  expect(authenticatedFetch).toHaveBeenCalledWith(
    "/v1/hosts/host%20id/mcp-servers/tools",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        harness: "claude",
        server: "odd/server",
        plugin: "toolkit",
        source_id: "source-id",
      }),
    }),
  );
  rerender({ enabled: false });
  rerender({ enabled: true });
  expect(authenticatedFetch).toHaveBeenCalledTimes(busy ? 2 : 1);
});
