import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getSessionHost, setSessionHost } from "@/lib/sessionHost";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { useRelatedSessionGroup } from "./useRelatedSessionLifecycle";
import { authenticatedFetch } from "@/lib/identity";
import {
  childSessionsQueryKey,
  fetchChildSessions,
  useChildSessions,
  useDescendantSession,
} from "./useChildSessions";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

function wrapper({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>{children}</CapabilitiesProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
});

describe("useChildSessions", () => {
  it("waits for capabilities so the shared roster includes archived peers on reload", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let info: ServerInfo | "loading" = "loading";
    const archivedPeer = {
      id: "archived-peer",
      archived: true,
      labels: { "omnigent.session_role": "peer" },
    };
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ object: "list", data: [archivedPeer] })),
    );
    const { result, rerender } = renderHook(
      () => ({
        roster: useChildSessions("root"),
        group: useRelatedSessionGroup("root"),
      }),
      {
        wrapper: ({ children }) => (
          <QueryClientProvider client={client}>
            <CapabilitiesProvider info={info}>{children}</CapabilitiesProvider>
          </QueryClientProvider>
        ),
      },
    );
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(result.current.roster.isLoading).toBe(true);
    expect(client.getQueryData(childSessionsQueryKey("root"))).toBeUndefined();
    info = { ...FALLBACK_SERVER_INFO, features: { related_sessions: true } };
    rerender();
    await waitFor(() => expect(result.current.group.hasPeers).toBe(true));
    expect(result.current.roster.children).toMatchObject([archivedPeer]);
    expect(authenticatedFetch).toHaveBeenCalledExactlyOnceWith(
      "/v1/sessions/root/child_sessions?include_archived=true",
    );
    expect(client.getQueryData(childSessionsQueryKey("root"))).toMatchObject([archivedPeer]);
    client.clear();
  });

  it("does not fetch child sessions for a provisional conversation", () => {
    const { result } = renderHook(() => useChildSessions("temp:pending-create"), { wrapper });

    expect(result.current.children).toEqual([]);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("useDescendantSession", () => {
  it("fetches the direct-child list when it is not cached", async () => {
    // WHY: a mirrored card can mount before anything loaded the ancestor's
    // child list; the lookup must fetch it rather than stay unlabeled.
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            {
              id: "conv_child",
              title: "researcher:auth",
              task_summary: "Investigate auth flow",
              tool: "researcher",
              session_name: "auth",
              current_task_status: "in_progress",
              busy: true,
            },
          ],
        }),
      ),
    );
    const { result } = renderHook(() => useDescendantSession("conv_parent", "conv_child"), {
      wrapper,
    });

    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current?.task_summary).toBe("Investigate auth flow"));
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv_parent/child_sessions");
  });
});

describe("fetchChildSessions pagination", () => {
  it("follows all pages so peers after the twentieth child are visible", async () => {
    const first = Array.from({ length: 20 }, (_, index) => ({ id: `helper-${index}` }));
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ object: "list", data: first, has_more: true, last_id: "helper-19" }),
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            object: "list",
            data: [{ id: "peer", labels: { "omnigent.session_role": "peer" }, archived: true }],
            has_more: false,
            last_id: "peer",
          }),
        ),
      );
    const children = await fetchChildSessions("parent", true);
    expect(children).toHaveLength(21);
    expect(children[20]).toMatchObject({
      id: "peer",
      labels: { "omnigent.session_role": "peer" },
      archived: true,
    });
    expect(authenticatedFetch).toHaveBeenLastCalledWith(
      "/v1/sessions/parent/child_sessions?after=helper-19&include_archived=true",
    );
  });

  it("rejects a repeated pagination cursor instead of looping forever", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            object: "list",
            data: [{ id: "child" }],
            has_more: true,
            last_id: "child",
          }),
        ),
    );
    await expect(fetchChildSessions("parent")).rejects.toThrow(
      "Invalid child-session pagination cursor",
    );
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });
});

it("uses a peer's own host before its snapshot and preserves routing for old summaries", async () => {
  setSessionHost("placement-root", "host-root");
  setSessionHost("known-child", "host-known");
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(
    new Response(
      JSON.stringify({
        object: "list",
        data: [
          { id: "remote-peer", labels: { "omnigent.session_role": "peer" }, host_id: "host-peer" },
          { id: "hostless-peer", labels: { "omnigent.session_role": "peer" }, host_id: null },
          { id: "legacy-helper" },
          { id: "known-child" },
        ],
      }),
    ),
  );
  await fetchChildSessions("placement-root");
  expect(getSessionHost("remote-peer")).toBe("host-peer");
  expect(getSessionHost("hostless-peer")).toBeNull();
  expect(getSessionHost("legacy-helper")).toBe("host-root");
  expect(getSessionHost("known-child")).toBe("host-known");
});
