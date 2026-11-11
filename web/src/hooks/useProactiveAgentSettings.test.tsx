import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { setOmnigentHostConfig } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import {
  useProactiveAgentSettings,
  useSetProactiveAgentSetting,
  type ProactiveAgentSettings,
} from "./useProactiveAgentSettings";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("./useViewerId", () => ({ useViewerId: () => "alice" }));

let client: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, proactive_agent_available: true }}>
      {children}
    </CapabilitiesProvider>
  </QueryClientProvider>
);

function settings(enabled: boolean): ProactiveAgentSettings {
  return {
    workspace_enabled: true,
    user_enabled: enabled,
    enabled,
    can_manage_workspace: false,
  };
}

function response(body: ProactiveAgentSettings): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity, retry: false } } });
  setOmnigentHostConfig({ serverIdentity: "server-a" });
  vi.mocked(authenticatedFetch).mockReset();
});

afterEach(() => {
  cleanup();
  client.clear();
  setOmnigentHostConfig({});
});

it("keeps an accepted toggle when an older settings read lands after it", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(response(settings(true)));
  const { result } = renderHook(
    () => ({ settings: useProactiveAgentSettings(), set: useSetProactiveAgentSetting() }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.settings.settings?.enabled).toBe(true));

  // A background read of the stale cache is in flight when the viewer flips the switch.
  const staleRead = deferred<Response>();
  vi.mocked(authenticatedFetch).mockReturnValueOnce(staleRead.promise);
  void act(() => void result.current.settings.query.refetch());
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(2));
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(response(settings(false)));
  await act(async () => {
    await result.current.set.mutateAsync({ scope: "me", enabled: false });
  });
  await waitFor(() => expect(result.current.settings.settings?.enabled).toBe(false));

  // The read saw the old value; it must not undo the accepted setting.
  await act(async () => {
    staleRead.resolve(response(settings(true)));
    await new Promise<void>((settle) => {
      setTimeout(settle, 20);
    });
  });
  expect(result.current.settings.settings?.enabled).toBe(false);
  expect(authenticatedFetch).toHaveBeenCalledTimes(3);
});
