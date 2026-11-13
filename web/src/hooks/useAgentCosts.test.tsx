import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { invalidateAgentCosts, useAgentCosts } from "./useAgentCosts";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

function renderCosts(enabled: boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, features: { session_overview: enabled } }}
      >
        {children}
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
  return { client, ...renderHook(() => useAgentCosts("conv_root"), { wrapper }) };
}

function costRequests(): URL[] {
  return vi
    .mocked(authenticatedFetch)
    .mock.calls.map(([input]) => new URL(String(input), "http://test.local"))
    .filter((url) => url.pathname.endsWith("/agent_costs"));
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
});

describe("useAgentCosts", () => {
  it("sends no request while the feature is off", () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("{}"));
    const { result } = renderCosts(false);

    expect(result.current.fetchStatus).toBe("idle");
    expect(costRequests()).toHaveLength(0);
  });

  it("follows every page of a large tree", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = new URL(String(input), "http://test.local");
      if (url.pathname.endsWith("/child_sessions")) return new Response('{"data":[]}');
      const first = url.searchParams.get("after") === null;
      return new Response(
        JSON.stringify(
          first
            ? { data: [{ id: "conv_root", cost_usd: 1 }], last_id: "conv_root", has_more: true }
            : { data: [{ id: "conv_child", cost_usd: 0.2, estimated: true }], has_more: false },
        ),
      );
    });
    const { result } = renderCosts(true);

    await waitFor(() => expect(result.current.data?.size).toBe(2));
    expect(result.current.data?.get("conv_child")).toEqual({ costUsd: 0.2, estimated: true });
    expect(costRequests().map((url) => url.searchParams.get("after"))).toEqual([null, "conv_root"]);
  });

  it("treats a missing endpoint as unsupported and stops asking", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) =>
      String(input).includes("/child_sessions")
        ? new Response('{"data":[{"id":"conv_child","busy":true}]}')
        : new Response("", { status: 404 }),
    );
    const { client, result } = renderCosts(true);

    await waitFor(() => expect(result.current.data).toBeNull());
    expect(result.current.isError).toBe(false);
    invalidateAgentCosts(client);
    // A busy child would otherwise poll; an unsupported server is not asked again.
    expect(client.getQueryState(["agent_costs", "conv_root"])?.isInvalidated).toBe(false);
    expect(costRequests()).toHaveLength(1);
  });
});
