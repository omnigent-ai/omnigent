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

// The busy child is on the second child-list page, as an older sibling.
function routeCosts(costs: (url: URL) => Response, childBusy: boolean) {
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    const url = new URL(String(input), "http://test.local");
    if (!url.pathname.endsWith("/child_sessions")) return costs(url);
    return new Response(
      JSON.stringify(
        url.searchParams.get("after") === null
          ? { data: [{ id: "conv_new", busy: false }], last_id: "conv_new", has_more: true }
          : { data: [{ id: "conv_child", busy: childBusy }], has_more: false },
      ),
    );
  });
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
    routeCosts(
      (url) =>
        new Response(
          JSON.stringify(
            url.searchParams.get("after") === null
              ? { data: [{ id: "conv_root", cost_usd: 1 }], last_id: "conv_root", has_more: true }
              : { data: [{ id: "conv_child", cost_usd: 0.2, estimated: true }], has_more: false },
          ),
        ),
      false,
    );
    const { result } = renderCosts(true);

    await waitFor(() => expect(result.current.data?.size).toBe(2));
    expect(result.current.data).toEqual(
      new Map([
        ["conv_root", { costUsd: 1, estimated: false }],
        ["conv_child", { costUsd: 0.2, estimated: true }],
      ]),
    );
    expect(costRequests().map((url) => url.searchParams.get("after"))).toEqual([null, "conv_root"]);
  });

  it("restarts once from the first page when a later cursor goes stale", async () => {
    routeCosts((url) => {
      const after = url.searchParams.get("after");
      if (after !== null && costRequests().length === 2) {
        return new Response('{"error":{"code":"stale_cursor"}}', { status: 400 });
      }
      return new Response(
        JSON.stringify(
          after === null
            ? { data: [{ id: "conv_root", cost_usd: 1 }], last_id: "conv_root", has_more: true }
            : { data: [{ id: "conv_child", cost_usd: 0.2 }], has_more: false },
        ),
      );
    }, false);
    const { result } = renderCosts(true);

    await waitFor(() => expect(result.current.data?.get("conv_child")?.costUsd).toBe(0.2));
    expect(costRequests().map((url) => url.searchParams.get("after"))).toEqual([
      null,
      "conv_root",
      null,
      "conv_root",
    ]);
  });

  it("fails rather than walking a third time when the restarted cursor goes stale too", async () => {
    routeCosts(
      (url) =>
        url.searchParams.get("after") === null
          ? new Response(
              JSON.stringify({ data: [{ id: "conv_root" }], last_id: "conv_root", has_more: true }),
            )
          : new Response('{"error":{"code":"stale_cursor"}}', { status: 400 }),
      false,
    );
    const { result } = renderCosts(true);

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(costRequests()).toHaveLength(4);
  });

  it.each([
    ["a route-missing body", '{"detail":"Not Found"}'],
    ["the Omnigent error envelope", '{"error":{"code":"not_found","message":"Not found"}}'],
  ])("treats any 404 (%s) as no breakdown and stops asking", async (_name, body) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    routeCosts(() => new Response(body, { status: 404 }), true);
    const { client, result } = renderCosts(true);

    await waitFor(() => expect(result.current.data).toBeNull());
    expect(result.current.isError).toBe(false);
    invalidateAgentCosts(client);
    // A busy child would otherwise poll; a server without the breakdown is not asked again.
    await vi.advanceTimersByTimeAsync(40_000);
    expect(costRequests()).toHaveLength(1);
  });

  it("keeps a 404 after the first page an error, not a missing breakdown", async () => {
    routeCosts(
      (url) =>
        url.searchParams.get("after") === null
          ? new Response(
              JSON.stringify({ data: [{ id: "conv_root" }], last_id: "conv_root", has_more: true }),
            )
          : new Response('{"error":{"code":"not_found"}}', { status: 404 }),
      false,
    );
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
    client.setQueryData(childSessionsQueryKey("conv_root"), [
      { id: "conv_new", busy: false },
      { id: "conv_child", busy: false },
    ]);
    await vi.advanceTimersByTimeAsync(45_000);
    expect(costRequests()).toHaveLength(2);
  });

  it("coalesces invalidations into one refetch", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    routeCosts(() => new Response('{"data":[]}'), false);
    const { client, result } = renderCosts(true);
    await waitFor(() => expect(result.current.data?.size).toBe(0));

    invalidateAgentCosts(client);
    invalidateAgentCosts(client);
    await vi.advanceTimersByTimeAsync(2_000);
    await waitFor(() => expect(costRequests()).toHaveLength(2));
    await vi.advanceTimersByTimeAsync(5_000);
    expect(costRequests()).toHaveLength(2);
  });
});
