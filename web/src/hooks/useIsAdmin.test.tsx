// `useIsAdmin` gates admin-only chrome on the `/v1/me` admin flag of the Server
// the app is bound to. `identity.ts` and `host.ts` keep that state at module
// scope, so each test resets modules and re-imports (as identity.test.ts does).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function serverFetcher(userId: string, isAdmin: boolean) {
  return vi.fn(async (path: string, _init?: RequestInit) =>
    jsonResponse(path === "/v1/me" ? { user_id: userId, is_admin: isAdmin } : {}),
  );
}

// One client for every (re)mount, like the embed's module-scope QueryClient.
function wrapperFor(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.resetModules();
});

it("reports the admin flag of the Server the host switched to", async () => {
  const { setOmnigentHostConfig } = await import("@/lib/host");
  const { useIsAdmin } = await import("./useIsAdmin");
  const wrapper = wrapperFor(new QueryClient());
  const serverA = serverFetcher("alice", true);
  const serverB = serverFetcher("bob", false);

  setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA });
  const onA = renderHook(() => useIsAdmin(), { wrapper });
  await waitFor(() => expect(onA.result.current).toBe(true));
  onA.unmount();

  // The host repoints the embed at Server B and remounts the app.
  setOmnigentHostConfig({ serverIdentity: "server-b", fetcher: serverB });
  const onB = renderHook(() => useIsAdmin(), { wrapper });

  await waitFor(() => expect(onB.result.current).toBe(false));
  expect(serverB.mock.calls.map((call) => call[0])).toContain("/v1/me");
});

it("settles to the resolved admin flag instead of a pre-resolution seed", async () => {
  const { setOmnigentHostConfig } = await import("@/lib/host");
  const { resolveIdentity } = await import("@/lib/identity");
  const { useIsAdmin } = await import("./useIsAdmin");
  let answer!: (response: Response) => void;
  const server = vi.fn((path: string, _init?: RequestInit) =>
    path === "/v1/me"
      ? new Promise<Response>((resolve) => {
          answer = resolve;
        })
      : Promise.resolve(jsonResponse({})),
  );

  setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: server });
  // The app kicks the probe off at mount; the hook may render before it lands.
  void resolveIdentity();
  const { result } = renderHook(() => useIsAdmin(), { wrapper: wrapperFor(new QueryClient()) });
  expect(result.current).toBe(false);

  await act(async () => {
    answer(jsonResponse({ user_id: "alice", is_admin: true }));
  });

  await waitFor(() => expect(result.current).toBe(true));
});
