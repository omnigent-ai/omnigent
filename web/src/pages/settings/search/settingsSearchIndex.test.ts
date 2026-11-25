import { describe, expect, it } from "vitest";

import { SETTINGS_ENTRIES } from "./catalog";
import type { SettingsAvailability, SettingsEntry } from "./catalog/types";
import {
  searchSettings,
  type SettingsSearchMatcher,
  settingsSearchTargets,
  TEXT_TIER_BASE,
  textMatcher,
} from "./settingsSearchIndex";

const SECTIONS = [
  { id: "general", label: "General" },
  { id: "appearance", label: "Appearance" },
] as const;

const STEER: SettingsEntry = {
  id: "fixture.steer",
  section: "general",
  label: "Always steer",
  description: "Send follow-ups into the running turn.",
  keywords: ["queue", "interrupt"],
};
const THEME: SettingsEntry = {
  id: "fixture.terminal-theme",
  section: "appearance",
  label: "Terminal theme",
  description: "Colors for terminals.",
  choices: ["Match app", "Light", "Dark"],
};
const DELAY: SettingsEntry = {
  id: "fixture.delay",
  section: "appearance",
  label: "Number hint delay",
  description: "How long to hold the command key before the sidebar numbers its sessions.",
  parts: { field: "Number hint delay in milliseconds", reset: "Reset to default" },
};
const STEERING: SettingsEntry = {
  id: "fixture.steering-wheel",
  section: "general",
  label: "Steering wheel",
  description: "Mentions nothing.",
};
const HIDDEN_SECTION: SettingsEntry = {
  id: "fixture.cli",
  section: "cli",
  label: "Command line tool",
  description: "Not shown here.",
};

const SHOWN: SettingsAvailability = {
  globalShortcut: true,
  floatingComposerShortcut: true,
  widenWindowForRightSidebar: true,
  inAppBrowser: true,
  desktopAppHandoff: true,
  adminView: true,
  accountsEnabled: true,
  archiveWorktreeCleanup: true,
  mcpServices: true,
  integrationProviders: { github: true, databricks: true },
  projectKanban: true,
  sessionTitleInstructions: true,
  floatingPanes: true,
  proactiveAgent: true,
};
const HIDDEN: SettingsAvailability = {
  globalShortcut: false,
  floatingComposerShortcut: false,
  widenWindowForRightSidebar: false,
  inAppBrowser: false,
  desktopAppHandoff: false,
  adminView: false,
  accountsEnabled: false,
  archiveWorktreeCleanup: false,
  mcpServices: false,
  integrationProviders: { github: false, databricks: false },
  projectKanban: false,
  sessionTitleInstructions: false,
  floatingPanes: false,
  proactiveAgent: false,
};

const targets = settingsSearchTargets(
  [STEER, THEME, DELAY, STEERING, HIDDEN_SECTION],
  SECTIONS,
  SHOWN,
);
const keys = (query: string, matchers?: readonly SettingsSearchMatcher[]) =>
  searchSettings(query, targets, matchers).map((result) => result.target.key);

describe("settingsSearchTargets", () => {
  it("makes a target for each row and each part, in a section shown here", () => {
    expect(targets.map((target) => target.key)).toEqual([
      "fixture.steer",
      "fixture.terminal-theme",
      "fixture.delay",
      "fixture.delay:field",
      "fixture.delay:reset",
      "fixture.steering-wheel",
    ]);
    const reset = targets.find((target) => target.key === "fixture.delay:reset")!;
    expect(reset).toMatchObject({
      label: "Reset to default",
      description: "Number hint delay",
      part: "reset",
      place: "Number hint delay · Appearance",
    });
  });

  it("holds only the catalog's own strings", () => {
    const catalogText = new Set(
      [STEER, THEME, DELAY, STEERING].flatMap((entry) => [
        entry.label,
        entry.description,
        ...(entry.keywords ?? []),
        ...(entry.choices ?? []),
        ...Object.values(entry.parts ?? {}),
      ]),
    );
    for (const section of SECTIONS) catalogText.add(section.label);
    for (const target of targets) {
      for (const text of [target.label, target.description, ...target.keywords]) {
        expect(catalogText.has(text)).toBe(true);
      }
    }
  });
});

describe("runtime choices", () => {
  it("never reach the index, nor the fixed names beside them", () => {
    const apps: SettingsEntry = {
      id: "fixture.apps",
      section: "general",
      label: "Apps",
      description: "Which app opens.",
      parts: { app: "Default app" },
      overlays: { app: { runtimeChoices: { fixed: ["None"] } } },
    };
    const appTargets = settingsSearchTargets([apps], SECTIONS, SHOWN);
    const text = appTargets.flatMap((target) => [
      target.label,
      target.description,
      ...target.keywords,
    ]);
    expect(text).toContain("Default app");
    expect(text).not.toContain("None");
    for (const query of ["None", "Omnigent Canary"]) {
      expect(searchSettings(query, appTargets)).toEqual([]);
    }
  });
});

describe("a part's choices", () => {
  const pages: SettingsEntry = {
    id: "fixture.pages",
    section: "general",
    label: "Pages",
    description: "A page's views.",
    parts: { tab: "Pages view", other: "Other" },
    partChoices: { tab: ["Settings", "Servers"] },
    choiceTemplates: { tab: { Servers: { before: "Servers · ", count: true } } },
  };
  const pageTargets = settingsSearchTargets([pages], SECTIONS, SHOWN);

  it("find their own part by a static choice, not the row or another part", () => {
    expect(pageTargets.find((target) => target.part === "tab")?.keywords).toEqual([
      "Settings",
      "Servers",
    ]);
    expect(pageTargets.find((target) => target.part === "other")?.keywords).toEqual([]);
    expect(pageTargets.find((target) => target.part === undefined)?.keywords).not.toContain(
      "Servers",
    );
    expect(searchSettings("Servers", pageTargets)[0]?.target.part).toBe("tab");
  });

  it("never index a counted choice's text around its count", () => {
    const text = pageTargets.flatMap((target) => [
      target.label,
      target.description,
      ...target.keywords,
    ]);
    expect(text.some((value) => value.includes("·"))).toBe(false);
  });
});

