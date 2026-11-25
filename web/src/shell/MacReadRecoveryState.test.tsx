// Recovery from a failed Mac read: its ways out change the Mac's account for the host.

import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));

import { DesktopPullRequestError } from "@/lib/desktopPullRequests";
import { GITHUB_MAC_READ_OPERATIONS, resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { PULL_REQUESTS_PAGE_ACCOUNTS_FLAG, resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";

import { MacReadRecoveryState } from "./MacReadRecoveryState";

const PR = "https://github.com/acme/app/pull/42";

/**
 * A shell whose Mac reads github.com as a manually chosen @alice, offering named reads. `get`
 * answers the accounts; a pending one never does.
 */
function installShell({
  accounts = [
    { login: "alice", active: true, state: "success" },
    { login: "carol", active: false, state: "success" },
  ],
  effective = { login: "alice", generation: 1 } as { login: string; generation: number } | null,
  pending = false,
} = {}) {
  const set = vi.fn(async () => ({ ok: false }));
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async () => ({ ok: false, error: { code: "query_failed" } })),
    codeHostAccounts: {
      get: () =>
        pending
          ? new Promise(() => {})
          : Promise.resolve({
              ok: true,
              data: { intent: { mode: "manual", login: "alice" }, effective, accounts },
            }),
      set,
      onChanged: () => () => {},
      onNamedChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS],
          accounts: true,
          answerProvenance: 1,
          readAs: 1,
        },
      ],
      featureFlags: { names: [PULL_REQUESTS_PAGE_ACCOUNTS_FLAG] },
    }),
    featureFlags: {
      subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
        if (name === PULL_REQUESTS_PAGE_ACCOUNTS_FLAG) callback(true);
        return () => {};
      },
    },
  };
  return { set };
}

const missing = (readAs?: string) =>
  new DesktopPullRequestError(
    {
      code: "account_missing",
      message: readAs
        ? `@${readAs} isn't signed in to github.com on this Mac anymore.`
        : "The GitHub account chosen for github.com isn't signed in on this Mac anymore.",
    },
    { provider: "github", host: "github.com", url: PR, ...(readAs ? { readAs } : {}) },
  );

function renderRecovery(error: unknown) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MacReadRecoveryState error={error} hostCanAnswer={false} onRetry={() => {}} />
    </QueryClientProvider>,
  );
}

/** Let the shell's features, the flag and the accounts answer. */
const settle = () =>
  act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
  });

afterEach(() => {
  resetMacNamedReadsForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  localStorage.clear();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("recovery from a failed Mac read", () => {
  it("changes the Mac's account for the host", async () => {
    const shell = installShell({
      accounts: [
        { login: "bob", active: true, state: "success" },
        { login: "carol", active: false, state: "success" },
      ],
      effective: null,
    });
    renderRecovery(missing());
    await settle();
    await userEvent.click(await screen.findByRole("button", { name: "Use gh’s active account" }));
    expect(shell.set).toHaveBeenCalledWith("github.com", { mode: "automatic" });
  });
});
