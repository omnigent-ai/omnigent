import { describe, expect, it } from "vitest";

import { DesktopPullRequestError } from "./desktopPullRequests";
import { isMacSignInError, macReadRecovery } from "./macReadRecovery";
import type { MacAccountChoice } from "./nativeBridge";

const GHES = "ghe.example.com";
const failure = (code: string, provider: "github" | "gitspoke" = "github", host = GHES) =>
  new DesktopPullRequestError(
    { code: code as never, message: `No GitHub account for ${host} is signed in on this Mac.` },
    { provider, host, url: `https://${host}/team/project/pull/7` },
  );

const choice = (
  logins: string[],
  intent: MacAccountChoice["intent"] = { mode: "automatic" },
): MacAccountChoice => ({
  intent,
  effective: null,
  accounts: logins.map((login, index) => ({
    login,
    active: index === 0,
    state: "success" as const,
  })),
});

const recover = (error: unknown, options: Partial<Parameters<typeof macReadRecovery>[1]> = {}) =>
  macReadRecovery(error, {
    signInAvailable: false,
    choice: null,
    hostCanAnswer: false,
    ...options,
  });

describe("macReadRecovery", () => {
  it.each(["signed_out", "account_missing", "account_invalid"])(
    "offers recovery for %s on the failing read's host",
    (code) => {
      expect(recover(failure(code))).toMatchObject({
        host: GHES,
        code,
        terminalCommand: `gh auth login --hostname ${GHES}`,
      });
    },
  );

  it("names github.com's sign-in without a hostname", () => {
    expect(recover(failure("signed_out", "github", "github.com"))?.terminalCommand).toBe(
      "gh auth login",
    );
  });

  it("leaves other failures, GitSpoke, and non-Mac errors to the panel's message", () => {
    expect(recover(failure("not_found"))).toBeNull();
    expect(recover(failure("certificate_rejected", "gitspoke"))).toBeNull();
    expect(recover(failure("signed_out", "gitspoke"))).toBeNull();
    expect(recover(new Error("host failed"))).toBeNull();
    expect(isMacSignInError(new Error("x"))).toBe(false);
  });

  it("offers the app's sign-in only where the shell can", () => {
    expect(recover(failure("signed_out"))?.signIn).toBe(false);
    expect(recover(failure("signed_out"), { signInAvailable: true })?.signIn).toBe(true);
  });

  it("offers another account only when one other than the failing one could read it", () => {
    const inUse = (logins: string[]) => ({
      ...choice(logins),
      effective: { login: logins[0], generation: 1 },
    });
    expect(recover(failure("signed_out"), { choice: choice([]) })?.chooseAccount).toBe(false);
    expect(recover(failure("account_invalid"), { choice: inUse(["a"]) })?.chooseAccount).toBe(
      false,
    );
    expect(recover(failure("account_invalid"), { choice: inUse(["a", "b"]) })?.chooseAccount).toBe(
      true,
    );
    // A chosen account that's gone: any signed-in account can read instead, or Automatic.
    const missing = recover(failure("account_missing"), {
      choice: choice(["a"], { mode: "manual", login: "gone" }),
    });
    expect(missing).toMatchObject({ chooseAccount: true, useAutomatic: true });
  });

  it("offers the session's machine only when it can answer", () => {
    expect(recover(failure("signed_out"))?.useSessionMachine).toBe(false);
    expect(recover(failure("signed_out"), { hostCanAnswer: true })?.useSessionMachine).toBe(true);
  });
});
