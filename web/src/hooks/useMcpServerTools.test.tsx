import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { useMcpServerTools } from "./useMcpServerTools";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

function wrapperFor(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

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
  const { result, rerender } = renderHook(
    ({ enabled }) =>
      useMcpServerTools("host id", "claude", "odd/server", "toolkit", {
        enabled,
        sourceId: "source-id",
      }),
    { wrapper: wrapperFor(new QueryClient()), initialProps: { enabled: false } },
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

it("re-probes a needs-auth server once the host's sign-in cache expires", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.mocked(authenticatedFetch).mockImplementation(async () =>
    Response.json({ tools: [], connection: "connected", truncated: false }),
  );
  vi.mocked(authenticatedFetch).mockImplementationOnce(async () =>
    Response.json({
      tools: [],
      connection: "needs_auth",
      truncated: false,
      harness_sign_in: "available",
    }),
  );
  const { result, rerender } = renderHook(
    ({ enabled }) => useMcpServerTools("host", "claude", "docs", undefined, { enabled }),
    { wrapper: wrapperFor(new QueryClient()), initialProps: { enabled: true } },
  );
  await waitFor(() => expect(result.current.data?.harness_sign_in).toBe("available"));
  const reopenAfter = async (milliseconds: number) => {
    rerender({ enabled: false });
    vi.setSystemTime(Date.now() + milliseconds);
    rerender({ enabled: true });
    await act(async () => {});
  };

  await reopenAfter(29_999);
  expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  // A sign-in made outside Omnigent shows on the next expand.
  await reopenAfter(1);
  await waitFor(() => expect(result.current.data?.connection).toBe("connected"));
  expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  await reopenAfter(60_000);
  expect(authenticatedFetch).toHaveBeenCalledTimes(2);
});
