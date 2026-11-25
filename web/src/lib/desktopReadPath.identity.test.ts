import type * as IdentityModule from "@/lib/identity";
import { afterEach, describe, expect, it, vi } from "vitest";

const { currentUser } = vi.hoisted(() => ({ currentUser: { id: "user-1" as string | null } }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: () => () => {},
}));

import {
  discoverMacIdentity,
  ensureMacIdentity,
  macAccountsChangedForTests,
  macIdentity,
  macIdentityChecking,
  resetDesktopReadPathForTests,
} from "./desktopReadPath";

interface AccountCheck {
  resolve: (effective: { login: string; generation: number } | null) => void;
}

/** A shell whose account answers wait for the test, one per request. */
function accountsShell() {
  const checks: AccountCheck[] = [];
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(),
    codeHostAccounts: {
      get: () =>
        new Promise((resolve) => {
          checks.push({
            resolve: (effective) =>
              resolve({
                ok: true,
                data: { intent: { mode: "automatic" }, effective, accounts: [] },
              }),
          });
        }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({ codeHosts: [] }),
  };
  return checks;
}

const settle = () =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

afterEach(() => {
  resetDesktopReadPathForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  currentUser.id = "user-1";
});

describe("ensureMacIdentity, for a page that never reads as the host's account", () => {
  it("names the account main reads the host as, asking once", async () => {
    const checks = accountsShell();
    ensureMacIdentity("github.com");
    ensureMacIdentity("GitHub.com");
    expect(checks).toHaveLength(1);
    expect(macIdentityChecking("github.com")).toBe(true);
    checks[0]!.resolve({ login: "alice", generation: 1 });
    await settle();
    expect(macIdentity("github.com")).toEqual({ login: "alice", generation: 1 });
    expect(macIdentityChecking("github.com")).toBe(false);
    ensureMacIdentity("github.com");
    expect(checks).toHaveLength(1);
  });

  it("refuses a startup answer older than a change main reported meanwhile, and asks again", async () => {
    const checks = accountsShell();
    ensureMacIdentity("github.com");
    // The host's account changed to generation 2 while the first ask was out.
    macAccountsChangedForTests("github.com", 2);
    checks[0]!.resolve({ login: "alice", generation: 1 });
    await settle();
    expect(macIdentity("github.com")).toBeNull();
    expect(checks).toHaveLength(2);
    expect(macIdentityChecking("github.com")).toBe(true);
    checks[1]!.resolve({ login: "carol", generation: 2 });
    await settle();
    expect(macIdentity("github.com")).toEqual({ login: "carol", generation: 2 });
  });

  it("stops asking a main that keeps naming an account older than its change, and keeps none", async () => {
    const checks = accountsShell();
    ensureMacIdentity("github.com");
    macAccountsChangedForTests("github.com", 2);
    for (let answer = 0; answer < 10 && checks[answer]; answer += 1) {
      checks[answer]!.resolve({ login: "alice", generation: 1 });
      // eslint-disable-next-line no-await-in-loop -- each answer prompts the next ask.
      await settle();
    }
    expect(checks).toHaveLength(4);
    expect(macIdentity("github.com")).toBeNull();
    expect(macIdentityChecking("github.com")).toBe(false);
  });

  it("lets a test reset end an account lookup still out, so it names nobody after it", async () => {
    const before = accountsShell();
    const lookup = discoverMacIdentity("github.com");
    resetDesktopReadPathForTests();
    const after = accountsShell();
    ensureMacIdentity("github.com");
    after[0]!.resolve({ login: "carol", generation: 1 });
    await settle();
    expect(macIdentity("github.com")?.login).toBe("carol");
    before[0]!.resolve({ login: "alice", generation: 1 });
    expect(await lookup).toBeNull();
  });

  it("asks again for the next workspace user, ignoring the previous one's late answer", async () => {
    const checks = accountsShell();
    ensureMacIdentity("github.com");
    currentUser.id = "user-2";
    // The same host for the next user: its own ask, though the previous one is still out.
    ensureMacIdentity("github.com");
    expect(checks).toHaveLength(2);
    checks[0]!.resolve({ login: "alice", generation: 1 });
    await settle();
    expect(macIdentity("github.com")).toBeNull();
    expect(macIdentityChecking("github.com")).toBe(true);
    checks[1]!.resolve({ login: "dana", generation: 1 });
    await settle();
    expect(macIdentity("github.com")).toEqual({ login: "dana", generation: 1 });
  });
});
