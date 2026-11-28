// A fast-track account ready on the page's first server reads as unsupported once the server
// moves, so commits and suggestions take their host path and no read reaches the shell.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import type * as BasePathModule from "@/lib/basePath";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

const state = vi.hoisted(() => ({ server: "https://server-a.example.com" }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => state.server,
}));
vi.mock("@/lib/basePath", async (importOriginal) => ({
  ...(await importOriginal<typeof BasePathModule>()),
  getApiBasePath: () => "/api",
}));

import { useGitHubFastTrack } from "@/hooks/useGitHubFastTrack";
import { githubFastTrackSupport, queryGitHubFastTrack } from "@/lib/githubFastTrack";
import {
  githubFastTrackServerMoved as readPathServerMoved,
  resetGitHubFastTrackServerForTests as resetReadPathServerForTests,
} from "@/lib/githubFastTrackServer";

const ACCOUNT = { host: "github.com", login: "octo" };

afterEach(() => {
  resetReadPathServerForTests();
  state.server = "https://server-a.example.com";
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

it("reads as unsupported once the server moves, and sends the shell nothing", async () => {
  const githubQuery = vi.fn(async () => ({ ok: true, data: { login: "octo" } }));
  (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
    kind: "electron",
    githubQuery,
    getDesktopFeatures: async () => ({
      githubFastTrack: true,
      githubFastTrackOperations: ["account_status", "pull_request_commits"],
    }),
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useGitHubFastTrack(ACCOUNT, ["pull_request_commits"]), {
    wrapper,
  });
  await waitFor(() => expect(result.current.status).toBe("ready"));
  expect(readPathServerMoved()).toBe(false);
  const sent = githubQuery.mock.calls.length;

  state.server = "https://server-b.example.com";
  await act(async () => {
    readPathServerMoved();
    await Promise.resolve();
  });
  expect(result.current).toEqual({ status: "unsupported" });
  expect(await githubFastTrackSupport("pull_request_commits")).toBe("outdated");
  expect(await queryGitHubFastTrack("pull_request_commits", ACCOUNT)).toMatchObject({
    ok: false,
    error: { code: "unsupported" },
  });
  expect(githubQuery.mock.calls.length).toBe(sent);
});
