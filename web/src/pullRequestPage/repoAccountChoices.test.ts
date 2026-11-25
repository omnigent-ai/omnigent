import { describe, expect, it } from "vitest";

import { repoAccountChoices, repoAccountNote, repoAccountProblem } from "./repoAccountChoices";

const APP = { host: "github.com", owner: "acme", repo: "app" };
const ACCOUNTS = [
  { login: "alice", active: true, state: "success" as const },
  { login: "bob", active: false, state: "success" as const },
  { login: "expired", active: false, state: "invalid" as const },
];

describe("repoAccountChoices", () => {
  it("offers Automatic, naming who it reads as, then each signed-in account", () => {
    const choices = repoAccountChoices({
      repository: APP,
      intent: { mode: "automatic" },
      accounts: ACCOUNTS,
      automaticLogin: "alice",
    });
    expect(choices.map((choice) => [choice.label, choice.checked])).toEqual([
      ["Automatic (@alice)", true],
      ["@alice", false],
      ["@bob", false],
    ]);
    expect(choices.map((choice) => choice.paletteLabel)).toEqual([
      "Read acme/app Pull Requests as This Mac’s Account",
      "Read acme/app Pull Requests as @alice",
      "Read acme/app Pull Requests as @bob",
    ]);
    expect(choices[1]!.description).toBe("gh’s active account on this Mac.");
  });

  it("keeps a chosen login gh no longer lists, checked, so the user can move off it", () => {
    const choices = repoAccountChoices({
      repository: APP,
      intent: { mode: "account", login: "Carol" },
      accounts: ACCOUNTS,
      automaticLogin: null,
    });
    expect(choices.at(-1)).toMatchObject({
      label: "@Carol",
      checked: true,
      description: "Not signed in to gh on this Mac.",
      intent: { mode: "account", login: "Carol" },
    });
    expect(choices[0]!.label).toBe("Automatic");
  });

  it("checks a chosen login whatever its case", () => {
    const choices = repoAccountChoices({
      repository: APP,
      intent: { mode: "account", login: "BOB" },
      accounts: ACCOUNTS,
      automaticLogin: "alice",
    });
    expect(choices.filter((choice) => choice.checked).map((choice) => choice.label)).toEqual([
      "@bob",
    ]);
  });

  it("says the choice is kept for the repository on this Mac", () => {
    expect(repoAccountNote(APP)).toBe("Remembered for acme/app on this Mac.");
  });
});

describe("repoAccountProblem", () => {
  const BOB = { host: "github.com", login: "bob" };

  it("says nothing while the login reads, and tells a signed-out login how to sign in", () => {
    expect(repoAccountProblem({ kind: "named", reader: BOB }, false)).toBeNull();
    expect(repoAccountProblem({ kind: "none" }, false)).toBeNull();
    expect(repoAccountProblem({ kind: "named", reader: BOB }, true)).toBe(
      "@bob isn’t signed in to gh on this Mac. Run gh auth login, or choose another account.",
    );
  });

  it.each([
    ["checking", BOB, "Checking whether this Mac can read as @bob…"],
    ["pending", null, "Checking which account reads this repository on this Mac…"],
    [
      "turned_off",
      BOB,
      "Using other GitHub accounts on this Mac is turned off, so nothing is read as @bob.",
    ],
    [
      "update_needed",
      BOB,
      "Update the app to read this repository as @bob. Nothing is read as another account meanwhile.",
    ],
  ] as const)("explains %s", (reason, reader, message) => {
    expect(repoAccountProblem({ kind: "blocked", reason, reader }, false)).toBe(message);
  });
});
