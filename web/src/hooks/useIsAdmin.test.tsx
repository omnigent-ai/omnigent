import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type PropsWithChildren, useEffect, useState } from "react";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { useIsAdmin } from "./useIsAdmin";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";

vi.mock("@/lib/identity", () => ({
  getCurrentIsAdmin: vi.fn(),
  resolveIdentity: vi.fn(),
}));

beforeEach(() => vi.resetAllMocks());
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.cookie = "omnigent_admin_view=; Max-Age=0; Path=/";
});

it.each([true, false])(
  "resolves a pending identity to admin=%s without waiting for cache expiry",
  async (isAdmin) => {
    let finishIdentity!: (id: string) => void;
    const pendingIdentity = new Promise<string>((resolve) => {
      finishIdentity = resolve;
    });
    vi.mocked(getCurrentIsAdmin).mockReturnValue(false);
    vi.mocked(resolveIdentity).mockReturnValue(pendingIdentity);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useIsAdmin(), {
      wrapper: ({ children }) => (
        <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        </CapabilitiesProvider>
      ),
    });

    expect(result.current).toBe(false);
    expect(resolveIdentity).toHaveBeenCalledOnce();
    vi.mocked(getCurrentIsAdmin).mockReturnValue(isAdmin);
    await act(async () => {
      finishIdentity("viewer");
      await pendingIdentity;
    });
    expect(client.getQueryData(["identity-is-admin"])).toBe(isAdmin);
    expect(client.getQueryState(["identity-is-admin"])?.dataUpdatedAt).toBeGreaterThan(0);
  },
);

it.each([true, undefined, "failed"])(
  "settles admin access after deferred capabilities advertise support=%s",
  async (supported) => {
    vi.resetModules();
    vi.doUnmock("@/lib/identity");
    let finishInfo!: (response: Response) => void;
    const pendingInfo = new Promise<Response>((resolve) => {
      finishInfo = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        url === "/v1/info"
          ? pendingInfo
          : Promise.resolve(new Response(JSON.stringify({ user_id: "admin", is_admin: true }))),
      ),
    );
    document.cookie = "omnigent_admin_view=off; Path=/";
    const { resolveServerInfo } = await import("@/lib/capabilities");
    const info = resolveServerInfo();
    const { getCurrentAdminEligibility, resolveIdentity: resolveRealIdentity } =
      await import("@/lib/identity");
    const pendingIdentity = resolveRealIdentity();
    const { useIsAdmin: useRealIsAdmin } = await import("./useIsAdmin");
    const { CapabilitiesProvider: RealCapabilitiesProvider, useServerInfo: useRealServerInfo } =
      await import("@/lib/CapabilitiesContext");
    const { createBootServerInfo, isBootFallbackInfo } = await import("@/lib/bootCapabilities");
    const boot = createBootServerInfo(info, 0);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    function Wrapper({ children }: PropsWithChildren) {
      const [serverInfo, setServerInfo] = useState<ServerInfo | "loading">("loading");
      useEffect(() => {
        void boot.initial.then(setServerInfo);
        void boot.settled.then(setServerInfo);
      }, []);
      return (
        <RealCapabilitiesProvider info={serverInfo}>
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        </RealCapabilitiesProvider>
      );
    }
    const { result } = renderHook(() => ({ admin: useRealIsAdmin(), info: useRealServerInfo() }), {
      wrapper: Wrapper,
    });

    await waitFor(() => expect(getCurrentAdminEligibility()).toBe(true));
    await expect(pendingIdentity).resolves.toBe("admin");
    await waitFor(() => expect(isBootFallbackInfo(result.current.info)).toBe(true));
    expect(result.current.admin).toBe(false);
    expect(client.getQueryState(["identity-is-admin"])?.dataUpdatedAt).toBe(0);
    await act(async () => {
      finishInfo(
        new Response(
          supported === "failed" ? "invalid JSON" : JSON.stringify({ admin_view: supported }),
        ),
      );
      await info;
    });
    await waitFor(() =>
      expect(client.getQueryState(["identity-is-admin"])?.fetchStatus).toBe("idle"),
    );
    await waitFor(() =>
      expect(client.getQueryState(["identity-is-admin"])?.dataUpdatedAt).toBeGreaterThan(0),
    );
    await waitFor(() => expect(result.current.admin).toBe(supported === undefined));
  },
);
