// After the page's server moves, every Mac read reads as a shell with no bridge: nothing is sent,
// an answer that lands after the move is dropped, and capability checks offer nothing.

import type * as BasePathModule from "@/lib/basePath";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

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

import {
  desktopCodeHostOperations,
  desktopCodeHostStatsSupport,
  desktopReadsByPriority,
  queryCodeHost,
} from "./desktopCodeHostReads";
import {
  desktopCodeHostQuery,
  desktopMacAccounts,
  githubFastTrackQuery,
  hasCodeHostAccountsBridge,
  hasCodeHostBridge,
  hasCodeHostReadCancel,
  hasGitHubFastTrackBridge,
  hasNamedAccountEvents,
  hasNamedCodeHostWriteBridge,
  useMacBridgeOpen,
} from "./macBridge";
import { readPathServerMoved, resetReadPathServerForTests } from "./readPathServer";

const ACCOUNT = { host: "github.com", login: "octo" };
const REQUEST = {
  provider: "github" as const,
  host: "github.com",
  operation: "pull_request_list_stats",
  params: {},
};

function installShell() {
  const shell = {
    kind: "electron",
    getDesktopFeatures: vi.fn(async () => ({
      codeHosts: [
        {
          id: "github",
          operations: ["pull_request_list_stats"],
          provenance: true,
          readPriority: 1,
        },
      ],
    })),
    codeHostQuery: vi.fn(async () => ({ ok: true, data: { reader: "octo" } })),
    codeHostQueryCancel: vi.fn(async () => undefined),
    codeHostWrite: vi.fn(),
    codeHostWriteCancel: vi.fn(),
    codeHostWriteAs: vi.fn(),
    githubQuery: vi.fn(async () => ({ ok: true, data: { login: "octo" } })),
    codeHostAccounts: {
      get: vi.fn(async () => ({ intent: { mode: "automatic" }, accounts: [] })),
      set: vi.fn(),
      onChanged: () => () => {},
      onNamedChanged: () => () => {},
    },
  };
  (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = shell;
  return shell;
}

function switchServer() {
  expect(readPathServerMoved()).toBe(false);
  state.server = "https://server-b.example.com";
}

afterEach(() => {
  resetReadPathServerForTests();
  state.server = "https://server-a.example.com";
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

it("offers every bridge on the page's first server", () => {
  installShell();
  expect([
    hasCodeHostBridge(),
    hasCodeHostReadCancel(),
    hasCodeHostAccountsBridge(),
    hasNamedAccountEvents(),
    hasNamedCodeHostWriteBridge(),
    hasGitHubFastTrackBridge(),
  ]).toEqual([true, true, true, true, true, true]);
});

it("offers no bridge and sends nothing to the shell after the server moves", async () => {
  const shell = installShell();
  switchServer();
  expect([
    hasCodeHostBridge(),
    hasCodeHostReadCancel(),
    hasCodeHostAccountsBridge(),
    hasNamedAccountEvents(),
    hasNamedCodeHostWriteBridge(),
    hasGitHubFastTrackBridge(),
  ]).toEqual([false, false, false, false, false, false]);
  expect(await desktopCodeHostQuery(REQUEST)).toMatchObject({
    ok: false,
    error: { code: "unsupported" },
  });
  expect(await githubFastTrackQuery("pull_request_list_stats", ACCOUNT)).toMatchObject({
    ok: false,
    error: { code: "unsupported" },
  });
  expect(await desktopMacAccounts("github.com")).toBeNull();
  expect(shell.codeHostQuery).not.toHaveBeenCalled();
  expect(shell.githubQuery).not.toHaveBeenCalled();
  expect(shell.codeHostAccounts.get).not.toHaveBeenCalled();
});

it("drops an answer that lands after the server moved", async () => {
  const shell = installShell();
  let answer: (value: unknown) => void = () => {};
  shell.codeHostQuery.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        answer = resolve;
      }) as never,
  );
  const pending = desktopCodeHostQuery(REQUEST);
  await vi.waitFor(() => expect(shell.codeHostQuery).toHaveBeenCalled());
  switchServer();
  answer({ ok: true, data: { reader: "octo" } });
  expect(await pending).toMatchObject({ ok: false, error: { code: "unsupported" } });
});

it("makes the code-host reads and their capability checks offer nothing after the server moves", async () => {
  const shell = installShell();
  expect(await desktopCodeHostStatsSupport("github")).toBe("supported");
  switchServer();
  expect(await queryCodeHost(REQUEST)).toMatchObject({ ok: false, error: { code: "unsupported" } });
  expect(await desktopCodeHostOperations("github")).toEqual([]);
  expect(await desktopReadsByPriority("github")).toBe(false);
  expect(await desktopCodeHostStatsSupport("github")).toBe("outdated");
  expect(shell.codeHostQuery).not.toHaveBeenCalled();
});

it("re-renders a reader once the server moves", async () => {
  installShell();
  const { result } = renderHook(() => useMacBridgeOpen());
  expect(result.current).toBe(true);
  state.server = "https://server-b.example.com";
  await act(async () => {
    readPathServerMoved();
    await Promise.resolve();
  });
  expect(result.current).toBe(false);
});
