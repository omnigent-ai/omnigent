// The rendered-page audit against small DOM fixtures: an owned row passes, and each way a
// control can escape its entry is reported.

import { afterEach, describe, expect, it } from "vitest";

import type { SettingsEntry } from "@/pages/settings/search/catalog/types";
import { provenOverlayOrigin } from "@/test/settingsPageSteps";

import {
  auditOpenedPortals,
  auditSettingsDom,
  auditSettingsDomFindings,
  domAuditProblems,
  renderedSettingsParts,
  settingsCoverageProblems,
  type SettingsDomScenarioAudit,
} from "./settingsDomAudit";

const SYNC: SettingsEntry = {
  id: "fixture.sync",
  section: "general",
  label: "Sync",
  description: "Keeps things in sync.",
  parts: { reset: "Reset sync" },
  choices: ["Fast", "Slow"],
  overlays: { reset: { dialogActions: ["Cancel"] } },
  regions: {
    items: {
      rowActions: { delete: "Delete" },
      rowFields: { token: { before: "Token for " } },
      rowOverlays: {
        delete: {
          rowActions: { cancel: "Cancel", confirm: "Delete" },
          rowFields: { reason: { before: "Why delete " } },
        },
      },
    },
    servers: { rowActions: { open: { dataName: true } } },
    serverDetails: { rowActions: { open: "Open details" } },
    tools: { rowActions: { expand: { dataName: true } } },
  },
  contentAreas: ["doc"],
};
const ACCENT: SettingsEntry = {
  id: "fixture.accent",
  section: "appearance",
  label: "Accent",
  description: "The accent color.",
};
const ENTRIES = new Map([SYNC, ACCENT].map((entry) => [entry.id, entry]));

const OWNED_ROW = `
  <div data-settings-item="fixture.sync">
    <span id="settings-fixture.sync-label">Sync</span>
    <button aria-label="About Sync">?</button>
    <button role="switch" aria-labelledby="settings-fixture.sync-label" data-settings-control="fixture.sync"></button>
    <button aria-label="Reset sync" data-settings-control="fixture.sync:reset">Reset sync</button>
  </div>`;

