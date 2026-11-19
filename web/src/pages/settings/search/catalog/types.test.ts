import { describe, expect, it } from "vitest";

import {
  SETTINGS_MAIN_CONTROL,
  type SettingsEntry,
  isSettingsEntryFamily,
  settingsChoiceNamed,
  settingsEntry,
  settingsEntryFamily,
  settingsEntryMember,
  settingsEntryProblems,
  settingsRevealedByProblems,
  settingsRevealer,
} from "./types";

const base = {
  id: "fixture-row",
  section: "general",
  label: "Row",
  description: "A row.",
  parts: { fix: "Fix", done: "Done", note: "Note" },
} as const;

describe("conditional parts", () => {
  const entry = (extra: Partial<SettingsEntry>): SettingsEntry => ({ ...base, ...extra });

  it("are checked as the catalog declares them", () => {
    settingsEntry({
      ...base,
      presence: { done: { overlay: "fix" }, note: { states: ["editing"] } },
      // A form field shown while editing, reached through the main control.
      reachedBy: { note: SETTINGS_MAIN_CONTROL },
    });
    // @ts-expect-error A presence names only the entry's own parts.
    settingsEntry({ ...base, presence: { other: { states: ["open"] } } });
    // @ts-expect-error An overlay part is opened by another part, not itself.
    settingsEntry({ ...base, presence: { done: { overlay: "done" } } });
    // @ts-expect-error An opener always shows: it can't be conditional itself.
    settingsEntry({ ...base, presence: { done: { overlay: "note" }, note: { states: ["x"] } } });
    // @ts-expect-error A redirect points at a declared part or the main control.
    settingsEntry({ ...base, reachedBy: { note: "missing" } });
    settingsEntry({
      ...base,
      // @ts-expect-error A captured dialog's origin is a part on the page, not one inside an overlay.
      presence: { done: { overlay: "fix" }, fix: { overlay: "note" } },
      overlays: { fix: { captured: true } },
    });
  });

  it("report the same mistakes in entries built without settingsEntry", () => {
    expect(settingsEntryProblems(entry({ presence: { other: { states: ["open"] } } }))).toEqual([
      'fixture-row gives a presence to "other", which isn\'t a part',
    ]);
    expect(settingsEntryProblems(entry({ presence: { done: { overlay: "done" } } }))).toEqual([
      "fixture-row:done is its own opener",
    ]);
    expect(
      settingsEntryProblems(
        entry({ presence: { done: { overlay: "note" }, note: { states: ["x"] } } }),
      ),
    ).toEqual(["fixture-row:done's opener \"note\" doesn't always show"]);
    expect(
      settingsEntryProblems(
        entry({ presence: { done: { overlay: "fix" } }, reachedBy: { done: "note" } }),
      ),
    ).toEqual(["fixture-row:done is in an overlay, so its overlay's opener reveals it"]);
    expect(settingsEntryProblems(entry({ reachedBy: { note: "missing" } }))).toEqual([
      "fixture-row:note's opener \"missing\" isn't a part",
    ]);
    expect(settingsEntryProblems(entry({ presence: { note: { states: [] } } }))).toEqual([
      "fixture-row:note shows in no state",
    ]);
  });

  it("reject an id that is blank or contains a colon, which the hash reveal can't name", () => {
    expect(settingsEntryProblems(entry({ id: "a:b" }))).toEqual([
      '"a:b" isn\'t a usable id: it must be non-blank and contain no ":"',
    ]);
    expect(settingsEntryProblems(entry({ id: " " }))).toEqual([
      '" " isn\'t a usable id: it must be non-blank and contain no ":"',
    ]);
  });

  it("reject a captured dialog's origin as a redirect target", () => {
    const presence = { fix: { states: ["editing"] } } as const;
    const overlays = { fix: { captured: true } } as const;
    expect(
      settingsEntryProblems({
        ...base,
        presence,
        overlays,
        reachedBy: { note: "fix" },
      } as SettingsEntry),
    ).toEqual(["fixture-row:note's opener \"fix\" doesn't always show"]);
  });

  it("reject a captured origin that is itself inside an overlay", () => {
    expect(
      settingsEntryProblems({
        ...base,
        presence: { done: { overlay: "fix" }, fix: { overlay: "note" } },
        overlays: { fix: { captured: true } },
      } as SettingsEntry),
    ).toEqual(["fixture-row:done's opener \"fix\" doesn't always show"]);
  });

  it("let a captured dialog keep an opener that shows only in some states", () => {
    const presence = { done: { overlay: "fix" }, fix: { states: ["editing"] } } as const;
    expect(
      settingsEntryProblems({
        ...base,
        presence,
        overlays: { fix: { captured: true } },
      } as SettingsEntry),
    ).toEqual([]);
    expect(settingsEntryProblems({ ...base, presence } as SettingsEntry)).toEqual([
      "fixture-row:done's opener \"fix\" doesn't always show",
    ]);
  });
});

