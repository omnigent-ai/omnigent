// The cooldown follows the account the shell proves for Automatic, through the real read path:
// a cold start's limit is held, a switch to another account reads at once, and a switch back
// waits out the first account's wait.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { readOnThisMac } from "./desktopPullRequests";
import { macAccountChanged, macIdentity, resetDesktopReadPathForTests } from "./desktopReadPath";
import { githubCooldownKey, githubCooldowns } from "./githubCooldown";

const target = { provider: "github" as const, host: "github.com" };
const choiceAs = (login: string, generation: number) => ({
  ok: true,
  data: {
    intent: { mode: "automatic" },
    effective: { login, generation },
    accounts: [{ login, active: true, state: "success" }],
  },
});
const limited = () => ({
  ok: false,
  error: { code: "rate_limited", message: "GitHub is limiting requests right now." },
});

function installShell(get: ReturnType<typeof vi.fn>, codeHostQuery: ReturnType<typeof vi.fn>) {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostAccounts: { get, onChanged: () => () => {} },
    codeHostQuery,
    getDesktopFeatures: async () => ({
      codeHosts: [{ id: "github", operations: ["pull_request_overview"], accounts: true }],
    }),
  };
}

beforeEach(() => {
  resetDesktopReadPathForTests();
  githubCooldowns.reset();
});

afterEach(() => {
  githubCooldowns.reset();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("Automatic reads under a rate limit", () => {
  it("finds out the account on a cold start, so the first limit holds the next read", async () => {
    const get = vi.fn(async () => choiceAs("amy", 1));
    const codeHostQuery = vi.fn(async () => limited());
    installShell(get, codeHostQuery);
    expect(macIdentity("github.com")).toBeNull();
    await readOnThisMac(target, "pull_request_overview", {}).catch(() => {});
    await readOnThisMac(target, "pull_request_overview", {}).catch(() => {});
    expect(get).toHaveBeenCalled();
    expect(macIdentity("github.com")?.login).toBe("amy");
    expect(githubCooldowns.phase(githubCooldownKey("github.com", "amy"))).not.toBeNull();
    expect(codeHostQuery).toHaveBeenCalledTimes(1);
  });

  it("reads at once as the next account after a switch, and waits again on the way back", async () => {
    let choice = choiceAs("amy", 1);
    const get = vi.fn(async () => choice);
    const codeHostQuery = vi.fn(async () => limited());
    installShell(get, codeHostQuery);
    await readOnThisMac(target, "pull_request_overview", {}).catch(() => {});
    expect(codeHostQuery).toHaveBeenCalledTimes(1);

    choice = choiceAs("bo", 2);
    macAccountChanged("github", "github.com");
    codeHostQuery.mockResolvedValueOnce({
      ok: true,
      data: { repository: "acme/app", pr: {} },
      identity: { host: "github.com", login: "bo", generation: 2 },
    } as never);
    await readOnThisMac(target, "pull_request_overview", {}).catch(() => {});
    expect(codeHostQuery).toHaveBeenCalledTimes(2);

    choice = choiceAs("amy", 3);
    macAccountChanged("github", "github.com");
    await expect(readOnThisMac(target, "pull_request_overview", {})).rejects.toMatchObject({
      code: "rate_limited",
      local: true,
    });
    expect(codeHostQuery).toHaveBeenCalledTimes(2);
  });

  it("never puts a limit read before a switch down to the account after it", async () => {
    const checks: ((choice: unknown) => void)[] = [];
    const get = vi.fn(
      () =>
        new Promise((resolve) => {
          checks.push(resolve);
        }),
    );
    const codeHostQuery = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        error: { code: "rate_limited", message: "Limited.", retryAt: Date.now() + 60 * 60_000 },
      })
      .mockResolvedValue({
        ok: true,
        data: { repository: "acme/app", pr: {} },
        identity: { host: "github.com", login: "bo", generation: 2 },
      });
    installShell(get, codeHostQuery);
    // A cold read as amy is limited while the shell's account check is still out.
    await readOnThisMac(target, "pull_request_overview", {}).catch(() => {});
    expect(checks).toHaveLength(1);
    // The account switches to bo before either check answers.
    macAccountChanged("github", "github.com");
    checks[0]!(choiceAs("amy", 1));
    checks[1]!(choiceAs("bo", 2));
    await vi.waitFor(() => expect(macIdentity("github.com")?.login).toBe("bo"));
    expect(githubCooldowns.heldUntil(githubCooldownKey("github.com", "bo"))).toBeNull();
    expect(githubCooldowns.heldUntil(githubCooldownKey("github.com", "amy"))).toBeNull();
    await expect(readOnThisMac(target, "pull_request_overview", {})).resolves.toBeDefined();
    expect(codeHostQuery).toHaveBeenCalledTimes(2);
  });

  it("reads unheld when the shell names no account, e.g. signed out", async () => {
    const get = vi.fn(async () => ({
      ok: true,
      data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
    }));
    const codeHostQuery = vi.fn(async () => limited());
    installShell(get, codeHostQuery);
    await readOnThisMac(target, "pull_request_overview", {}).catch(() => {});
    await readOnThisMac(target, "pull_request_overview", {}).catch(() => {});
    expect(codeHostQuery).toHaveBeenCalledTimes(2);
  });
});