function audit(html: string, expected: SettingsEntry[] = [], portal?: string): string[] {
  document.body.innerHTML = `<main>${html}</main>${portal ? `<aside>${portal}</aside>` : ""}`;
  const roots = [
    document.querySelector("main")!,
    ...(portal ? [document.querySelector("aside")!] : []),
  ];
  return auditSettingsDom({ roots, entries: ENTRIES, expected });
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("auditSettingsDom", () => {
  it("accepts an owned row with its parts, choices, help button and dialog actions", () => {
    expect(
      audit(
        OWNED_ROW.replace(
          "</div>",
          `<div role="radiogroup" aria-labelledby="settings-fixture.sync-label" data-settings-control="fixture.sync">
             <button role="radio">Fast</button><button role="radio">Slow</button>
           </div></div>`,
        ),
        [SYNC],
        `<div data-settings-overlay-owner="fixture.sync:reset">
           <button data-settings-dialog-action="Cancel">Cancel</button>
           <button data-slot="dialog-close">Close</button>
         </div>`,
      ),
    ).toEqual([]);
  });

  it("takes the options in a part's own control from that part's choices, not its entry's", () => {
    const withSpeed: SettingsEntry = {
      ...SYNC,
      parts: { ...SYNC.parts, speed: "Speed" },
      partChoices: { speed: ["Quick", "Careful"] },
    };
    document.body.innerHTML = `<main>${OWNED_ROW.replace(
      "</div>",
      `<div role="radiogroup" aria-label="Speed" data-settings-control="fixture.sync:speed">
         <button role="radio">Quick</button><button role="radio">Fast</button></div></div>`,
    )}</main>`;
    expect(
      auditSettingsDom({
        roots: [document.querySelector("main")!],
        entries: new Map([[withSpeed.id, withSpeed]]),
        expected: [],
      }),
    ).toEqual([
      'button[role=radio] "Fast": is a choice of fixture.sync:speed that its part doesn\'t list',
    ]);
  });

  it("rejects a disclosure with no summary, and a summary no entry owns, but not a later one", () => {
    expect(
      audit(OWNED_ROW.replace("</div>", "<details><p>Additional information</p></details></div>")),
    ).toEqual(['details "": is a disclosure with no <summary> of its own']);
    // Only a disclosure's first summary is its control.
    expect(
      audit(
        OWNED_ROW.replace(
          "</div>",
          "<details><p>Intro</p><summary>Notes</summary><summary>More</summary></details></div>",
        ),
      ),
    ).toEqual(['summary "Notes": has no owner in the Settings catalog']);
  });

  it("rejects a separately labelled control inside an indexed row", () => {
    expect(
      audit(
        OWNED_ROW.replace("</div>", `<input type="checkbox" aria-label="Also sync drafts"></div>`),
      ),
    ).toEqual(['input "Also sync drafts": has no owner in the Settings catalog']);
  });

  it("rejects a control that reuses an entry under another name", () => {
    expect(
      audit(
        OWNED_ROW.replace(
          "</div>",
          `<button role="switch" aria-label="Sync drafts" data-settings-control="fixture.sync"></button></div>`,
        ),
      ),
    ).toEqual([
      'button[role=switch] "Sync drafts": is named "Sync drafts", but fixture.sync names it "Sync"',
    ]);
  });

  it("rejects a control placed in another entry's row", () => {
    expect(
      audit(
        `<div data-settings-item="fixture.accent"><span>Accent</span>
           <button aria-label="Reset sync" data-settings-control="fixture.sync:reset"></button></div>`,
      ),
    ).toEqual(['button "Reset sync": belongs to fixture.sync but sits in fixture.accent']);
  });

  it("rejects undeclared choices, row actions, row fields and dialog actions", () => {
    expect(
      audit(
        `${OWNED_ROW.replace(
          "</div>",
          `<div role="radiogroup" aria-labelledby="settings-fixture.sync-label" data-settings-control="fixture.sync">
             <button role="radio">Turbo</button></div></div>`,
        )}
         <div data-settings-region="fixture.sync:items">
           <button data-settings-row-action="archive">Archive</button>
           <select aria-label="Row mode" data-settings-row-field="mode"></select>
           <select aria-label="Row filter"></select>
         </div>`,
        [],
        `<div data-settings-overlay-owner="fixture.sync:reset">
           <button data-settings-dialog-action="Apply">Apply</button>
           <input aria-label="Name in dialog">
         </div>`,
      ),
    ).toEqual([
      'button[role=radio] "Turbo": is a choice of fixture.sync that its entry doesn\'t list',
      'button "Archive": is in a region of fixture.sync that sits in no row',
      'select "Row mode": is in a region of fixture.sync that sits in no row',
      'select "Row filter": has no owner in the Settings catalog',
      'button "Apply": dialog action "Apply" isn\'t declared',
      'input "Name in dialog": has no owner in the Settings catalog',
    ]);
  });

  it("links each overlay to its owner's row and keeps other entries' controls out of it", () => {
    const accentRow = `<div data-settings-item="fixture.accent"><span id="settings-fixture.accent-label">Accent</span>
      <button aria-labelledby="settings-fixture.accent-label" data-settings-control="fixture.accent"></button></div>`;
    expect(
      audit(
        `${OWNED_ROW}${accentRow}`,
        [],
        `<div data-settings-overlay-owner="fixture.accent">
           <button aria-label="Reset sync" data-settings-control="fixture.sync:reset"></button>
         </div>
         <div data-settings-overlay-owner="typo">
           <button aria-label="Reset sync" data-settings-control="fixture.sync:reset"></button>
         </div>
         <div data-settings-overlay-owner="fixture.sync:missing"></div>`,
      ),
    ).toEqual([
      'overlay "typo": names an entry that isn\'t in the catalog',
      'overlay "fixture.sync:missing": "missing" isn\'t a part of fixture.sync',
      'button "Reset sync": belongs to fixture.sync but sits in an overlay of fixture.accent',
      'button "Reset sync": belongs to fixture.sync but sits in an overlay of typo',
    ]);
    // An overlay only counts when its owner's row renders the control that opens it.
    expect(
      audit(
        `<div data-settings-item="fixture.accent"><span>Accent</span></div>`,
        [],
        `<div data-settings-overlay-owner="fixture.accent"></div>`,
      ),
    ).toEqual(['overlay "fixture.accent": fixture.accent\'s row has no control that opens it']);
  });

  it("finds a part only in its own row or an overlay that row opens", () => {
    const syncWithoutReset = OWNED_ROW.replace(
      `<button aria-label="Reset sync" data-settings-control="fixture.sync:reset">Reset sync</button>`,
      "",
    );
    const accentRow = `<div data-settings-item="fixture.accent"><span id="settings-fixture.accent-label">Accent</span>
      <button aria-labelledby="settings-fixture.accent-label" data-settings-control="fixture.accent"></button></div>`;
    expect(
      audit(
        `${syncWithoutReset}${accentRow}`,
        [SYNC],
        `<div data-settings-overlay-owner="fixture.accent">
           <button aria-label="Reset sync" data-settings-control="fixture.sync:reset"></button>
         </div>`,
      ),
    ).toEqual([
      'button "Reset sync": belongs to fixture.sync but sits in an overlay of fixture.accent',
      'fixture.sync: part "reset" doesn\'t render',
    ]);
  });

  it("checks names as a browser exposes them", () => {
    const row = (control: string) =>
      `<div data-settings-item="fixture.sync"><span id="settings-fixture.sync-label">Sync</span>${control}</div>`;
    expect(
      audit(
        row(
          `<button data-settings-control="fixture.sync"><span aria-hidden="true">Sync</span></button>`,
        ),
      ),
    ).toEqual(['button "": is named "", but fixture.sync names it "Sync"']);
    expect(
      audit(
        row(`<button aria-labelledby="missing" data-settings-control="fixture.sync"></button>`),
      ),
    ).toEqual(['button "": is named "", but fixture.sync names it "Sync"']);
    // With no element to point at, a browser falls back to the button's own text.
    expect(
      audit(
        row(`<button aria-labelledby="missing" data-settings-control="fixture.sync">Sync</button>`),
      ),
    ).toEqual([]);
  });

  it("expects one row per entry, showing its label, with every part", () => {
    expect(audit(`${OWNED_ROW}${OWNED_ROW}`, [SYNC])).toEqual([
      "fixture.sync: renders 2 rows, expected 1",
    ]);
    expect(
      audit(`<div data-settings-item="fixture.sync"><span>Other</span></div>`, [SYNC]),
    ).toEqual([
      'fixture.sync: its row doesn\'t show "Sync"',
      'fixture.sync: part "reset" doesn\'t render',
    ]);
    expect(audit("", [ACCENT])).toEqual(["fixture.accent: renders 0 rows, expected 1"]);
    expect(audit(OWNED_ROW, [{ ...ACCENT, revealedBy: "fixture.sync" }])).toEqual([]);
  });

  describe("a part shown only in some states, or inside an overlay", () => {
    /** Sync, whose Reset shows while it's dirty or in conflict. */
    const STATEFUL: SettingsEntry = {
      ...SYNC,
      presence: { reset: { states: ["dirty", "conflict"] } },
    };
    /** A row whose Done button is in the dialog its Fix button opens. */
    const FIXABLE: SettingsEntry = {
      id: "fixture.fixable",
      section: "general",
      label: "Fixable",
      description: "Can be fixed.",
      parts: { fix: "Fix it", other: "Other", done: "Done" },
      presence: { done: { overlay: "fix" } },
    };
    const ALL = new Map([STATEFUL, FIXABLE, ACCENT].map((entry) => [entry.id, entry]));
    const withoutReset = OWNED_ROW.replace(
      /<button aria-label="Reset sync"[^>]*>Reset sync<\/button>/,
      "",
    );
    const FIXABLE_ROW = `
      <div data-settings-item="fixture.fixable">
        <span>Fixable</span>
        <button aria-label="Fix it" data-settings-control="fixture.fixable:fix">Fix it</button>
        <button aria-label="Other" data-settings-control="fixture.fixable:other">Other</button>
      </div>`;
    const DONE =
      '<button aria-label="Done" data-settings-control="fixture.fixable:done">Done</button>';
    const auditIn = (
      html: string,
      expected: SettingsEntry[],
      driven: Record<string, string[]> = {},
      portal?: string,
    ) => {
      document.body.innerHTML = `<main>${html}</main>${portal ? `<aside>${portal}</aside>` : ""}`;
      return auditSettingsDom({
        roots: [
          document.querySelector("main")!,
          ...(portal ? [document.querySelector("aside")!] : []),
        ],
        entries: ALL,
        expected,
        drivenStates: new Map(Object.entries(driven).map(([id, states]) => [id, new Set(states)])),
      });
    };

    it("must show when any of its states is driven, and not otherwise", () => {
      expect(auditIn(withoutReset, [STATEFUL])).toEqual([]);
      expect(auditIn(OWNED_ROW, [STATEFUL], { "fixture.sync": ["conflict"] })).toEqual([]);
      expect(auditIn(withoutReset, [STATEFUL], { "fixture.sync": ["dirty"] })).toEqual([
        'fixture.sync: part "reset" doesn\'t render',
      ]);
      expect(auditIn(OWNED_ROW, [STATEFUL])).toEqual([
        'fixture.sync: part "reset" renders, though none of the states it shows in is driven',
      ]);
    });

    it("can't be driven by an unknown entry or state, nor excuse an always-shown part", () => {
      expect(auditIn(OWNED_ROW, [STATEFUL], { "fixture.sync": ["dirty", "stale"] })).toEqual([
        'drives fixture.sync\'s state "stale", which no part declares',
      ]);
      expect(auditIn(withoutReset, [STATEFUL], { "fixture.accent": ["dirty"] })).toEqual([
        "drives states of fixture.accent, which this render doesn't show",
      ]);
      // Without a presence, Reset always shows: no driven state can leave it out.
      expect(auditIn(withoutReset, [SYNC], { "fixture.sync": ["dirty"] })).toEqual([
        'fixture.sync: part "reset" doesn\'t render',
        'drives fixture.sync\'s state "dirty", which no part declares',
      ]);
    });

    it("shows only inside the overlay its own opener opens", () => {
      const open = (owner: string, inside: string) =>
        `<div data-settings-overlay-owner="${owner}">${inside}</div>`;
      expect(auditIn(FIXABLE_ROW, [FIXABLE])).toEqual([]);
      expect(auditIn(FIXABLE_ROW, [FIXABLE], {}, open("fixture.fixable:fix", DONE))).toEqual([]);
      // Open, but Done is missing, or sits in another overlay of the same row.
      expect(auditIn(FIXABLE_ROW, [FIXABLE], {}, open("fixture.fixable:fix", ""))).toEqual([
        'fixture.fixable: part "done" doesn\'t render',
      ]);
      expect(
        auditIn(
          FIXABLE_ROW,
          [FIXABLE],
          {},
          `${open("fixture.fixable:fix", "")}${open("fixture.fixable", DONE)}`,
        ),
      ).toContain('fixture.fixable: part "done" doesn\'t render');
      // Closed, yet Done renders in the row.
      expect(auditIn(FIXABLE_ROW.replace("</div>", `${DONE}</div>`), [FIXABLE])).toEqual([
        'fixture.fixable: part "done" renders, though none of the states it shows in is driven',
      ]);
    });

    it("must render in some scenario", () => {
      document.body.innerHTML = `<main>${withoutReset}</main>`;
      const parts = renderedSettingsParts([document.querySelector("main")!], ALL);
      expect(settingsCoverageProblems([STATEFUL], new Set(["fixture.sync"]), parts)).toEqual([
        'fixture.sync: no scenario renders its part "reset"',
      ]);
      document.body.innerHTML = `<main>${FIXABLE_ROW}</main><aside><div data-settings-overlay-owner="fixture.fixable">${DONE}</div></aside>`;
      const misplaced = renderedSettingsParts(
        [document.querySelector("main")!, document.querySelector("aside")!],
        ALL,
      );
      // Done in the row's other overlay isn't Done in Fix's overlay.
      expect(misplaced.has("fixture.fixable:done")).toBe(false);
      document.body.innerHTML = `<main>${OWNED_ROW}</main>`;
      const all = renderedSettingsParts([document.querySelector("main")!], ALL);
      expect(settingsCoverageProblems([STATEFUL], new Set(["fixture.sync"]), all)).toEqual([]);
      expect(settingsCoverageProblems([STATEFUL, ACCENT], new Set(["fixture.sync"]), all)).toEqual([
        "fixture.accent: no scenario renders its row",
      ]);
    });
  });
});

describe("domAuditProblems", () => {
  // A section with one owned row, and two controls that answer to no entry.
  const UNOWNED = `${OWNED_ROW}
    <button aria-label="Export theme">Export</button>
    <input type="text" aria-label="Theme name">`;
  const scenario = (
    html: string,
    expected: SettingsEntry[] = [SYNC],
    name = "default",
  ): SettingsDomScenarioAudit => {
    document.body.innerHTML = `<main>${html}</main>`;
    return {
      section: "appearance",
      scenario: name,
      findings: auditSettingsDomFindings({
        roots: [document.querySelector("main")!],
        entries: ENTRIES,
        expected,
      }),
    };
  };

  it("passes a section whose every control has its owner", () => {
    expect(domAuditProblems([scenario(OWNED_ROW)])).toEqual([]);
  });

  it("fails every unowned control, under its section and scenario", () => {
    expect(domAuditProblems([scenario(UNOWNED), scenario(UNOWNED, [SYNC], "admin")])).toEqual([
      'appearance › default: button "Export theme": has no owner in the Settings catalog',
      'appearance › default: input "Theme name": has no owner in the Settings catalog',
      'appearance › admin: button "Export theme": has no owner in the Settings catalog',
      'appearance › admin: input "Theme name": has no owner in the Settings catalog',
    ]);
  });

  it("fails a wrong name, a control in another row, and an entry that doesn't render", () => {
    const misnamed = OWNED_ROW.replace('aria-label="Reset sync"', 'aria-label="Reset all"');
    const misplaced = `${OWNED_ROW}<div data-settings-item="fixture.accent"><button aria-label="Sync" data-settings-control="fixture.sync"></button></div>`;
    for (const [html, expected] of [
      [misnamed, [SYNC]],
      [misplaced, [SYNC]],
      [OWNED_ROW, [SYNC, ACCENT]],
    ] as const) {
      const found = scenario(html, [...expected]).findings;
      expect(found).toEqual([expect.objectContaining({ kind: "problem" })]);
      expect(domAuditProblems([scenario(html, [...expected])])).toEqual([
        `appearance › default: ${found[0].message}`,
      ]);
    }
  });
});

describe("a part named around a runtime value", () => {
  const COPY: SettingsEntry = {
    id: "fixture.copy",
    section: "general",
    label: "Link command",
    description: "A command to run.",
    parts: { copy: "Copy the link command" },
    nameTemplates: { copy: { before: "Copy ", variants: { copied: { before: "Copied " } } } },
  };
  const entries = new Map([[COPY.id, COPY]]);
  const auditNamed = (name: string) => {
    document.body.innerHTML = `<main><div data-settings-item="fixture.copy"><span>Link command</span>
      <button aria-label="${name}" data-settings-control="fixture.copy:copy"></button></div></main>`;
    return auditSettingsDom({
      roots: [document.querySelector("main")!],
      entries,
      expected: [COPY],
    });
  };

  it("is named by a declared prefix around a value, or by the static name", () => {
    for (const name of ["Copy ln -s a b", "Copied ln -s a b", "Copy the link command"]) {
      expect(auditNamed(name), name).toEqual([]);
    }
  });

  it("fails another prefix and a blank value", () => {
    // A browser trims the name, so a blank value leaves just the prefix.
    for (const name of ["Paste ln -s a b", "Copy ", "Copy    ", "Copied", "copy ln -s a b"]) {
      const shown = name.trim();
      expect(auditNamed(name), name).toEqual([
        `button "${shown}": is named "${shown}", but fixture.copy names it "Copy the link command"`,
      ]);
    }
  });

  it("can't tell which value is right, so a component test names the value it shows", () => {
    // Same prefix, wrong command: the audit accepts it; only the full name proves the value.
    expect(auditNamed("Copy rm -rf /tmp/other")).toEqual([]);
  });
});

describe("an overlay of runtime choices", () => {
  /** A row whose Default app part opens a list of the apps the shell finds, plus None. */
  const APPS: SettingsEntry = {
    id: "fixture.apps",
    section: "general",
    label: "Apps",
    description: "Which app opens.",
    choices: ["Automatic", "Off"],
    parts: { app: "Default app" },
    overlays: { "": {}, app: { runtimeChoices: { fixed: ["None"] } } },
  };
  const entries = new Map([SYNC, ACCENT, APPS].map((entry) => [entry.id, entry]));
  const APPS_ROW = `
    <div data-settings-item="fixture.apps">
      <span id="settings-fixture.apps-label">Apps</span>
      <button role="combobox" aria-labelledby="settings-fixture.apps-label" data-settings-control="fixture.apps"></button>
      <button aria-label="About Default app">?</button>
      <button role="combobox" aria-label="Default app" data-settings-control="fixture.apps:app"></button>
    </div>`;
  const option = (name: string, marker?: string) =>
    `<div role="option"${marker === undefined ? "" : ` data-settings-runtime-choice="${marker}"`}>${name}</div>`;
  const overlay = (owner: string, body: string) =>
    `<div data-settings-overlay-owner="${owner}">${body}</div>`;
  function auditApps(html: string, portal: string): string[] {
    document.body.innerHTML = `<main>${html}</main><aside>${portal}</aside>`;
    const roots = [document.querySelector("main")!, document.querySelector("aside")!];
    return auditSettingsDom({ roots, entries, expected: [] });
  }

  it("names marked options from runtime data, and the rest by its fixed list", () => {
    const list = `${option("Omnigent Canary (this app)", "fixture.apps:app")}${option("Omnigent (not running)", "fixture.apps:app")}${option("None")}`;
    expect(auditApps(APPS_ROW, overlay("fixture.apps:app", list))).toEqual([]);
  });

  it("fails an unmarked name it doesn't list, and a marked option with no name", () => {
    expect(
      auditApps(
        APPS_ROW,
        overlay("fixture.apps:app", `${option("Omnigent")}${option(" ", "fixture.apps:app")}`),
      ),
    ).toEqual([
      'div[role=option] "Omnigent": is an option of fixture.apps:app that isn\'t marked as a runtime choice or listed as fixed',
      'div[role=option] "": is a runtime choice with no name',
    ]);
  });

  it("keeps the options of a part's own select to that part's choices", () => {
    const KEEP: SettingsEntry = {
      id: "fixture.keep",
      section: "general",
      label: "Keep",
      description: "How long to keep things.",
      parts: { days: "Keep for" },
      partChoices: { days: ["7 days", "Never"] },
      overlays: { days: {} },
    };
    const keepRow = `<div data-settings-item="fixture.keep"><span>Keep</span><button role="combobox" aria-label="Keep for" data-settings-control="fixture.keep:days"></button></div>`;
    document.body.innerHTML = `<main>${keepRow}</main><aside>${overlay("fixture.keep:days", `${option("7 days")}${option("Monthly")}`)}</aside>`;
    expect(
      auditSettingsDom({
        roots: [document.querySelector("main")!, document.querySelector("aside")!],
        entries: new Map([[KEEP.id, KEEP]]),
        expected: [],
      }),
    ).toEqual([
      'div[role=option] "Monthly": is a choice of fixture.keep:days that its part doesn\'t list',
    ]);
  });

  it("keeps a finite select's choices to its entry, beside the runtime list", () => {
    expect(auditApps(APPS_ROW, overlay("fixture.apps", option("Omnigent")))).toEqual([
      'div[role=option] "Omnigent": is a choice of fixture.apps that its entry doesn\'t list',
    ]);
  });

  it("fails a marker anywhere but its own part's overlay", () => {
    const marked = option("Omnigent", "fixture.apps:app");
    expect(
      auditApps(
        `${APPS_ROW}${OWNED_ROW}${marked}`,
        [
          // The row's other select, another row's overlay, and inside another overlay.
          overlay("fixture.apps", marked),
          overlay("fixture.sync:reset", marked),
          overlay("fixture.apps:app", overlay("fixture.sync:reset", marked)),
          // The marker names the overlay, but that overlay doesn't declare runtime choices.
          overlay("fixture.sync:reset", option("Fast", "fixture.sync:reset")),
          overlay(
            "fixture.apps:app",
            `<button data-settings-runtime-choice="fixture.apps:app">Omnigent</button>`,
          ),
        ].join(""),
      ),
    ).toEqual([
      'div[role=option] "Omnigent": is a runtime choice of fixture.apps:app outside any overlay',
      'div[role=option] "Omnigent": is a runtime choice of fixture.apps:app, but sits in an overlay of fixture.apps',
      'div[role=option] "Omnigent": is a runtime choice of fixture.apps:app, but sits in an overlay of fixture.sync:reset',
      'div[role=option] "Omnigent": is a runtime choice of fixture.apps:app, but sits in an overlay of fixture.sync:reset',
      "div[role=option] \"Fast\": is a runtime choice, but fixture.sync:reset's overlay doesn't declare runtime choices",
      'button "Omnigent": carries a runtime-choice marker, but isn\'t an option',
    ]);
  });

  it("holds every option and marker to the contract, however else it's owned", () => {
    // An unmarked option owned as the part itself, and a native option marked for another row.
    const ownedOption = `<div role="option" aria-label="Default app" data-settings-control="fixture.apps:app">Default app</div>`;
    const nativeSelect = `<select aria-labelledby="settings-fixture.apps-label" data-settings-control="fixture.apps"><option data-settings-runtime-choice="fixture.sync:wrong">Omnigent</option></select>`;
    expect(
      auditApps(
        APPS_ROW.replace("</div>", `${nativeSelect}</div>`),
        overlay("fixture.apps:app", ownedOption),
      ),
    ).toEqual([
      'option "Omnigent": is a runtime choice of fixture.sync:wrong outside any overlay',
      'div[role=option] "Default app": is an option of fixture.apps:app that isn\'t marked as a runtime choice or listed as fixed',
    ]);
  });

  describe("a part's help button", () => {
    const noOwner = 'button "About Default app": has no owner in the Settings catalog';
    const withoutPart = APPS_ROW.replace(
      '<button role="combobox" aria-label="Default app" data-settings-control="fixture.apps:app"></button>',
      "",
    );

    it("is owned beside that part's control in the same row", () => {
      expect(auditApps(APPS_ROW, "")).toEqual([]);
    });

    it("isn't owned when the part doesn't render there", () => {
      // The part absent, rendered only in a portal, or in a row nested inside this one.
      expect(auditApps(withoutPart, "")).toEqual([noOwner]);
      expect(
        auditApps(
          withoutPart,
          overlay(
            "fixture.apps",
            '<button role="combobox" aria-label="Default app" data-settings-control="fixture.apps:app"></button>',
          ),
        ),
      ).toContain(noOwner);
      expect(
        auditApps(
          withoutPart.replace(
            "</div>",
            '<div data-settings-item="fixture.accent"><button role="combobox" aria-label="Default app" data-settings-control="fixture.apps:app"></button></div></div>',
          ),
          "",
        ),
      ).toContain(noOwner);
    });

    it("isn't owned by a part that only labels something that can't be focused", () => {
      const labelOnly = withoutPart.replace(
        "</div>",
        '<span aria-label="Default app" data-settings-control="fixture.apps:app">Default app</span></div>',
      );
      expect(auditApps(labelOnly, "")).toEqual([noOwner]);
    });

    it("isn't owned for another row's part", () => {
      expect(
        auditApps(`${APPS_ROW.replace("About Default app", "About Reset sync")}${OWNED_ROW}`, ""),
      ).toEqual(['button "About Reset sync": has no owner in the Settings catalog']);
    });
  });
});

describe("a row's plumbing and sub-labels", () => {
  /** A row like Color theme: a main select under a "Theme palette" sub-label, and a slider. */
  const THEME: SettingsEntry = {
    id: "fixture.theme",
    section: "appearance",
    label: "Color theme",
    description: "Colors.",
    parts: { contrast: "Theme contrast", chooseFile: "Choose file" },
    partLabels: { "": "Theme palette", contrast: "Contrast" },
  };
  const entries = new Map([SYNC, THEME].map((entry) => [entry.id, entry]));
  const MAIN = `<button role="combobox" aria-labelledby="settings-fixture.theme-label" data-settings-control="fixture.theme"></button>`;
  const CONTRAST = `<input type="range" aria-label="Theme contrast" data-settings-control="fixture.theme:contrast">`;
  const row = (body: string) =>
    `<div data-settings-item="fixture.theme"><span id="settings-fixture.theme-label">Color theme</span>${body}</div>`;
  const run = (html: string) => {
    document.body.innerHTML = `<main>${html}</main>`;
    return auditSettingsDom({ roots: [document.querySelector("main")!], entries });
  };
  const help = (label: string) => `<button aria-label="About ${label}">?</button>`;

  describe("a sub-label's help button", () => {
    it("is owned beside the control it labels, the main control included", () => {
      expect(run(row(`${help("Theme palette")}${MAIN}${help("Contrast")}${CONTRAST}`))).toEqual([]);
    });

    it("isn't owned when that control is absent, not interactive, or in another row", () => {
      const unowned = (label: string) =>
        `button "About ${label}": has no owner in the Settings catalog`;
      expect(run(row(`${help("Theme palette")}${help("Contrast")}`))).toEqual([
        unowned("Theme palette"),
        unowned("Contrast"),
      ]);
      expect(
        run(
          row(
            `${help("Contrast")}<span aria-label="Theme contrast" data-settings-control="fixture.theme:contrast"></span>`,
          ),
        ),
      ).toEqual([unowned("Contrast")]);
      expect(
        run(`${row(help("Contrast"))}<div data-settings-item="fixture.sync">${CONTRAST}</div>`),
      ).toContain(unowned("Contrast"));
    });
  });

  describe("a hidden file input", () => {
    const input = (attributes: string) =>
      `<input type="file" ${attributes} data-settings-file-input="fixture.theme:chooseFile">`;

    it("is plumbing for the part it's bound to, while hidden and out of the tab order", () => {
      expect(run(row(input('hidden tabindex="-1"')))).toEqual([]);
    });

    it("fails exposed, focusable, of another kind, in another row, or bound to no part", () => {
      expect(run(row(input('tabindex="-1"')))).toEqual([
        'input "": is a file input bound to a part, but isn\'t hidden',
      ]);
      expect(run(row(input("hidden")))).toEqual([
        'input "": is a hidden file input that can take focus',
      ]);
      expect(
        run(row(`<div hidden data-settings-file-input="fixture.theme:chooseFile"></div>`)),
      ).toEqual(['div "": carries a file-input marker, but isn\'t a file input']);
      expect(
        run(
          `${row("")}<div data-settings-item="fixture.sync">${input('hidden tabindex="-1"')}</div>`,
        ),
      ).toEqual(['input "": is bound to fixture.theme:chooseFile but sits in fixture.sync']);
      expect(
        run(
          row(
            `<input type="file" hidden tabindex="-1" data-settings-file-input="fixture.theme:upload">`,
          ),
        ),
      ).toEqual(['input "": is bound to "upload", which isn\'t a part of fixture.theme']);
    });

    it("leaves an unmarked hidden input as it was: unowned", () => {
      expect(run(row('<input type="file" hidden tabindex="-1">'))).toEqual([
        'input "": has no owner in the Settings catalog',
      ]);
    });
  });
});

describe("auditOpenedPortals", () => {
  /** A row whose Reset opens a dialog holding its confirm button, as Settings data does. */
  const DATA: SettingsEntry = {
    id: "fixture.data",
    section: "appearance",
    label: "Settings data",
    description: "Data.",
    parts: { reset: "Reset to defaults", confirm: "Reset", retry: "Try again" },
    presence: { confirm: { overlay: "reset" }, retry: { states: ["failed"] } },
    overlays: { reset: { dialogActions: ["Cancel"] } },
  };
  const entries = new Map([DATA].map((entry) => [entry.id, entry]));
  // The page, with an unowned control the scenario's own audit already counted.
  const PAGE = `<div data-settings-item="fixture.data"><span>Settings data</span>
      <button aria-label="Reset to defaults" data-settings-control="fixture.data:reset"></button>
    </div>
    <input aria-label="An unowned field">`;
  const CONFIRM = `<button aria-label="Reset" data-settings-control="fixture.data:confirm"></button>`;
  const CANCEL = `<button data-settings-dialog-action="Cancel">Cancel</button>`;
  const dialog = (body: string, owner = "fixture.data:reset") =>
    `<div${owner ? ` data-settings-overlay-owner="${owner}"` : ""}><h2>Reset appearance?</h2>${body}</div>`;
  function open(portal: string) {
    document.body.innerHTML = `<main>${PAGE}</main>`;
    const opened = document.createElement("aside");
    opened.innerHTML = portal;
    document.body.append(opened);
    const input = { roots: [document.body], entries, expected: [DATA], opened: [opened] };
    return {
      messages: auditOpenedPortals(input).map((finding) => finding.message),
      parts: renderedSettingsParts([document.body], entries),
    };
  }

  it("passes an open overlay holding its parts, and doesn't recount the page", () => {
    const { messages, parts } = open(dialog(`${CONFIRM}${CANCEL}`));
    expect(messages).toEqual([]);
    // What an action shows counts toward coverage.
    expect(parts.has("fixture.data:confirm")).toBe(true);
  });

  it("keeps a problem outside what opened, and drops only the page's unowned controls", () => {
    // The dialog is valid, but its opener on the page was renamed while it was open.
    document.body.innerHTML = `<main>${PAGE.replace('aria-label="Reset to defaults"', 'aria-label="Reset everything"')}</main>`;
    const opened = document.createElement("aside");
    opened.innerHTML = dialog(`${CONFIRM}${CANCEL}`);
    document.body.append(opened);
    const findings = auditOpenedPortals({
      roots: [document.body],
      entries,
      expected: [DATA],
      opened: [opened],
    });
    expect(findings.map((finding) => finding.message)).toEqual([
      'button "Reset everything": is named "Reset everything", but fixture.data names it "Reset to defaults"',
    ]);
  });

  it("fails a required part gone while the dialog still shows its title", () => {
    expect(open(dialog(CANCEL)).messages).toEqual(['fixture.data: part "confirm" doesn\'t render']);
  });

  it("fails a part under another opener's overlay, or in a portal without its owner", () => {
    expect(open(dialog(`${CONFIRM}${CANCEL}`, "fixture.data")).messages).toContain(
      'fixture.data: part "confirm" renders, though none of the states it shows in is driven',
    );
    expect(open(dialog(`${CONFIRM}${CANCEL}`, "")).messages).toEqual([
      'button "Reset": belongs to fixture.data but sits in no row',
      'button "Cancel": dialog action "Cancel" isn\'t declared',
      'fixture.data: part "confirm" renders, though none of the states it shows in is driven',
    ]);
  });

  it("counts an unowned control nested in what opened, and a part in a state not driven", () => {
    const nested = `<div><div><input aria-label="Extra field"></div></div>`;
    const retry = `<button aria-label="Try again" data-settings-control="fixture.data:retry"></button>`;
    expect(open(dialog(`${CONFIRM}${CANCEL}${nested}${retry}`)).messages).toEqual([
      'input "Extra field": has no owner in the Settings catalog',
      'fixture.data: part "retry" renders, though none of the states it shows in is driven',
    ]);
  });

  it("leaves an overlay part out of coverage when no action opens it", () => {
    document.body.innerHTML = `<main>${PAGE}</main>`;
    const parts = renderedSettingsParts([document.body], entries);
    expect(settingsCoverageProblems([DATA], new Set([DATA.id]), parts)).toEqual([
      'fixture.data: no scenario renders its part "confirm"',
      'fixture.data: no scenario renders its part "retry"',
    ]);
  });
});

describe("a select's hidden form value", () => {
  it("passes beside its own trigger, and fails anywhere else", () => {
    const hidden =
      '<select aria-hidden="true" tabindex="-1"><option value="fast">Fast</option></select>';
    const trigger =
      '<button role="combobox" aria-labelledby="settings-fixture.sync-label" data-settings-control="fixture.sync"></button>';
    const row = (body: string) => OWNED_ROW.replace(/<\/div>$/, `${body}</div>`);
    expect(audit(row(`${trigger}${hidden}`))).toEqual([]);
    expect(audit(row(hidden))).toEqual(['select "": has no owner in the Settings catalog']);
  });
});

describe("a data region's rows", () => {
  const region = (rows: string, value = "fixture.sync:items") =>
    `<div data-settings-region="${value}">${rows}</div>`;
  const row = (body: string) => OWNED_ROW.replace(/<\/div>$/, `${body}</div>`);
  const DELETE = '<button data-settings-row-action="delete">Delete</button>';
  const TOKEN = (name: string) => `<input aria-label="${name}" data-settings-row-field="token">`;

  describe("named by their row's data", () => {
    const servers = (rows: string) => row(region(rows, "fixture.sync:servers"));
    const serverRow = (body: string) => `<div data-settings-region-row>${body}</div>`;
    const open = (name: string, tag = "button") =>
      `<${tag} data-settings-row-action="open"${tag === "a" ? ' href="#x"' : ""}>${name}</${tag}>`;
    const reasons = (html: string) => audit(html).map((problem) => problem.replace(/^[^:]*: /, ""));

    it("take any name from their row, once in each region row", () => {
      expect(
        audit(
          servers(
            serverRow(open("docs · mcp.example.com · Connected")) + serverRow(open("notes", "a")),
          ),
        ),
      ).toEqual([]);
    });

    it("fail without a name, as something else, outside a region row, or twice in one", () => {
      expect(reasons(servers(serverRow(open(""))))).toEqual([
        'row action "open" has no name from its row\'s data',
      ]);
      expect(
        reasons(
          servers(
            serverRow('<div role="switch" tabindex="0" data-settings-row-action="open">On</div>'),
          ),
        ),
      ).toEqual(['row action "open" isn\'t a button, link or disclosure']);
      expect(reasons(servers(open("docs")))).toEqual([
        'row action "open" isn\'t in a row of fixture.sync:servers',
      ]);
      expect(reasons(servers(serverRow(open("docs") + open("notes"))))).toEqual([
        'row action "open" shows 2 times in one row of fixture.sync:servers',
        'row action "open" shows 2 times in one row of fixture.sync:servers',
      ]);
      // A row of another region doesn't count as this one's.
      const borrowed = servers(region(serverRow(open("docs")), "fixture.sync:items"));
      expect(reasons(borrowed)).toEqual([
        'row action "open" isn\'t declared for fixture.sync:items',
      ]);
    });

    it("counts an action once per row of its own region, not a nested region's", () => {
      const nested = region(
        '<button data-settings-row-action="open">Open details</button>',
        "fixture.sync:serverDetails",
      );
      expect(audit(servers(serverRow(open("docs") + nested)))).toEqual([]);
    });

    describe("a disclosure in a tool row", () => {
      const tools = (rows: string) => region(rows, "fixture.sync:tools");
      const toolRow = (body: string) => `<li data-settings-region-row>${body}</li>`;
      const expand = (name: string, before = "") =>
        `<details>${before}<summary data-settings-row-action="expand">${name}</summary><p>What it does.</p></details>`;
      const server = (name: string, rows: string) =>
        serverRow(open(name) + tools(`<ul>${rows}</ul>`));

      it("opens by its first summary, in each tool row of each server, by the tool's name", () => {
        // A tool name may repeat across servers; a tool without a description is plain text.
        expect(
          audit(
            servers(
              server("docs", toolRow(expand("lookup_docs")) + toolRow("<span>list_pages</span>")) +
                server("notes", toolRow(expand("lookup_docs"))),
            ),
          ),
        ).toEqual([]);
      });

      it("takes the first summary after another element, but no later one", () => {
        expect(
          audit(servers(server("docs", toolRow(expand("lookup_docs", "<p>Intro</p>"))))),
        ).toEqual([]);
        // A later summary isn't a control, so a marker on it is refused; marked on both, the
        // row also has the action twice, and marked only there, the real control is unowned.
        const both = `<details><summary data-settings-row-action="expand">lookup_docs</summary><summary data-settings-row-action="expand">Again</summary></details>`;
        expect(reasons(servers(server("docs", toolRow(both))))).toEqual([
          "carries a row marker, but isn't a control",
          'row action "expand" shows 2 times in one row of fixture.sync:tools',
        ]);
        const later = `<details><summary>lookup_docs</summary><summary data-settings-row-action="expand">Again</summary></details>`;
        expect(reasons(servers(server("docs", toolRow(later))))).toEqual([
          "carries a row marker, but isn't a control",
          "has no owner in the Settings catalog",
        ]);
        const span = '<span data-settings-row-action="expand">lookup_docs</span>';
        expect(reasons(servers(server("docs", toolRow(span))))).toEqual([
          "carries a row marker, but isn't a control",
        ]);
      });

      it("fails without a name, twice in one row, or outside its own region's row", () => {
        expect(reasons(servers(server("docs", toolRow(expand("")))))).toEqual([
          'row action "expand" has no name from its row\'s data',
        ]);
        expect(
          reasons(servers(server("docs", toolRow(expand("lookup_docs") + expand("list_pages"))))),
        ).toEqual([
          'row action "expand" shows 2 times in one row of fixture.sync:tools',
          'row action "expand" shows 2 times in one row of fixture.sync:tools',
        ]);
        // In its server's row but no tool row of its own, it can't borrow the server's.
        expect(reasons(servers(serverRow(open("docs") + tools(expand("lookup_docs")))))).toEqual([
          'row action "expand" isn\'t in a row of fixture.sync:tools',
        ]);
      });
    });

    it("keeps region rows inside their region, and one row out of another", () => {
      expect(reasons(row(serverRow("")))).toEqual(["is a region row outside any data region"]);
      expect(reasons(servers(serverRow(serverRow(open("docs")))))).toEqual([
        "is a region row inside another row of its region",
      ]);
    });
  });

  it("takes declared actions by their exact names, and fields named around a value", () => {
    expect(audit(row(region(`${DELETE}${TOKEN("Token for Tracker")}`)))).toEqual([]);
  });

  it("fails a declared action with another name, and a field without its affix or value", () => {
    expect(
      audit(
        row(
          region(
            `<button data-settings-row-action="delete">Delete now</button>${TOKEN("Key for Tracker")}${TOKEN("Token for  ")}`,
          ),
        ),
      ),
    ).toEqual([
      'button "Delete now": row action "delete" is named "Delete now", but fixture.sync:items names it "Delete"',
      'input "Key for Tracker": row field "token" is named "Key for Tracker", not fixture.sync:items\'s "Token for …"',
      'input "Token for": row field "token" is named "Token for", not fixture.sync:items\'s "Token for …"',
    ]);
  });

  it("fails a field marker on a control that takes no value", () => {
    const marked = (tag: string) =>
      `<${tag} data-settings-row-field="token">Token for New action</${tag}>`;
    expect(
      audit(
        row(
          region(
            `${marked("button")}<a href="#token" data-settings-row-field="token">Token for New action</a>`,
          ),
        ),
      ),
    ).toEqual([
      'button "Token for New action": row field "token" marks a control that takes no value',
      'a "Token for New action": row field "token" marks a control that takes no value',
    ]);
  });

  it("fails a row control in another entry's row nested inside the region", () => {
    const nested = `<div data-settings-item="fixture.accent"><span>Accent</span>${DELETE}</div>`;
    expect(audit(row(region(nested)))).toContain(
      'button "Delete": is in fixture.accent\'s row inside a region of fixture.sync',
    );
  });

  describe("in the dialog a row action opens", () => {
    const dialog = (body: string, value = "fixture.sync:items:delete") =>
      `<div role="dialog" data-settings-row-overlay="${value}">${body}</div>`;
    const CONFIRM = `<input aria-label="Why delete Tracker" data-settings-row-field="reason"><button data-settings-row-action="cancel">Cancel</button><button data-settings-row-action="confirm">Delete</button><button data-slot="dialog-close">Close</button>`;

    it("take the dialog's declared actions and fields, opened from a row that shows its action", () => {
      expect(audit(row(region(DELETE)), [], dialog(CONFIRM))).toEqual([]);
    });

    it("fail the region's own controls, a wrong name, and anything unmarked", () => {
      expect(
        audit(
          row(region(DELETE)),
          [],
          dialog(
            `${DELETE}${TOKEN("Token for Tracker")}<button data-settings-row-action="confirm">Delete now</button><button>Other</button>`,
          ),
        ),
      ).toEqual([
        'button "Delete": row action "delete" isn\'t declared for fixture.sync:items:delete',
        'input "Token for Tracker": row field "token" isn\'t declared for fixture.sync:items:delete',
        'button "Delete now": row action "confirm" is named "Delete now", but fixture.sync:items:delete names it "Delete"',
        'button "Other": has no owner in the Settings catalog',
      ]);
    });

    it("fail a dialog no region declares, or one no row on the page opens", () => {
      const cancel = '<button data-settings-row-action="cancel">Cancel</button>';
      expect(audit(row(region(DELETE)), [], dialog(cancel, "fixture.sync:servers:open"))).toEqual([
        'row dialog "fixture.sync:servers:open": is in a row\'s dialog "fixture.sync:servers:open" that no region declares',
        'button "Cancel": is in a row\'s dialog "fixture.sync:servers:open" that no region declares',
      ]);
      expect(audit(row(region("")), [], dialog(cancel))).toEqual([
        'row dialog "fixture.sync:items:delete": no row of fixture.sync:items shows the action that opens it',
      ]);
      // The action outside its region, or its region outside the entry's row, opens nothing.
      expect(audit(`${row("")}${region(DELETE)}`, [], dialog(cancel))).toContain(
        'row dialog "fixture.sync:items:delete": no row of fixture.sync:items shows the action that opens it',
      );
    });
  });

  it("lets a row field's own label have a help button, and nothing else named like one", () => {
    const fieldRow = (field: string, help: string) =>
      row(
        region(
          `<div data-settings-region-row><button aria-label="About ${help}">?</button><input aria-label="${field}" data-settings-row-field="token"></div>`,
        ),
      );
    expect(audit(fieldRow("Token for Tracker", "Tracker"))).toEqual([]);
    expect(audit(fieldRow("Token for Tracker", "Notes"))).toEqual([
      'button "About Notes": has no owner in the Settings catalog',
    ]);
  });

  it("lets a region sit in an overlay its own row opens, and only its own", () => {
    const inOverlay = (owner: string) =>
      `<div role="dialog" data-settings-overlay-owner="${owner}">${region(DELETE)}</div>`;
    expect(audit(row(""), [], inOverlay("fixture.sync:reset"))).toEqual([]);
    expect(audit(row(""), [], inOverlay("fixture.accent"))).toContain(
      'button "Delete": is in a region of fixture.sync that sits in fixture.accent',
    );
  });

  it("fails a region in another row, of an unknown entry or region, or no region at all", () => {
    const accentRow = (body: string) =>
      `<div data-settings-item="fixture.accent"><span>Accent</span>${body}</div>`;
    expect(audit(accentRow(region(DELETE)))).toEqual([
      'button "Delete": is in a region of fixture.sync that sits in fixture.accent',
    ]);
    expect(audit(row(region(DELETE, "fixture.missing:items")))).toEqual([
      'button "Delete": is in a data region of an entry that isn\'t in the catalog (fixture.missing:items)',
    ]);
    expect(audit(row(region(DELETE, "fixture.sync:other")))).toEqual([
      'button "Delete": is in "other", which isn\'t a declared region of fixture.sync',
    ]);
    expect(audit(row(DELETE))).toEqual([
      'button "Delete": is a row control outside any data region',
    ]);
  });
});

describe("a dialog's parts in its states", () => {
  const FORM: SettingsEntry = {
    id: "fixture.password",
    section: "account",
    label: "Password",
    description: "Your password.",
    parts: { change: "Change password", current: "Current password", done: "Done" },
    presence: {
      current: { overlay: "change", states: ["editing"] },
      done: { overlay: "change", states: ["changed"] },
    },
    nameAlternatives: { change: { busy: "Changing…" } },
  };
  const entries = new Map([[FORM.id, FORM]]);
  const ROW = `<div data-settings-item="fixture.password"><span>Password</span>
    <button aria-label="Change password" data-settings-control="fixture.password:change"></button></div>`;
  const CURRENT =
    '<input aria-label="Current password" data-settings-control="fixture.password:current">';
  const DONE = '<button aria-label="Done" data-settings-control="fixture.password:done"></button>';
  const dialog = (body: string, owner = "fixture.password:change") =>
    `<div data-settings-overlay-owner="${owner}">${body}</div>`;
  function run(portal: string, states: string[]) {
    document.body.innerHTML = `<main>${ROW}</main><aside>${portal}</aside>`;
    return auditSettingsDom({
      roots: [document.querySelector("main")!, document.querySelector("aside")!],
      entries,
      expected: [FORM],
      drivenStates: new Map([[FORM.id, new Set(states)]]),
    });
  }

  it("needs a part while its overlay is open and its state is driven, and not otherwise", () => {
    expect(run(dialog(CURRENT), ["editing"])).toEqual([]);
    expect(run(dialog(DONE), ["changed"])).toEqual([]);
    // Closed, nothing is needed.
    expect(run("", ["editing"])).toEqual([]);
  });

  it("fails a part in a state not driven, missing while needed, or outside its overlay", () => {
    expect(run(dialog(`${CURRENT}${DONE}`), ["editing"])).toEqual([
      'fixture.password: part "done" renders, though none of the states it shows in is driven',
    ]);
    expect(run(dialog(""), ["editing"])).toEqual([
      'fixture.password: part "current" doesn\'t render',
    ]);
    expect(run(`${dialog("")}${CURRENT}`, ["editing"])).toContain(
      'fixture.password: part "current" doesn\'t render',
    );
    expect(
      run(dialog(`<div data-settings-overlay-owner="fixture.password">${CURRENT}</div>`), [
        "editing",
      ]),
    ).toContain('fixture.password: part "current" doesn\'t render');
  });

  it("names a part by a declared alternative, and no other", () => {
    const busy = ROW.replace('aria-label="Change password"', 'aria-label="Changing…"');
    document.body.innerHTML = `<main>${busy}</main>`;
    expect(auditSettingsDom({ roots: [document.querySelector("main")!], entries })).toEqual([]);
    document.body.innerHTML = `<main>${ROW.replace('aria-label="Change password"', 'aria-label="Saving…"')}</main>`;
    expect(auditSettingsDom({ roots: [document.querySelector("main")!], entries })).toEqual([
      'button "Saving…": is named "Saving…", but fixture.password names it "Change password"',
    ]);
  });
});

describe("a tab list's tabs and panels", () => {
  const PAGES: SettingsEntry = {
    id: "fixture.pages",
    section: "general",
    label: "Pages",
    description: "A page's views.",
    parts: { tab: "Pages", other: "Other pages" },
    partChoices: { tab: ["Settings", "Servers"] },
    choiceTemplates: { tab: { Servers: { before: "Servers · ", count: true } } },
  };
  const entries = new Map([PAGES, SYNC].map((entry) => [entry.id, entry]));
  const tab = (name: string, id: string, controls: string, selected = false) =>
    `<button role="tab" id="${id}" aria-controls="${controls}" aria-selected="${selected}">${name}</button>`;
  const list = (tabs: string, part = "tab") =>
    `<div role="tablist" tabindex="0" aria-label="${PAGES.parts![part]}" data-settings-control="fixture.pages:${part}">${tabs}</div>`;
  const panel = (id: string, labelledBy: string, body = "") =>
    `<div role="tabpanel" tabindex="0" id="${id}" aria-labelledby="${labelledBy}">${body}</div>`;
  const row = (body: string) =>
    `<div data-settings-item="fixture.pages"><span>Pages</span><div data-slot="tabs">${body}</div></div>`;
  const VALID =
    list(
      tab("Settings", "t-settings", "p-settings", true) +
        tab("Servers · 2", "t-servers", "p-servers"),
    ) +
    panel("p-settings", "t-settings", "Settings view") +
    // An inactive panel renders hidden and empty.
    `<div role="tabpanel" tabindex="0" hidden id="p-servers" aria-labelledby="t-servers"></div>`;
  function run(html: string): string[] {
    document.body.innerHTML = `<main>${html}</main>`;
    return auditSettingsDom({ roots: [document.querySelector("main")!], entries });
  }

  it("accepts an owned tab list, static and counted tabs, and their panels, active or not", () => {
    expect(run(row(VALID))).toEqual([]);
  });

  it("takes a counted choice only around a whole number", () => {
    const named = (name: string) => run(row(list(tab(name, "t", "p", true)) + panel("p", "t")));
    for (const name of ["Servers · 0", "Servers · 12"]) expect(named(name)).toEqual([]);
    for (const name of ["Servers · 1.5", "Servers · -1", "Servers · 01", "Servers · two"]) {
      expect(named(name)).toEqual([
        `button[role=tab] "${name}": is a tab of fixture.pages:tab named "${name}", which its entry doesn't list`,
        `div[role=tabpanel] "${name}": is the panel of a tab that is a tab of fixture.pages:tab named "${name}", which its entry doesn't list`,
      ]);
    }
    // The other part declares no choices, so even a static one isn't its.
    expect(run(row(list(tab("Settings", "t", "p", true), "other") + panel("p", "t")))).toEqual([
      'button[role=tab] "Settings": is a tab of fixture.pages:other named "Settings", which its entry doesn\'t list',
      'div[role=tabpanel] "Settings": is the panel of a tab that is a tab of fixture.pages:other named "Settings", which its entry doesn\'t list',
    ]);
  });

  it("leaves an unowned tab list's tabs and panels unowned, and rejects a tab outside any list", () => {
    const unowned = `<div data-slot="tabs"><div role="tablist" tabindex="0">${tab("A", "t", "p", true)}</div>${panel("p", "t")}</div>`;
    expect(run(unowned)).toEqual([
      'div[role=tablist] "": has no owner in the Settings catalog',
      'button[role=tab] "A": has no owner in the Settings catalog',
      'div[role=tabpanel] "A": has no owner in the Settings catalog',
    ]);
    expect(run(row(tab("Settings", "t", "p")))).toEqual([
      'button[role=tab] "Settings": is a tab outside any tab list',
    ]);
  });

  it("covers a panel only through one reciprocal owned tab in its group and row", () => {
    const owned = list(tab("Settings", "t", "p", true));
    const cases: [string, string][] = [
      [
        owned + `<div role="tabpanel" tabindex="0" id="p"></div>`,
        "is a tab panel not labelled by exactly one tab",
      ],
      [owned + panel("p", "t other"), "is a tab panel not labelled by exactly one tab"],
      [
        owned + `<div role="tabpanel" tabindex="0" aria-labelledby="t"></div>`,
        "is a tab panel without an id for its tab to name",
      ],
      [
        owned + panel("p", "t") + `<span id="p"></span>`,
        'is a tab panel whose id "p" isn\'t unique',
      ],
      [owned + panel("p", "missing"), 'is labelled by "missing", which isn\'t exactly one element'],
      [
        owned + `<span id="label">Settings</span>` + panel("p", "label"),
        "is a tab panel labelled by something that isn't a tab",
      ],
      [
        list(tab("Settings", "t", "elsewhere", true)) + panel("p", "t"),
        "is a tab panel its tab doesn't name with aria-controls",
      ],
    ];
    for (const [body, reason] of cases) {
      const problems = run(row(body)).filter((problem) => problem.startsWith("div[role=tabpanel]"));
      expect(problems.map((problem) => problem.replace(/^[^:]*: /, ""))).toEqual([reason]);
    }
    // Another group's panel, and a panel borrowed by a nested row, aren't the tab's.
    const otherGroup = row(owned).replace(
      "</div></div>",
      `</div><div data-slot="tabs">${panel("p", "t")}</div></div>`,
    );
    expect(run(otherGroup).filter((problem) => problem.includes("tabpanel"))).toEqual([
      'div[role=tabpanel] "Settings": is a tab panel of another tab group',
    ]);
    const nestedRow = row(
      owned + `<div data-settings-item="fixture.sync"><span>Sync</span>${panel("p", "t")}</div>`,
    );
    expect(run(nestedRow).filter((problem) => problem.includes("tabpanel"))).toEqual([
      'div[role=tabpanel] "Settings": is a tab panel outside its tab\'s row',
    ]);
  });

  it("rejects a tab that sits in another row inside its list", () => {
    const nested = list(
      `<div data-settings-item="fixture.sync"><span>Sync</span>${tab("Settings", "t", "p", true)}</div>`,
    );
    expect(run(row(nested)).filter((problem) => problem.startsWith("button[role=tab]"))).toEqual([
      'button[role=tab] "Settings": is a tab of fixture.pages:tab in another row',
    ]);
  });

  it("checks a tab's and a panel's structure before any owner marker on them", () => {
    expect(
      run(
        row(
          `<button role="tab" aria-label="Pages view" data-settings-control="fixture.pages:tab">Pages view</button>`,
        ),
      ),
    ).toEqual([
      'button[role=tab] "Pages view": is a tab with an owner of its own; a tab is a choice of its owned tab list',
    ]);
    expect(
      run(
        row(
          list(tab("Settings", "t", "p", true)) +
            `<div role="tabpanel" tabindex="0" id="p" aria-labelledby="t" data-settings-control="fixture.pages:tab">View</div>`,
        ),
      ),
    ).toEqual([
      'div[role=tabpanel] "Settings": is a tab panel with an owner of its own; its tab covers it',
    ]);
  });

  it("checks a file-input or runtime-choice marker on a tab or panel before its structure", () => {
    const owned = (marker: string) =>
      list(
        `<button role="tab" id="t" aria-controls="p" aria-selected="true" ${marker}>Settings</button>`,
      ) + panel("p", "t");
    const panelMarked = (marker: string) =>
      list(tab("Settings", "t", "p", true)) +
      `<div role="tabpanel" tabindex="0" id="p" aria-labelledby="t" ${marker}></div>`;
    const file = 'data-settings-file-input="fixture.pages:tab"';
    const choice = 'data-settings-runtime-choice="fixture.sync:reset"';
    const reasons = (html: string) =>
      run(row(html)).map((problem) => problem.replace(/^[^:]*: /, ""));
    expect(reasons(owned(file))).toEqual([expect.stringContaining("isn't a file input")]);
    expect(reasons(owned(choice))).toEqual([expect.stringContaining("isn't an option")]);
    expect(reasons(panelMarked(file))).toEqual([expect.stringContaining("isn't a file input")]);
    expect(reasons(panelMarked(choice))).toEqual([expect.stringContaining("isn't an option")]);
    // A panel that takes no focus is reported once, by the ownership pass, not again by the sweep.
    expect(
      reasons(
        list(tab("Settings", "t", "p", true)) +
          `<div role="tabpanel" id="p" aria-labelledby="t" ${file}></div>`,
      ),
    ).toEqual([expect.stringContaining("isn't a file input")]);
  });

  it("checks tab lists and panels that take no focus, but leaves unowned ones as they were", () => {
    const owned = list(tab("Settings", "t", "p", true));
    for (const tabindex of ["", ' tabindex="-1"']) {
      expect(
        run(row(owned + `<div role="tabpanel"${tabindex} id="p" aria-labelledby="missing"></div>`)),
      ).toEqual([
        'div[role=tabpanel] "": is labelled by "missing", which isn\'t exactly one element',
      ]);
    }
    const misnamed = `<div role="tablist" aria-label="Wrong" data-settings-control="fixture.pages:tab">${tab("Settings", "t", "p", true)}</div>`;
    expect(run(row(misnamed + panel("p", "t")))).toEqual([
      'div[role=tablist] "Wrong": is named "Wrong", but fixture.pages names it "Pages"',
    ]);
    // An unowned list and panel that take no focus aren't controls, as before; the tab still is.
    expect(
      run(
        `<div data-slot="tabs"><div role="tablist">${tab("A", "t", "p", true)}</div><div role="tabpanel" id="p" aria-labelledby="t"></div></div>`,
      ),
    ).toEqual(['button[role=tab] "A": has no owner in the Settings catalog']);
  });

  it("still audits the controls inside a covered panel", () => {
    expect(
      run(row(list(tab("Settings", "t", "p", true)) + panel("p", "t", `<button>Extra</button>`))),
    ).toEqual(['button "Extra": has no owner in the Settings catalog']);
  });
});

describe("a content area", () => {
  const row = (body: string) => OWNED_ROW.replace(/<\/div>$/, `${body}</div>`);
  const area = (body: string, value = "fixture.sync:doc") =>
    `<div data-settings-content="${value}">${body}</div>`;
  const LINK = '<a href="https://example.com/guide" rel="noreferrer" target="_blank">Guide</a>';
  const reasons = (html: string) => audit(html).map((problem) => problem.replace(/^[^:]*: /, ""));

  it("holds links out in its entry's row, and nothing else needs an owner there", () => {
    expect(audit(row(area(`<p>Read the ${LINK}.</p>`)))).toEqual([]);
  });

  it("holds only plain links out: not a relative link, a button, a marked link or a task box", () => {
    const only = "is in a content area of fixture.sync, which holds only links out";
    for (const body of [
      '<a href="#section">Section</a>',
      "<button>Copy</button>",
      '<a href="https://example.com" data-settings-control="fixture.sync">Guide</a>',
      '<input type="checkbox" disabled checked>',
      // A link out that says it's something else.
      '<a href="https://example.com" role="button">Run</a>',
    ]) {
      expect(reasons(row(area(body)))).toEqual([only]);
    }
  });

  it("holds only links even where an overlay's option would be valid", () => {
    const APPS: SettingsEntry = {
      id: "fixture.apps",
      section: "general",
      label: "Apps",
      description: "Which app opens.",
      parts: { pick: "Pick" },
      overlays: { pick: { runtimeChoices: { fixed: ["None"] } } },
      contentAreas: ["doc"],
    };
    const inOverlay = (body: string) =>
      `<div data-settings-item="fixture.apps">Apps<button aria-label="Pick" data-settings-control="fixture.apps:pick">Pick</button>` +
      `<div data-settings-overlay-owner="fixture.apps:pick"><div data-settings-content="fixture.apps:doc">${body}</div></div></div>`;
    const run = (body: string) => {
      document.body.innerHTML = `<main>${inOverlay(body)}</main>`;
      return auditSettingsDom({
        roots: [document.querySelector("main")!],
        entries: new Map([[APPS.id, APPS]]),
      });
    };
    const only = "is in a content area of fixture.apps, which holds only links out";
    expect(
      run(
        '<a href="https://example.test/guide" role="option" data-settings-runtime-choice="fixture.apps:pick">Guide</a>',
      ),
    ).toEqual([`a[role=option] "Guide": ${only}`]);
    expect(run('<button role="option">None</button>')).toEqual([
      `button[role=option] "None": ${only}`,
    ]);
  });

  it("is a declared area in its own row, claimed once and not nested", () => {
    // The area and the link in it are each reported.
    expect(audit(row(area(LINK, "fixture.sync:other")))).toEqual([
      'content area "fixture.sync:other": is in "fixture.sync:other", which isn\'t a declared content area',
      'a "Guide": is in "fixture.sync:other", which isn\'t a declared content area',
    ]);
    const accent = `<div data-settings-item="fixture.accent"><span>Accent</span>${area(LINK)}</div>`;
    expect(audit(accent)).toEqual([
      'content area "fixture.sync:doc": is in a content area of fixture.sync outside its row',
      'a "Guide": is in a content area of fixture.sync outside its row',
    ]);
    // With only text, it's still a declared area in its own row.
    expect(audit(row(area("<p>Just words.</p>", "fixture.sync:other")))).toEqual([
      'content area "fixture.sync:other": is in "fixture.sync:other", which isn\'t a declared content area',
    ]);
    const nestedRow = row(
      area(`<div data-settings-item="fixture.accent"><span>Accent</span>${LINK}</div>`),
    );
    expect(reasons(nestedRow)).toContain("is in another row inside a content area of fixture.sync");
    expect(audit(row(area(LINK) + area(LINK)))).toEqual([
      'content area "fixture.sync:doc": is claimed 2 times in one row',
    ]);
    // Nested, it's inside another and a second claim in the row.
    expect(audit(row(area(area(LINK))))).toEqual([
      'content area "fixture.sync:doc": is claimed 2 times in one row',
      'content area "fixture.sync:doc": is inside another',
    ]);
  });
});

describe("a modal's focus guards", () => {
  it("aren't controls, though they take focus; another focusable span still is", () => {
    const guard =
      '<span data-radix-focus-guard="" tabindex="0" aria-hidden="true" style="outline: none; opacity: 0; position: fixed; pointer-events: none;"></span>';
    expect(audit(`${OWNED_ROW}${guard}${guard}`, [SYNC])).toEqual([]);
    expect(audit(`${OWNED_ROW}<span tabindex="0">Focus me</span>`, [SYNC])).toEqual([
      'span "": has no owner in the Settings catalog',
    ]);
    // Without the guard's own marks, it's no guard.
    expect(
      audit(`${OWNED_ROW}<span data-radix-focus-guard="" tabindex="0"></span>`, [SYNC]),
    ).toEqual(['span "": has no owner in the Settings catalog']);
  });
});

describe("a captured overlay", () => {
  // A fix dialog its own Set up opens, which goes away once the fix installs; and a member's twin.
  const fix = (id: string, captured: boolean): SettingsEntry => ({
    id,
    section: "general",
    label: "Fix",
    description: "A harness fix.",
    parts: { setUp: "Set up", retry: "Try again" },
    presence: { setUp: { states: ["can be set up"] }, retry: { overlay: "setUp" } },
    overlays: { setUp: captured ? { captured: true } : {} },
  });
  const FIX = fix("fixture.fix-alpha", true);
  const TWIN = fix("fixture.fix-beta", true);
  const PLAIN = fix("fixture.fix-plain", false);
  const entries = new Map([FIX, TWIN, PLAIN].map((entry) => [entry.id, entry]));
  const row = (id: string, opener: boolean) => `
    <div data-settings-item="${id}">
      <span>Fix</span>
      ${opener ? `<button aria-label="Set up" data-settings-control="${id}:setUp">+</button>` : ""}
    </div>`;
  const dialog = (
    owner: string,
    body = `<button aria-label="Try again" data-settings-control="${owner.split(":")[0]}:retry">Try again</button>`,
  ) => `<div role="dialog" data-settings-overlay-owner="${owner}">${body}</div>`;
  const auditFix = (
    entry: SettingsEntry,
    { opener = false, portal = dialog(`${entry.id}:setUp`), origins = [] as string[] } = {},
  ) => {
    document.body.innerHTML = `<main>${row(entry.id, opener)}</main><aside>${portal}</aside>`;
    return auditSettingsDom({
      roots: [document.querySelector("main")!, document.querySelector("aside")!],
      entries,
      expected: [entry],
      origins,
      drivenStates: opener ? new Map([[entry.id, new Set(["can be set up"])]]) : new Map(),
    });
  };

  it("stands in for its opener once that's gone, only with an origin proved for it", () => {
    expect(auditFix(FIX, { origins: ["fixture.fix-alpha:setUp"] })).toEqual([]);
    const unproved =
      'overlay "fixture.fix-alpha:setUp": no origin was proved for this captured overlay';
    // Being captured proves nothing; nor does another member's origin.
    expect(auditFix(FIX)).toEqual([unproved]);
    expect(auditFix(FIX, { origins: ["fixture.fix-beta:setUp"] })).toEqual([unproved]);
    // A live opener is no proof either: the overlay answers to the origin proved for it.
    expect(auditFix(FIX, { opener: true })).toEqual([unproved]);
    expect(auditFix(FIX, { opener: true, origins: ["fixture.fix-beta:setUp"] })).toEqual([
      unproved,
    ]);
    expect(auditFix(FIX, { opener: true, origins: ["fixture.fix-alpha:setUp"] })).toEqual([]);
    // An ordinary overlay keeps needing its opener, whatever origin is offered.
    expect(auditFix(PLAIN, { origins: ["fixture.fix-plain:setUp"] })).toEqual([
      'overlay "fixture.fix-plain:setUp": fixture.fix-plain\'s row has no control that opens it',
    ]);
  });

  it("still holds its parts, its place and its owner to the catalog", () => {
    const origins = ["fixture.fix-alpha:setUp"];
    expect(auditFix(FIX, { origins, portal: dialog("fixture.fix-alpha:setUp", "") })).toEqual([
      'fixture.fix-alpha: part "retry" doesn\'t render',
    ]);
    expect(
      auditFix(FIX, {
        origins,
        portal: `<div data-settings-overlay-owner="fixture.fix-beta:setUp">${dialog("fixture.fix-alpha:setUp")}</div>`,
      }),
    ).toContain(
      'overlay "fixture.fix-alpha:setUp": a captured overlay opens on the page, not inside another',
    );
    // Content without an owner is no overlay of its, and its controls are unowned.
    expect(
      auditFix(FIX, {
        origins,
        portal: '<div role="dialog"><button aria-label="Try again">Try again</button></div>',
      }),
    ).toEqual(['button "Try again": has no owner in the Settings catalog']);
  });

  it("is proved only from its opener, owned in its own row, before it opens", () => {
    document.body.innerHTML = `<main>${row("fixture.fix-alpha", true)}</main>`;
    expect(provenOverlayOrigin("fixture.fix-alpha:setUp")).toBe("fixture.fix-alpha:setUp");
    document.body.innerHTML = `<main>${row("fixture.fix-alpha", false)}</main>`;
    expect(() => provenOverlayOrigin("fixture.fix-alpha:setUp")).toThrow(/isn't rendered/);
    // An opener's attributes copied into the dialog aren't the opener.
    document.body.innerHTML = `<main>${row("fixture.fix-alpha", false)}</main><aside><div data-settings-overlay-owner="fixture.fix-alpha:setUp"><button data-settings-control="fixture.fix-alpha:setUp">Set up</button></div></aside>`;
    expect(() => provenOverlayOrigin("fixture.fix-alpha:setUp")).toThrow(/isn't rendered/);
    // Nor is its marker inside another row nested in its own, or on something that's no control.
    document.body.innerHTML = `<main><div data-settings-item="fixture.fix-alpha"><span>Fix</span><div data-settings-item="fixture.fix-beta"><span>Fix</span><button aria-label="Set up" data-settings-control="fixture.fix-alpha:setUp">+</button></div></div></main>`;
    expect(() => provenOverlayOrigin("fixture.fix-alpha:setUp")).toThrow(/isn't rendered/);
    document.body.innerHTML = `<main><div data-settings-item="fixture.fix-alpha"><span>Fix</span><span aria-label="Set up" data-settings-control="fixture.fix-alpha:setUp">+</span></div></main>`;
    expect(() => provenOverlayOrigin("fixture.fix-alpha:setUp")).toThrow(/isn't rendered/);
    // Nor is a control an overlay holds, even an overlay rendered inside its own row.
    document.body.innerHTML = `<main><div data-settings-item="fixture.fix-alpha"><span>Fix</span><div data-settings-overlay-owner="fixture.fix-alpha:setUp"><button aria-label="Set up" data-settings-control="fixture.fix-alpha:setUp">+</button></div></div></main>`;
    expect(() => provenOverlayOrigin("fixture.fix-alpha:setUp")).toThrow(/isn't rendered/);
  });

  it("is audited against the origin proved before it opened, even after its opener goes", () => {
    document.body.innerHTML = `<main>${row("fixture.fix-alpha", true)}${row("fixture.fix-beta", true)}</main>`;
    const origin = provenOverlayOrigin("fixture.fix-alpha:setUp");
    // Another member's dialog, though its own opener is live, isn't the one proved.
    document.body.insertAdjacentHTML(
      "beforeend",
      `<aside>${dialog("fixture.fix-beta:setUp")}</aside>`,
    );
    const roots = [document.querySelector("main")!, document.querySelector("aside")!];
    const driven = new Map([
      ["fixture.fix-alpha", new Set(["can be set up"])],
      ["fixture.fix-beta", new Set(["can be set up"])],
    ]);
    expect(
      auditSettingsDom({
        roots,
        entries,
        expected: [FIX, TWIN],
        origins: [origin],
        drivenStates: driven,
      }),
    ).toEqual(['overlay "fixture.fix-beta:setUp": no origin was proved for this captured overlay']);
    // Its own dialog, once its opener has gone, still passes on that origin.
    document.body.innerHTML = `<main>${row("fixture.fix-alpha", false)}</main><aside>${dialog("fixture.fix-alpha:setUp")}</aside>`;
    expect(
      auditSettingsDom({
        roots: [document.querySelector("main")!, document.querySelector("aside")!],
        entries,
        expected: [FIX],
        origins: [origin],
      }),
    ).toEqual([]);
  });
});
