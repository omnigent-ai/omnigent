import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as IdentityModule from "@/lib/identity";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
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
const unlistedHost = {
  forkDisabledReason: undefined,
  switchHostDisabledReason: SESSION_ACTIONS_UNAVAILABLE,
};
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

function renderRestrictions(
  fallback?: SessionActionSource,
  cachedSession?: Session,
  // Omitted leaves server capabilities loading.
  info?: Partial<ServerInfo>,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  if (cachedSession) client.setQueryData(["session", "session-1"], cachedSession);
  clients.add(client);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      {info === undefined ? (
        children
      ) : (
        <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, ...info }}>
          {children}
        </CapabilitiesProvider>
      )}
    </QueryClientProvider>
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

  it("uses the fetched lakebox provider without a managed label", async () => {
    fetchMock.mockImplementation(async () => hostsResponse("lakebox"));
    const { result } = renderRestrictions();
    await waitFor(() => expect(result.current).toEqual(unsupported));
  });

  it.each<Record<string, string>>([{}, { "omnigent.host_type": "managed" }])(
    "offers an Arclet source a fork onto a fresh Arclet with labels %j",
    async (labels) => {
      getSessionMock.mockResolvedValue(session(labels));
      fetchMock.mockImplementation(async () => hostsResponse("arclet"));
      const { result } = renderRestrictions(undefined, undefined, {
        managed_sandboxes_enabled: true,
        sandbox_providers: ["lakebox", "arclet"],
      });
      await waitFor(() =>
        expect(result.current).toEqual({
          forkDisabledReason: undefined,
          forkSandboxProvider: "arclet",
          switchHostDisabledReason: SANDBOX_SWITCH_HOST_UNSUPPORTED,
        }),
      );
    },
  );

  it.each([
    ["offers no Arclet", { managed_sandboxes_enabled: true, sandbox_providers: ["lakebox"] }],
    ["can't launch sandboxes", { managed_sandboxes_enabled: false, sandbox_providers: ["arclet"] }],
  ])("keeps an Arclet fork unavailable on a server that %s", async (_server, info) => {
    fetchMock.mockImplementation(async () => hostsResponse("arclet"));
    const { result } = renderRestrictions(undefined, undefined, info);
    await waitFor(() =>
      expect(result.current).toEqual({ ...unsupported, forkSandboxProvider: "arclet" }),
    );
  });

  it("keeps an Arclet fork disabled until server capabilities load", async () => {
    fetchMock.mockImplementation(async () => hostsResponse("arclet"));
    const { result } = renderRestrictions();
    await waitFor(() => expect(result.current.forkSandboxProvider).toBe("arclet"));
    expect(result.current.forkDisabledReason).toBe(SESSION_ACTIONS_LOADING);
  });

  it("retains a known managed restriction when both lookups fail", async () => {
    getSessionMock.mockRejectedValue(new Error("Session unavailable"));
    fetchMock.mockRejectedValue(new Error("Hosts unavailable"));
    const { client, result } = renderRestrictions({
      hostId: "host-1",
      labels: { "omnigent.host_type": "managed" },
    });
    // Until the host lookup settles, the provider could still be a forkable Arclet.
    expect(result.current).toEqual({ ...unsupported, forkDisabledReason: SESSION_ACTIONS_LOADING });
    await waitFor(() =>
      expect(client.getQueryState(["session", "session-1"])?.status).toBe("error"),
    );
    await waitFor(() =>
      expect(client.getQueryState(["hosts", { includeSandbox: true }])?.status).toBe("error"),
    );
    expect(result.current).toEqual(unsupported);
  });

  it("reports a managed source as loading, not unsupported, while its host loads", async () => {
    let resolveHosts: (response: Response) => void = () => {};
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveHosts = resolve;
        }),
    );
    const { result } = renderRestrictions(undefined, session({ "omnigent.host_type": "managed" }), {
      managed_sandboxes_enabled: true,
      sandbox_providers: ["arclet"],
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(result.current.forkDisabledReason).toBe(SESSION_ACTIONS_LOADING);

    act(() => resolveHosts(hostsResponse("arclet")));

    await waitFor(() => expect(result.current.forkSandboxProvider).toBe("arclet"));
    expect(result.current.forkDisabledReason).toBeUndefined();
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
      expect(result.current).toEqual(managed ? unsupported : unlistedHost);
    },
  );

  it("enables switching after an unlisted source host becomes available", async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ hosts: [] })));
    const { client, result } = renderRestrictions();
    await waitFor(() => expect(result.current).toEqual(unlistedHost));
    fetchMock.mockImplementation(async () => hostsResponse());
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["hosts"] });
    });
    await waitFor(() => expect(result.current).toEqual(unrestricted));
  });

  it.each([false, true])(
    "preserves hostless session capabilities without requesting hosts (managed=%s)",
    async (managed) => {
      getSessionMock.mockResolvedValue({
        ...session(managed ? { "omnigent.host_type": "managed" } : {}),
        hostId: undefined,
      });
      const { client, result } = renderRestrictions();
      await waitFor(() =>
        expect(client.getQueryState(["session", "session-1"])?.status).toBe("success"),
      );
      expect(result.current).toEqual(managed ? unsupported : unrestricted);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each([{ hostId: "host-1" }, { host_id: "host-1" }])(
    "uses the loaded hostless snapshot instead of a stale fallback %j",
    (fallback) => {
      const { result } = renderRestrictions(fallback, { ...session(), hostId: undefined });
      expect(result.current).toEqual(unrestricted);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each([false, true])(
    "keeps supported actions available after failed background refetches (host listed=%s)",
    async (hostListed) => {
      if (!hostListed) {
        fetchMock.mockImplementation(async () => new Response(JSON.stringify({ hosts: [] })));
      }
      const { client, result, rerender } = renderRestrictions();
      const expected = hostListed ? unrestricted : unlistedHost;
      await waitFor(() => expect(result.current).toEqual(expected));
      getSessionMock.mockRejectedValue(new Error("Session unavailable"));
      fetchMock.mockRejectedValue(new Error("Hosts unavailable"));
      await act(async () => {
        await client.invalidateQueries({ queryKey: ["session", "session-1"] });
        await client.invalidateQueries({ queryKey: ["hosts"] });
      });
      expect(client.getQueryState(["session", "session-1"])?.status).toBe("error");
      expect(client.getQueryState(["hosts", { includeSandbox: true }])?.status).toBe("error");
      rerender();
      expect(result.current).toEqual(expected);
    },
  );
});
