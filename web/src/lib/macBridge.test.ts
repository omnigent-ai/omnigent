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
  authorizeDesktopCodeHostRun,
  cancelDesktopCodeHostWrite,
  desktopBranchPullRequestChoices,
  desktopCodeHostQuery,
  desktopCodeHostWrite,
  desktopCodeHostWriteAs,
  desktopLocalPullRequestLinks,
  desktopMacAccounts,
  githubFastTrackQuery,
  hasCodeHostAccountsBridge,
  hasCodeHostBridge,
  hasCodeHostReadCancel,
  hasCodeHostWriteBridge,
  hasGitHubFastTrackBridge,
  hasLocalPullRequestLinksBridge,
  hasNamedAccountEvents,
  hasNamedCodeHostWriteBridge,
  onDesktopMacAccountsChanged,
  onDesktopNamedAccountsChanged,
  setDesktopMacAccount,
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

// What the shell sends each account listener: one change, as an object.
interface AccountChange {
  host: string;
  login?: string;
  generation: number;
}
let accountListeners: ((change: AccountChange) => void)[] = [];
let namedListeners: ((change: AccountChange) => void)[] = [];

function installShell() {
  accountListeners = [];
  namedListeners = [];
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
    codeHostWriteAuthorizeRun: vi.fn(async () => ({ ok: true, grantId: "grant-1" })),
    localPullRequestLinks: vi.fn(async () => ({ ok: true, data: [] })),
    codeHostAccounts: {
      get: vi.fn(async () => ({ intent: { mode: "automatic" }, accounts: [] })),
      set: vi.fn(async () => ({ intent: { mode: "automatic" }, accounts: [] })),
      onChanged: vi.fn((callback: (change: AccountChange) => void) => {
        accountListeners.push(callback);
        return () => {};
      }),
      onNamedChanged: vi.fn((callback: (change: AccountChange) => void) => {
        namedListeners.push(callback);
        return () => {};
      }),
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
    hasCodeHostWriteBridge(),
    hasLocalPullRequestLinksBridge(),
  ]).toEqual([true, true, true, true, true, true, true, true]);
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
    hasCodeHostWriteBridge(),
    hasLocalPullRequestLinksBridge(),
  ]).toEqual([false, false, false, false, false, false, false, false]);
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

it("keeps no saved link, account or change on this Mac after the server moves", async () => {
  const shell = installShell();
  switchServer();
  const list = { action: "list" as const, sessionId: "session-1", userId: "user-1" };
  expect(await desktopLocalPullRequestLinks(list)).toBeNull();
  expect(
    await desktopBranchPullRequestChoices({
      action: "list_branch_choices",
      userId: "user-1",
    } as never),
  ).toBeNull();
  expect(await setDesktopMacAccount("github.com", { mode: "automatic" })).toBeNull();
  const change = { requestId: "change-1" } as never;
  expect(await desktopCodeHostWrite(change)).toMatchObject({ error: { code: "unsupported" } });
  expect(await desktopCodeHostWriteAs(change)).toMatchObject({ error: { code: "unsupported" } });
  expect(await authorizeDesktopCodeHostRun(change)).toEqual({ ok: false, code: "unsupported" });
  expect(shell.localPullRequestLinks).not.toHaveBeenCalled();
  expect(shell.codeHostAccounts.set).not.toHaveBeenCalled();
  expect(shell.codeHostWrite).not.toHaveBeenCalled();
  expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  expect(shell.codeHostWriteAuthorizeRun).not.toHaveBeenCalled();
  // Letting go of a change sent before the move still reaches the shell.
  cancelDesktopCodeHostWrite("change-0");
  expect(shell.codeHostWriteCancel).toHaveBeenCalledWith("change-0");
});

it("drops saved links that land after the server moved", async () => {
  const shell = installShell();
  let answer: (value: unknown) => void = () => {};
  shell.localPullRequestLinks.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        answer = resolve;
      }) as never,
  );
  const pending = desktopLocalPullRequestLinks({
    action: "list",
    sessionId: "session-1",
    userId: "user-1",
  });
  await vi.waitFor(() => expect(shell.localPullRequestLinks).toHaveBeenCalled());
  switchServer();
  answer({ ok: true, data: [] });
  expect(await pending).toBeNull();
});

it("stops passing on this Mac's account changes after the server moves", () => {
  installShell();
  const accounts = vi.fn();
  const named = vi.fn();
  onDesktopMacAccountsChanged(accounts);
  onDesktopNamedAccountsChanged(named);
  accountListeners[0]!({ host: "github.com", generation: 1 });
  namedListeners[0]!({ host: "github.com", login: "octo", generation: 1 });
  switchServer();
  accountListeners[0]!({ host: "github.com", generation: 2 });
  namedListeners[0]!({ host: "github.com", login: "octo", generation: 2 });
  expect(accounts.mock.calls).toEqual([["github.com", 1]]);
  expect(named.mock.calls).toEqual([["github.com", "octo", 1]]);
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