describe("the entry's name parts", () => {
  it("name a templated part by its static text around the value, and check it has some", () => {
    const entry = {
      ...base,
      nameTemplates: { done: { before: "Copy ", variants: { copied: { before: "Copied " } } } },
    } as SettingsEntry;
    expect(settingsEntryProblems(entry)).toEqual([]);
    expect(
      settingsEntryProblems({ ...base, nameTemplates: { done: { before: "", after: " " } } }),
    ).toEqual(["fixture-row:done's name template has no static text around its value"]);
    expect(settingsEntryProblems({ ...base, nameTemplates: { other: { before: "x" } } })).toEqual([
      'fixture-row names "other" by a template, but it isn\'t a part',
    ]);
  });

  it("reject blank alternative names, and alternatives for a missing part", () => {
    expect(
      settingsEntryProblems({
        ...base,
        nameAlternatives: { done: { busy: " " } },
      } as SettingsEntry),
    ).toEqual(['fixture-row:done\'s "busy" name is blank']);
    expect(
      settingsEntryProblems({
        ...base,
        nameAlternatives: { other: { busy: "x" } },
      } as SettingsEntry),
    ).toEqual(['fixture-row gives other names to "other", which isn\'t a part']);
  });

  it("reject a blank choice of the main control", () => {
    expect(settingsEntryProblems({ ...base, choices: ["System", " "] } as SettingsEntry)).toEqual([
      "fixture-row lists a blank choice",
    ]);
  });

  it("reject blank labels, choices and content areas", () => {
    const problems = settingsEntryProblems({
      ...base,
      partLabels: { done: "" },
      partChoices: { done: ["On", " "] },
      contentAreas: ["Notes", "Notes"],
    } as SettingsEntry);
    expect(problems).toEqual([
      "fixture-row:done lists a blank choice",
      "fixture-row's content areas must be named, each once",
      "fixture-row:done's label is blank",
    ]);
  });

  it("require a row action to be named, and a dialog to belong to one", () => {
    const problems = settingsEntryProblems({
      ...base,
      regions: {
        list: {
          rowActions: { remove: " " },
          rowOverlays: { rename: { rowActions: { save: "Save" } } },
        },
      },
    } as SettingsEntry);
    expect(problems).toEqual([
      'fixture-row:list\'s row action "remove" has no name',
      'fixture-row:list gives a dialog to "rename", which isn\'t a row action',
    ]);
  });
});

describe("a part's counted choices", () => {
  const counted = {
    ...base,
    partChoices: { done: ["Plugins", "Skills"] },
    choiceTemplates: { done: { Plugins: { before: "Plugins · ", count: true as const } } },
  } as SettingsEntry;

  it("name a static choice, or the same choice around a whole count", () => {
    expect(settingsChoiceNamed(counted, "done", "Plugins", "Plugins")).toBe(true);
    expect(settingsChoiceNamed(counted, "done", "Plugins", "Plugins · 2")).toBe(true);
    expect(settingsChoiceNamed(counted, "done", "Plugins", "Plugins · 02")).toBe(false);
    expect(settingsChoiceNamed(counted, "done", "Plugins", "Plugins · -1")).toBe(false);
    expect(settingsChoiceNamed(counted, "done", "Skills", "Skills · 2")).toBe(false);
  });

  it("are checked to belong to a static choice of a declared part", () => {
    expect(settingsEntryProblems(counted)).toEqual([]);
    expect(
      settingsEntryProblems({
        ...base,
        choiceTemplates: { done: { Plugins: { before: "Plugins · ", count: true as const } } },
      } as SettingsEntry),
    ).toEqual(['fixture-row:done counts "Plugins", which isn\'t one of its static choices']);
  });
});

