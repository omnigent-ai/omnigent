import { describe, expect, it } from "vitest";

import { actionRouteHint } from "@/lib/actionRouteHint";

import {
  readSourceChangesNote,
  readSourceChoices,
  readSourceTriggerText,
  thisMacLabel,
} from "./pullRequestSource";

describe("readSourceChoices", () => {
  it("offers three single lines and checks the current one", () => {
    expect(
      readSourceChoices({ choice: "automatic", automaticPick: "mac", login: "octo" }).map(
        ({ label, checked }) => [label, checked],
      ),
    ).toEqual([
      ["Automatic (this Mac)", true],
      ["This Mac · @octo", false],
      ["Session’s machine", false],
    ]);
  });

  it("names Automatic's own pick, not the route an explicit choice gives", () => {
    const choices = readSourceChoices({ choice: "mac", automaticPick: "host", login: null });
    expect(choices[0]).toMatchObject({ label: "Automatic (session’s machine)", checked: false });
    expect(choices[1]).toMatchObject({ label: "This Mac", checked: true });
  });

  it("gives every choice its own ⌘K row", () => {
    expect(
      readSourceChoices({ choice: "session", automaticPick: "mac", login: "octo" }).map(
        (choice) => choice.paletteLabel,
      ),
    ).toEqual([
      "Read This Pull Request Automatically",
      "Read This Pull Request on This Mac",
      "Read This Pull Request on the Session’s Machine",
    ]);
  });
});

describe("readSourceChoices' descriptions", () => {
  it("says Automatic follows Settings and this Mac's reads, without guessing why", () => {
    const [mac] = readSourceChoices({
      choice: "automatic",
      automaticPick: "mac",
      login: "octo",
      host: "github.com",
    });
    const [host] = readSourceChoices({ choice: "automatic", automaticPick: "host", login: null });
    expect(mac!.description).toBe(
      "This Mac now. Follows Settings and whether this Mac can read github.com.",
    );
    expect(host!.description).toBe(
      "The session’s machine now. Follows Settings and whether this Mac can read GitHub.",
    );
  });

  it("names the Mac's account", () => {
    const session = { choice: "automatic" as const, automaticPick: "mac" as const, login: "octo" };
    expect(readSourceChoices(session)[1]!.description).toBe(
      "Uses gh on this Mac as @octo. Works while the session’s machine is off.",
    );
  });
});

describe("readSourceChangesNote", () => {
  it("says changes are made on this Mac only where they are, else on the session's machine", () => {
    expect(readSourceChangesNote("octo")).toBe("Changes are made on this Mac as @octo.");
    // Read on this Mac with its changes off, or an older shell: they still go to the host.
    expect(readSourceChangesNote(null)).toBe("Changes are made through the session’s machine.");
  });
});

describe("readSourceTriggerText", () => {
  it("names the place, with the Mac's account apart so it can truncate on its own", () => {
    const mac = { kind: "mac" as const, target: {} as never };
    expect(readSourceTriggerText(mac, "octo")).toEqual({
      place: "This Mac",
      account: "@octo",
      full: "This Mac · @octo",
    });
    expect(readSourceTriggerText(mac, null)).toEqual({
      place: "This Mac",
      account: null,
      full: "This Mac",
    });
    // The session's machine reads as its own account, and a blocked Mac reads as none.
    expect(readSourceTriggerText({ kind: "host" }, "octo")).toEqual({
      place: "Session’s machine",
      account: null,
      full: "Session’s machine",
    });
    expect(
      readSourceTriggerText({ kind: "blocked", reason: "mac_unsupported" }, "octo"),
    ).toMatchObject({ place: "This Mac", account: null });
    expect(thisMacLabel("a")).toBe("This Mac · @a");
  });
});

describe("actionRouteHint", () => {
  it("speaks up only where the change goes elsewhere than the read", () => {
    expect(actionRouteHint("Merge", { readOnMac: true, writesOnMac: false })).toBe(
      "Merge through the session’s machine",
    );
    expect(actionRouteHint("Merge", { readOnMac: true, writesOnMac: true })).toBeUndefined();
    expect(actionRouteHint("Merge", { readOnMac: false, writesOnMac: false })).toBeUndefined();
  });
});
