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

it("probes only expanded rows and reuses fresh results", async () => {
  vi.mocked(authenticatedFetch).mockImplementation(async () =>
    Response.json({
      tools: [{ name: "read", description: null }],
      connection: "connected",
      truncated: false,
    }),
  );
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, rerender } = renderHook(
    ({ enabled }) => useMcpServerTools("host id", "claude", "odd/server", "toolkit", { enabled }),
    { wrapper, initialProps: { enabled: false } },
  );
  expect(authenticatedFetch).not.toHaveBeenCalled();
  rerender({ enabled: true });
  await waitFor(() => expect(result.current.data?.connection).toBe("connected"));
  expect(authenticatedFetch).toHaveBeenCalledWith(
    "/v1/hosts/host%20id/mcp-servers/tools",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ harness: "claude", server: "odd/server", plugin: "toolkit" }),
    }),
  );
  rerender({ enabled: false });
  rerender({ enabled: true });
  expect(authenticatedFetch).toHaveBeenCalledTimes(1);
});
