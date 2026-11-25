import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

const { currentUser, identityListeners } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  identityListeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
}));

import {
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  rememberGitHubHosts,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";

import {
  resetReadSourceFailuresForTests,
  setReadSourceFailure,
  useReadSource,
  useReadSourceFailure,
} from "./useReadSource";

const GHES_PR = "https://ghe.example.com/team/project/pull/7";

function installShell(github: Record<string, unknown> | null) {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async (request: { operation: string }) =>
      request.operation === "account_list"
        ? { ok: true, data: [{ login: "octo", active: true, state: "success" }] }
        : { ok: false, error: { code: "unsupported", message: "Not in this test." } },
    ),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({ codeHosts: github ? [github] : [] }),
  };
}

afterEach(() => {
  resetDesktopReadPathForTests();
  resetReadSourceFailuresForTests();
  identityListeners.clear();
  githubMacReadsPreference.write("automatic");
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("useReadSource", () => {
  it.each(["on", "off"] as const)(
    "keeps a known Enterprise PR's choices through Mac, session and back, with Settings %s",
    async (setting) => {
      installShell({ id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true });
      githubMacReadsPreference.write(setting);
      // The session vouches for the host, as its info does for an Enterprise PR it lists.
      rememberGitHubHosts(["ghe.example.com"]);
      await act(async () => {
        await desktopReadTarget(GHES_PR);
      });
      const { result } = renderHook(() => useReadSource(GHES_PR));
      expect(result.current).toMatchObject({
        offered: true,
        provider: "github",
        host: "ghe.example.com",
        automaticPick: setting === "on" ? "mac" : "host",
      });
      act(() => result.current.choose("mac"));
      expect(result.current.route.kind).toBe("mac");
      act(() => result.current.choose("session"));
      expect(result.current).toMatchObject({ offered: true, host: "ghe.example.com" });
      expect(result.current.route.kind).toBe("host");
      act(() => result.current.choose("mac"));
      expect(result.current.route.kind).toBe("mac");
    },
  );

  it.each([
    [
      "only the overview read",
      { id: "github", operations: ["pull_request_overview"], accounts: true },
    ],
    ["no account list", { id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS] }],
    ["no GitHub at all", null],
  ])("offers nothing to choose on a shell with %s", async (_case, github) => {
    installShell(github);
    githubMacReadsPreference.write("on");
    await act(async () => {
      await desktopReadTarget("https://github.com/acme/app/pull/1");
    });
    const { result } = renderHook(() => useReadSource("https://github.com/acme/app/pull/1"));
    expect(result.current.offered).toBe(false);
  });

  it("clears a failed switch's message once a change goes through, for any spelling", async () => {
    installShell({ id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true });
    await act(async () => {
      await desktopReadTarget(GHES_PR);
    });
    const { result } = renderHook(() => ({
      source: useReadSource(GHES_PR),
      failure: useReadSourceFailure(`${GHES_PR}/files`),
    }));
    act(() =>
      setReadSourceFailure(GHES_PR.replace("team", "Team"), "Couldn’t change the account."),
    );
    expect(result.current.failure).toBe("Couldn’t change the account.");
    act(() => result.current.source.choose("session"));
    expect(result.current.failure).toBeNull();
  });

  it("forgets a failed switch's message when the workspace user changes", () => {
    const { result } = renderHook(() => useReadSourceFailure(GHES_PR));
    act(() => setReadSourceFailure(GHES_PR, "Couldn’t change the account."));
    expect(result.current).toBe("Couldn’t change the account.");
    act(() => {
      currentUser.id = "user-2";
      for (const listener of identityListeners) listener();
    });
    expect(result.current).toBeNull();
  });
});