describe("searchSettings", () => {
  it("finds nothing for an empty query", () => {
    expect(keys("   ")).toEqual([]);
  });

  it("ranks label matches before description, keyword and section matches", () => {
    // As in ⌘K: an exact label first, then word prefixes, a label's first word leading.
    expect(keys("always steer")).toEqual(["fixture.steer"]);
    expect(keys("steer")).toEqual(["fixture.steering-wheel", "fixture.steer"]);
    expect(keys("running turn")).toEqual(["fixture.steer"]);
    expect(keys("interrupt")).toEqual(["fixture.steer"]);
    expect(keys("appearance")).toEqual(["fixture.terminal-theme", "fixture.delay"]);
  });

  it("finds a part by its own name and a row by a choice's name", () => {
    expect(keys("reset to default")).toEqual(["fixture.delay:reset"]);
    expect(keys("milliseconds")).toEqual(["fixture.delay:field"]);
    expect(keys("match app")).toEqual(["fixture.terminal-theme"]);
  });

  it("matches words across the label and keywords, ignoring case", () => {
    expect(keys("STEER queue")).toEqual(["fixture.steer"]);
  });

  it("highlights the label and the place", () => {
    const [result] = searchSettings("reset", targets);
    expect(result.labelRanges).toEqual([{ start: 0, end: 5 }]);
    expect(searchSettings("appearance", targets)[0].placeRanges).toEqual([{ start: 0, end: 10 }]);
  });

  it("lets an earlier tier from another matcher lead, and lists each target once", () => {
    const exactly: SettingsSearchMatcher = (query) =>
      query === "dark" ? (target) => (target.key === "fixture.delay" ? { tier: 0 } : null) : null;
    const results = keys("dark", [exactly, textMatcher]);
    expect(results).toEqual(["fixture.delay", "fixture.terminal-theme"]);
    expect(new Set(results).size).toBe(results.length);
    const both: SettingsSearchMatcher = () => (target) =>
      target.key === "fixture.terminal-theme" ? { tier: TEXT_TIER_BASE + 10 } : null;
    expect(keys("dark", [both, textMatcher])).toEqual(["fixture.terminal-theme"]);
  });
});

describe("availability", () => {
  const LINKS: SettingsEntry = {
    id: "fixture.links",
    section: "general",
    label: "Open links in app",
    description: "Opens chat links in the app.",
    parts: { toggle: "Open links" },
    available: (availability) => availability.inAppBrowser,
  };

  it("leaves out a row, and its parts, that availability hides", () => {
    expect(settingsSearchTargets([LINKS], SECTIONS, SHOWN).map((target) => target.key)).toEqual([
      "fixture.links",
      "fixture.links:toggle",
    ]);
    expect(settingsSearchTargets([LINKS], SECTIONS, HIDDEN)).toEqual([]);
  });

  it("keeps a row without an availability rule, whatever is hidden", () => {
    expect(settingsSearchTargets([STEER], SECTIONS, HIDDEN).map((target) => target.key)).toEqual([
      "fixture.steer",
    ]);
  });

  // The rows the page renders only where the build or server offers them, each by its own
  // gate: turning off only that gate hides it, and turning on only that gate shows it.
  it.each([
    ["browser-agent-tools", "inAppBrowser"],
    ["open-in-desktop-app", "desktopAppHandoff"],
    ["project-kanban-board", "projectKanban"],
    ["session-title-instructions", "sessionTitleInstructions"],
    ["proactive-agent", "proactiveAgent"],
    ["widen-window-for-right-sidebar", "widenWindowForRightSidebar"],
    ["pane-corners", "floatingPanes"],
  ] as const)("offers the catalog's %s only where %s is on", (id, gate) => {
    const offered = (availability: SettingsAvailability) =>
      settingsSearchTargets(SETTINGS_ENTRIES, SECTIONS, availability).map((target) => target.key);
    expect(offered(SHOWN)).toContain(id);
    expect(offered({ ...SHOWN, [gate]: false })).not.toContain(id);
    expect(offered({ ...HIDDEN, [gate]: true })).toContain(id);
  });
});

describe("a dialog's parts", () => {
  const SHORTCUT: SettingsEntry = {
    id: "fixture.shortcut",
    section: "general",
    label: "Open from anywhere",
    description: "A keyboard shortcut that works in every app.",
    parts: { fix: "How to fix…", done: "Mark as fixed" },
    presence: { done: { overlay: "fix" } },
    available: (availability) => availability.globalShortcut,
  };

  it("lists each part once, placed under the control that opens it", () => {
    const parts = settingsSearchTargets([SHORTCUT], SECTIONS, SHOWN).filter(
      (target) => target.part !== undefined,
    );
    expect(parts.map((target) => target.key)).toEqual([
      "fixture.shortcut:fix",
      "fixture.shortcut:done",
    ]);
    expect(parts.find((target) => target.part === "done")?.place).toBe(
      "How to fix… · Open from anywhere · General",
    );
  });

  it("is offered only where its row is", () => {
    expect(settingsSearchTargets([SHORTCUT], SECTIONS, HIDDEN)).toEqual([]);
  });
});
