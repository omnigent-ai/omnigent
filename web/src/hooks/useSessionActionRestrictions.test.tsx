import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as IdentityModule from "@/lib/identity";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { authenticatedFetch } from "@/lib/identity";
import { getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import {
  SANDBOX_FORK_UNSUPPORTED,
  SANDBOX_SWITCH_HOST_UNSUPPORTED,
  SESSION_ACTIONS_LOADING,
  SESSION_ACTIONS_UNAVAILABLE,
  type SessionActionSource,
} from "@/lib/sessionCapabilities";
import { useSessionActionRestrictions } from "./useSessionActionRestrictions";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSlim: vi.fn(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));

const getSessionMock = vi.mocked(getSessionSlim);
const fetchMock = vi.mocked(authenticatedFetch);
const clients = new Set<QueryClient>();
const unrestricted = { forkDisabledReason: undefined, switchHostDisabledReason: undefined };
const unsupported = {
  forkDisabledReason: SANDBOX_FORK_UNSUPPORTED,
  switchHostDisabledReason: SANDBOX_SWITCH_HOST_UNSUPPORTED,
};

function session(labels: Record<string, string> = {}): Session {
  return { id: "session-1", hostId: "host-1", labels } as Session;
}

function hostsResponse(provider: string | null = null) {
  return new Response(
    JSON.stringify({
      hosts: [{ host_id: "host-1", name: "Source", status: "online", sandbox_provider: provider }],
    }),
  );
}

function renderRestrictions(fallback?: SessionActionSource) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  clients.add(client);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return {
    client,
    ...renderHook(() => useSessionActionRestrictions("session-1", fallback), { wrapper }),
  };
}

beforeEach(() => {
  getSessionMock.mockReset().mockResolvedValue(session());
  fetchMock.mockReset().mockImplementation(async () => hostsResponse());
});

afterEach(() => {
  cleanup();
  for (const client of clients) client.clear();
  clients.clear();
});

describe("useSessionActionRestrictions", () => {
  it("keeps actions disabled while the snapshot is loading", () => {
    getSessionMock.mockReturnValue(new Promise(() => {}));
    const { result } = renderRestrictions();
    expect(result.current).toEqual({
      forkDisabledReason: SESSION_ACTIONS_LOADING,
      switchHostDisabledReason: SESSION_ACTIONS_LOADING,
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["arclet", "lakebox"])(
    "uses the fetched %s provider without a managed label",
    async (provider) => {
      fetchMock.mockImplementation(async () => hostsResponse(provider));
      const { result } = renderRestrictions();
      await waitFor(() => expect(result.current).toEqual(unsupported));
    },
  );

  it("retains a known managed restriction when both lookups fail", async () => {
    getSessionMock.mockRejectedValue(new Error("Session unavailable"));
    fetchMock.mockRejectedValue(new Error("Hosts unavailable"));
    const { client, result } = renderRestrictions({
      hostId: "host-1",
      labels: { "omnigent.host_type": "managed" },
    });
    expect(result.current).toEqual(unsupported);
    await waitFor(() =>
      expect(client.getQueryState(["session", "session-1"])?.status).toBe("error"),
    );
    await waitFor(() =>
      expect(client.getQueryState(["hosts", { includeSandbox: true }])?.status).toBe("error"),
    );
    expect(result.current).toEqual(unsupported);
  });

  it("explains a failed snapshot lookup instead of enabling actions", async () => {
    getSessionMock.mockRejectedValue(new Error("Session unavailable"));
    const { result } = renderRestrictions();
    await waitFor(() =>
      expect(result.current).toEqual({
        forkDisabledReason: SESSION_ACTIONS_UNAVAILABLE,
        switchHostDisabledReason: SESSION_ACTIONS_UNAVAILABLE,
      }),
    );
  });

  it("keeps an unknown host disabled on lookup failure and recovers after a successful refetch", async () => {
    fetchMock.mockRejectedValue(new Error("Hosts unavailable"));
    const { client, result } = renderRestrictions();
    await waitFor(() =>
      expect(result.current).toEqual({
        forkDisabledReason: SESSION_ACTIONS_UNAVAILABLE,
        switchHostDisabledReason: SESSION_ACTIONS_UNAVAILABLE,
      }),
    );
    fetchMock.mockImplementation(async () => hostsResponse());
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["hosts"] });
    });
    await waitFor(() => expect(result.current).toEqual(unrestricted));
  });

  it.each([false, true])(
    "uses the snapshot for a shared session with an unlisted host (managed=%s)",
    async (managed) => {
      getSessionMock.mockResolvedValue(session(managed ? { "omnigent.host_type": "managed" } : {}));
      fetchMock.mockImplementation(async () => new Response(JSON.stringify({ hosts: [] })));
      const { client, result } = renderRestrictions();
      await waitFor(() =>
        expect(client.getQueryState(["hosts", { includeSandbox: true }])?.status).toBe("success"),
      );
      expect(result.current).toEqual(managed ? unsupported : unrestricted);
    },
  );

  it("keeps supported actions available when a background refetch fails with usable cached data", async () => {
    const { client, result } = renderRestrictions();
    await waitFor(() => expect(result.current).toEqual(unrestricted));
    getSessionMock.mockRejectedValue(new Error("Session unavailable"));
    fetchMock.mockRejectedValue(new Error("Hosts unavailable"));
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["session", "session-1"] });
      await client.invalidateQueries({ queryKey: ["hosts"] });
    });
    expect(client.getQueryState(["session", "session-1"])?.status).toBe("error");
    expect(client.getQueryState(["hosts", { includeSandbox: true }])?.status).toBe("error");
    expect(result.current).toEqual(unrestricted);
  });
});
