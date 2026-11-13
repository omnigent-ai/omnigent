import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { invalidateAgentCosts, useAgentCosts } from "./useAgentCosts";
import { childSessionsQueryKey } from "./useChildSessions";

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

function routeCosts(costs: () => Response, childBusy: boolean) {
  vi.mocked(authenticatedFetch).mockImplementation(async (input) =>
    String(input).includes("/child_sessions")
      ? new Response(JSON.stringify({ data: [{ id: "conv_child", busy: childBusy }] }))
      : costs(),
  );
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
});

afterEach(() => {
  vi.useRealTimers();
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

  it("treats a missing route as unsupported and stops asking", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    routeCosts(() => new Response('{"detail":"Not Found"}', { status: 404 }), true);
    const { client, result } = renderCosts(true);

    await waitFor(() => expect(result.current.data).toBeNull());
    expect(result.current.isError).toBe(false);
    invalidateAgentCosts(client);
    // A busy child would otherwise poll; an unsupported server is not asked again.
    await vi.advanceTimersByTimeAsync(40_000);
    expect(costRequests()).toHaveLength(1);
  });

  it("keeps a missing session an error rather than an unsupported server", async () => {
    routeCosts(() => new Response('{"error":{"code":"not_found"}}', { status: 404 }), false);
    const { result } = renderCosts(true);

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
  });

  it("polls while a child works and stops once it settles", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    routeCosts(() => new Response('{"data":[]}'), true);
    const { client, result } = renderCosts(true);
    await waitFor(() => expect(result.current.data?.size).toBe(0));

    await vi.advanceTimersByTimeAsync(15_000);
    await waitFor(() => expect(costRequests()).toHaveLength(2));
    client.setQueryData(childSessionsQueryKey("conv_root"), [{ id: "conv_child", busy: false }]);
    await vi.advanceTimersByTimeAsync(45_000);
    expect(costRequests()).toHaveLength(2);
  });
});
