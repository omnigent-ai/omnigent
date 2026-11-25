import { afterEach, describe, expect, it, vi } from "vitest";

import { chooseMacAccount, macAccountCommands } from "./macAccountCommands";
import type { MacAccountChoice } from "./nativeBridge";

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

const CHOICE: MacAccountChoice = {
  intent: { mode: "manual", login: "Bob" },
  effective: { login: "Bob", generation: 3 },
  accounts: [
    { login: "alice", active: true, state: "success" },
    { login: "Bob", active: false, state: "success" },
    { login: "expired", active: false, state: "invalid" },
  ],
};

describe("macAccountCommands", () => {
  it("offers Automatic and each working account, checking the current choice", () => {
    const commands = macAccountCommands("github.com", CHOICE);
    expect(commands.map(({ label, checked }) => [label, checked])).toEqual([
      ["Automatic (gh’s active account)", false],
      ["@alice", false],
      ["@Bob", true],
    ]);
    expect(commands[2]).toMatchObject({
      id: "mac-account:github.com:bob",
      paletteLabel: "Read github.com pull requests as @Bob on this Mac",
      intent: { mode: "manual", login: "Bob" },
    });
  });

  it("checks Automatic when no account is chosen", () => {
    const commands = macAccountCommands("github.com", { ...CHOICE, intent: { mode: "automatic" } });
    expect(commands.filter((command) => command.checked).map((command) => command.id)).toEqual([
      "mac-account:github.com:automatic",
    ]);
  });
});

describe("chooseMacAccount", () => {
  it("saves through the shell and answers its choice, or null when refused or missing", async () => {
    expect(await chooseMacAccount("github.com", { mode: "automatic" })).toBeNull();
    const set = vi.fn(async () => ({ ok: true, data: CHOICE }));
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      codeHostAccounts: { get: vi.fn(), set },
    };
    expect(await chooseMacAccount("github.com", { mode: "manual", login: "Bob" })).toEqual(CHOICE);
    expect(set).toHaveBeenCalledWith("github.com", { mode: "manual", login: "Bob" });
    set.mockResolvedValueOnce({ ok: false, error: { code: "invalid_request" } } as never);
    expect(await chooseMacAccount("github.com", { mode: "automatic" })).toBeNull();
  });
});