describe("a structured revealedBy", () => {
  const tabs = {
    ...base,
    id: "fixture-tabs",
    parts: { tabs: "Tabs" },
    partChoices: { tabs: ["Plugins"] },
  } as const;
  const child = {
    ...base,
    id: "fixture-child",
    revealedBy: { entry: "fixture-tabs", part: "tabs", choice: "Plugins" },
  } as SettingsEntry;

  it("names a tab list part and one of its static choices", () => {
    expect(settingsRevealer(child)).toEqual({
      id: "fixture-tabs",
      part: "tabs",
      choice: "Plugins",
    });
    expect(settingsRevealedByProblems([tabs as SettingsEntry, child])).toEqual([]);
  });

  it("rejects a missing entry, another section, an undeclared part, and an unknown choice", () => {
    expect(
      settingsRevealedByProblems([{ ...child, revealedBy: "fixture-child" } as SettingsEntry]),
    ).toEqual(["fixture-child is revealed by itself"]);
    expect(settingsRevealedByProblems([child])).toEqual([
      "fixture-child is revealed by fixture-tabs, which isn't an entry",
    ]);
    expect(
      settingsRevealedByProblems([
        tabs as SettingsEntry,
        { ...child, section: "cli" } as SettingsEntry,
      ]),
    ).toEqual(["fixture-child is revealed by fixture-tabs, in another section"]);
    expect(settingsRevealedByProblems([{ ...tabs, parts: {} } as SettingsEntry, child])).toEqual([
      "fixture-child is revealed by fixture-tabs:tabs, which isn't a part",
    ]);
    expect(
      settingsRevealedByProblems([
        tabs as SettingsEntry,
        {
          ...child,
          revealedBy: { entry: "fixture-tabs", part: "tabs", choice: "Other" },
        } as SettingsEntry,
      ]),
    ).toEqual([
      'fixture-child is revealed by "Other", which isn\'t a static choice of fixture-tabs:tabs',
    ]);
  });

  it("reveals through a plain id, with no part", () => {
    expect(settingsRevealer({ ...child, revealedBy: "fixture-tabs" } as SettingsEntry)).toEqual({
      id: "fixture-tabs",
    });
    expect(settingsRevealer({ ...child, revealedBy: undefined } as SettingsEntry)).toBeUndefined();
  });
});

describe("an entry family", () => {
  const members = ["alpha", "beta", "gamma"] as const;
  const family = settingsEntryFamily({
    key: "fixture",
    members,
    build: (member) =>
      settingsEntry({
        id: `fixture-${member}`,
        section: "general",
        label: member,
        description: "d",
      }),
  });

  it("builds one entry per member, in order, and finds each by its member", () => {
    expect(family.entries.map((entry) => entry.id)).toEqual([
      "fixture-alpha",
      "fixture-beta",
      "fixture-gamma",
    ]);
    expect(family.entry("beta").id).toBe("fixture-beta");
    expect(settingsEntryMember(family.entry("beta"))).toBe("beta");
    expect(isSettingsEntryFamily(family)).toBe(true);
  });

  it("throws for a member it doesn't have, instead of falling back to another", () => {
    expect(() => family.entry("delta" as never)).toThrow(
      "delta isn't a member of the fixture Settings family",
    );
  });

  it("narrows to a subset that shares its members' entries", () => {
    const subset = family.subset("fixture-subset", ["gamma"]);
    expect(subset.entries).toHaveLength(1);
    expect(subset.entries[0]).toBe(family.entry("gamma"));
    expect(isSettingsEntryFamily(subset)).toBe(true);
    expect(() => family.subset("fixture-subset", ["delta" as never])).toThrow(
      "fixture-subset names delta, which fixture doesn't have",
    );
  });

  it("refuses a member listed twice, and a copy isn't a family", () => {
    expect(() =>
      settingsEntryFamily({
        key: "fixture",
        members: ["alpha", "alpha"],
        build: (member) =>
          settingsEntry({ id: member, section: "general", label: member, description: "d" }),
      }),
    ).toThrow("The fixture Settings family lists a member twice");
    expect(isSettingsEntryFamily({ ...family })).toBe(false);
    expect(isSettingsEntryFamily(null)).toBe(false);
  });
});
