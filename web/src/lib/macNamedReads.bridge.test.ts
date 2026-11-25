import type * as IdentityModule from "@/lib/identity";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));

import { readOnThisMac } from "./desktopPullRequests";
import {
  GITHUB_MAC_READ_OPERATIONS,
  refreshDesktopReadSupport,
  resetDesktopReadPathForTests,
} from "./desktopReadPath";
import {
  namedReadsAvailable,
  PULL_REQUESTS_PAGE_ACCOUNTS_FLAG,
  resetMacNamedReadsForTests,
} from "./macNamedReads";
import {
  macNamedReadScopeKey,
  macReadScopeKey,
  onMacReadScopeAdvanced,
  resetMacReadScopesForTests,
  type MacReadScopeKey,
  type MacReadScopeMove,
} from "./macReadScope";

const TARGET = { provider: "github" as const, host: "github.com", readAs: "bob" };

type Gap = "get" | "accounts" | "onNamedChanged" | "codeHostQuery" | "readAs" | null;

/** A shell offering named reads behind the flag, but for the one piece `gap` names. */
function installShell(gap: Gap) {
  const codeHostQuery = vi.fn(async (request: Record<string, unknown>) =>
    request.macLogin
      ? { ok: false, error: { code: "account_missing", message: "@bob isn't signed in" } }
      : { ok: false, error: { code: "query_failed", message: "no" } },
  );
  const accounts: Record<string, unknown> = {
    set: async () => ({ ok: false }),
    onChanged: () => () => {},
  };
  if (gap !== "get") accounts.get = async () => ({ ok: false });
  if (gap !== "onNamedChanged") accounts.onNamedChanged = () => () => {};
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    ...(gap === "codeHostQuery" ? {} : { codeHostQuery }),
    codeHostAccounts: accounts,
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [
            ...GITHUB_MAC_READ_OPERATIONS,
            "pull_request_inbox",
            "pull_request_list_stats",
          ],
          accounts: gap !== "accounts",
          answerProvenance: 1,
          ...(gap === "readAs" ? {} : { readAs: 1 }),
        },
      ],
      featureFlags: { names: [PULL_REQUESTS_PAGE_ACCOUNTS_FLAG] },
    }),
    featureFlags: {
      subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
        if (name === PULL_REQUESTS_PAGE_ACCOUNTS_FLAG) queueMicrotask(() => callback(true));
        return () => {};
      },
    },
  };
  return codeHostQuery;
}

/** Let the shell's features and the flag answer. */
async function settled() {
  namedReadsAvailable();
  await refreshDesktopReadSupport();
  await new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

afterEach(() => {
  resetMacNamedReadsForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("the bridge contract for reading as a named login", () => {
  it("is whole with reads, accounts, readAs and the named event", async () => {
    installShell(null);
    await settled();
    expect(namedReadsAvailable()).toBe(true);
  });

  it.each<Gap>(["get", "accounts", "onNamedChanged", "codeHostQuery", "readAs"])(
    "isn't, without %s, so a saved choice never reads as the login",
    async (gap) => {
      const codeHostQuery = installShell(gap);
      await settled();
      expect(namedReadsAvailable()).toBe(false);
      await readOnThisMac(TARGET, "pull_request_inbox", {}).catch(() => {});
      expect(codeHostQuery.mock.calls.some(([request]) => "macLogin" in request)).toBe(false);
    },
  );

  it("isn't in a browser, or on a shell without the bridge", async () => {
    await settled();
    expect(namedReadsAvailable()).toBe(false);
  });
});

describe("main refusing the named login", () => {
  it("moves only the login's scope, as refused, leaving the host's account's", async () => {
    installShell(null);
    await settled();
    const moves: [MacReadScopeKey, MacReadScopeMove][] = [];
    const stop = onMacReadScopeAdvanced((key, move) => moves.push([key, move]));
    await expect(readOnThisMac(TARGET, "pull_request_inbox", {})).rejects.toMatchObject({
      code: "account_missing",
      readAs: "bob",
    });
    stop();
    expect(moves).toEqual([[macNamedReadScopeKey("github.com", "bob", "user-1"), "refused"]]);
    expect(moves.map(([key]) => key)).not.toContainEqual(
      macReadScopeKey("github", "github.com", "user-1"),
    );
  });
});

describe("a list row's stats as a named login", () => {
  it("read as that login, so a named row follows its own reader's batch", async () => {
    const codeHostQuery = installShell(null);
    await settled();
    await readOnThisMac(TARGET, "pull_request_list_stats", { pull_requests: [] }).catch(() => {});
    expect(codeHostQuery).toHaveBeenCalledWith(
      expect.objectContaining({ operation: "pull_request_list_stats", macLogin: "bob" }),
    );
  });
});
