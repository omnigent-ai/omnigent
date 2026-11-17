// The rendered-page audit against small DOM fixtures: an owned row passes, and each way a
// control can escape its entry is reported.

import { afterEach, describe, expect, it } from "vitest";

import type { SettingsEntry } from "@/pages/settings/search/catalog/types";

import {
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
