// The rendered half of searchable Settings' checks: in a rendered page (and its portals),
// every control belongs to the catalog entry of the row it sits in, carries that entry's name,
// and every entry expected here renders exactly one row with all of its parts.

import { computeAccessibleName } from "dom-accessibility-api";

import {
  type SettingsEntry,
  type SettingsNameAffix,
  type SettingsRegion,
  type SettingsRowOverlay,
  settingsChoiceNamed,
  settingsRevealer,
} from "@/pages/settings/search/catalog/types";
import { settingsControlId } from "@/pages/settings/search/settingsControl";

/** A <details>'s control: its first <summary> child, wherever among its children that is. */
const DISCLOSURE = "details > summary:first-of-type";
const INTERACTIVE = [
  "button",
  "a[href]",
  "input:not([type=hidden])",
  "select",
  "textarea",
  "[tabindex]:not([tabindex='-1'])",
  ...[
    "button",
    "link",
    "switch",
    "checkbox",
    "radio",
    "combobox",
    "slider",
    "spinbutton",
    "tab",
    "textbox",
    "menuitem",
    "menuitemradio",
    "menuitemcheckbox",
    "option",
  ].map((role) => `[role=${role}]`),
  DISCLOSURE,
].join(", ");

const OPTION = "option, input[type=radio], [role=option], [role=radio], [role=menuitemradio]";
const TAB = "[role=tab]";
const TAB_LIST = "[role=tablist]";
const TAB_PANEL = "[role=tabpanel]";
/** What the ownership pass audits: every control, plus tab lists and panels, focusable or not. */
const AUDITED = `${INTERACTIVE}, ${TAB_LIST}, ${TAB_PANEL}`;
/** Radix's focus-trap sentinels around an open modal: hidden, and not anything to operate. */
const FOCUS_GUARD = "span[data-radix-focus-guard][aria-hidden='true']";
/** The select primitive's hidden native select, which carries its value in a form. */
const FORM_VALUE_SELECT = "select[aria-hidden='true'][tabindex='-1']";

export interface SettingsDomAuditInput {
  /** The page, plus any open portals. */
  roots: readonly Element[];
  entries: ReadonlyMap<string, SettingsEntry>;
  /** Entries this render should show, checked for one row each with every part. */
  expected?: readonly SettingsEntry[];
  /**
   * The states each entry is in here, by entry id, such as `"load failed"`. A part declared to
   * show in some states must show when any of them is driven, and must not show otherwise.
   */
  drivenStates?: ReadonlyMap<string, ReadonlySet<string>>;
  /**
   * The overlays the page audit proved, before opening them, were opened by their owned
   * opener: `entry:part` each. A captured overlay among them needn't find its opener still
   * rendered; every other check of it and its parts stands.
   */
  origins?: readonly string[];
}

/** The name a control shows for `entry`, or for one of its parts. */
function controlName(entry: SettingsEntry, part?: string): string {
  return part === undefined ? entry.label : (entry.parts?.[part] ?? entry.label);
}

function textOf(element: Element): string {
  return (element.textContent ?? "").replace(/\s+/g, " ").trim();
}

/** The accessible name a browser exposes, as testing-library computes it. */
export function accessibleName(element: Element): string {
  return computeAccessibleName(element);
}

const HEADING = "h1, h2, h3, h4, h5, h6, [role=heading]";

/**
 * Whether a row with no label of its own is named by a heading showing `label`: the one its
 * aria-labelledby names, or else the nearest heading before it that sits outside every row.
 */
function rowNamedByHeading(row: Element, label: string): boolean {
  const labelledBy = (row.getAttribute("aria-labelledby") ?? "").split(/\s+/).filter(Boolean);
  if (labelledBy.length > 0) {
    const heading =
      labelledBy.length === 1 ? row.ownerDocument.getElementById(labelledBy[0]!) : null;
    return heading !== null && heading.matches(HEADING) && textOf(heading) === label;
  }
  let nearest: Element | null = null;
  for (const heading of row.ownerDocument.querySelectorAll(HEADING)) {
    const precedes =
      (heading.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
    if (precedes && !heading.contains(row) && heading.closest("[data-settings-item]") === null) {
      nearest = heading;
    }
  }
  return nearest !== null && textOf(nearest) === label;
}

function describe(element: Element): string {
  const role = element.getAttribute("role");
  return `${element.tagName.toLowerCase()}${role ? `[role=${role}]` : ""} "${accessibleName(element)}"`;
}

function parseControlId(value: string): { id: string; part?: string } {
  const separator = value.indexOf(":");
  // A row's main control is `id:`, the same form its hash takes, and names no part.
  if (separator === -1 || separator === value.length - 1) {
    return { id: separator === -1 ? value : value.slice(0, separator) };
  }
  return { id: value.slice(0, separator), part: value.slice(separator + 1) };
}

/**
 * One audit finding: `unowned`, a control with no catalog owner at all, or any other `problem`,
 * such as a wrong name, a control in the wrong row or overlay, or a row or part that doesn't render.
 */
export interface SettingsDomFinding {
  kind: "unowned" | "problem";
  /** The control itself, so an audit of one opened portal can tell which findings are in it. */
  element?: Element;
  message: string;
}

/**
 * The audit of a page while an action has `opened` some portals, such as a dialog, the page
 * having been audited a moment before. Every problem counts, wherever it is, including the
 * open overlay's parts that must render inside it and nowhere else. An unowned control counts
 * only inside what opened: the page's own were counted, once, by its audit.
 */
export function auditOpenedPortals(
  input: SettingsDomAuditInput & { opened: readonly Element[] },
): SettingsDomFinding[] {
  return auditSettingsDomFindings(input).filter(
    (finding) =>
      finding.kind === "problem" ||
      input.opened.some((node) => finding.element !== undefined && node.contains(finding.element)),
  );
}

export function auditSettingsDom(input: SettingsDomAuditInput): string[] {
  return auditSettingsDomFindings(input).map((finding) => finding.message);
}

export function auditSettingsDomFindings({
  roots,
  entries,
  expected = [],
  drivenStates = new Map(),
  origins = [],
}: SettingsDomAuditInput): SettingsDomFinding[] {
  const findings: SettingsDomFinding[] = [];
  const problem = (message: string) => findings.push({ kind: "problem", message });
  const entryFor = (value: string | null) =>
    value === null
      ? undefined
      : { ...parseControlId(value), entry: entries.get(parseControlId(value).id) };

  const inRowOf = (id: string, selector: string) =>
    roots.some((root) =>
      [...root.querySelectorAll(`[data-settings-item="${CSS.escape(id)}"]`)].some((row) =>
        row.querySelector(selector),
      ),
    );

  // Portaled content must name an owner whose row renders the control that opens it.
  for (const root of roots) {
    for (const overlay of [
      ...(root.matches("[data-settings-overlay-owner]") ? [root] : []),
      ...root.querySelectorAll("[data-settings-overlay-owner]"),
    ]) {
      const value = overlay.getAttribute("data-settings-overlay-owner") ?? "";
      const owner = entryFor(value);
      if (!owner?.entry) {
        problem(`overlay "${value}": names an entry that isn't in the catalog`);
        continue;
      }
      if (owner.part !== undefined && owner.entry.parts?.[owner.part] === undefined) {
        problem(`overlay "${value}": "${owner.part}" isn't a part of ${owner.id}`);
        continue;
      }
      const opener = `[data-settings-control="${CSS.escape(value)}"], [data-settings-widget="${CSS.escape(value)}"]`;
      const captured = owner.entry.overlays?.[owner.part ?? ""]?.captured === true;
      if (captured && overlay.parentElement?.closest("[data-settings-overlay-owner]")) {
        problem(`overlay "${value}": a captured overlay opens on the page, not inside another`);
      }
      // A captured overlay answers to the origin proved before it opened, live opener or not;
      // an ordinary one needs its opener in its row.
      if (captured) {
        if (!origins.includes(value)) {
          problem(`overlay "${value}": no origin was proved for this captured overlay`);
        }
      } else if (!inRowOf(owner.id, opener)) {
        problem(`overlay "${value}": ${owner.id}'s row has no control that opens it`);
      }
    }
  }

  // A row's dialog is one its region declares, opened from that region on the page: one of its
  // rows still shows the action that opens it.
  for (const dialog of withinRoots(roots, ROW_OVERLAY)) {
    const value = dialog.getAttribute("data-settings-row-overlay") ?? "";
    const declared = rowOverlayDeclaration(value, entryFor);
    if (typeof declared === "string") {
      problem(`row dialog "${value}": ${declared}`);
      continue;
    }
    const [id, region, action] = value.split(":") as [string, string, string];
    const opener = `[data-settings-region="${CSS.escape(`${id}:${region}`)}"] [data-settings-row-action="${CSS.escape(action)}"]`;
    if (!inRowOf(id, opener)) {
      problem(`row dialog "${value}": no row of ${id}:${region} shows the action that opens it`);
    }
    if (dialog.parentElement?.closest(`${ROW_OVERLAY}, [data-settings-overlay-owner]`)) {
      problem(`row dialog "${value}": opens on the page, not inside another overlay`);
    }
  }

  // A content area is claimed once in its row, and not inside another.
  for (const root of roots) {
    const areas = [...(root.matches(CONTENT) ? [root] : []), ...root.querySelectorAll(CONTENT)];
    for (const element of areas) {
      // Even one holding only text is a declared area in its entry's row.
      const placement = contentAreaProblem(element, entryFor);
      if (placement) {
        problem(`content area "${element.getAttribute("data-settings-content")}": ${placement}`);
      }
      if (element.parentElement?.closest(CONTENT)) {
        problem(
          `content area "${element.getAttribute("data-settings-content")}": is inside another`,
        );
      }
      const row = element.closest("[data-settings-item]");
      const value = element.getAttribute("data-settings-content");
      const claims = areas.filter(
        (other) =>
          other.getAttribute("data-settings-content") === value &&
          other.closest("[data-settings-item]") === row,
      );
      if (claims[0] === element && claims.length > 1) {
        problem(`content area "${value}": is claimed ${claims.length} times in one row`);
      }
    }
  }

  // A region row sits in a data region, and not inside another row of it.
  for (const root of roots) {
    for (const element of [
      ...(root.matches(REGION_ROW) ? [root] : []),
      ...root.querySelectorAll(REGION_ROW),
    ]) {
      const region = element.closest("[data-settings-region]");
      if (!region) problem(`${describe(element)}: is a region row outside any data region`);
      else if (
        element.parentElement?.closest(REGION_ROW)?.closest("[data-settings-region]") === region
      ) {
        problem(`${describe(element)}: is a region row inside another row of its region`);
      }
    }
  }

  // A row action or field marks a control; on anything else, such as a disclosure's later
  // <summary>, it owns nothing, so it's refused rather than left looking owned.
  for (const element of withinRoots(
    roots,
    "[data-settings-row-action], [data-settings-row-field]",
  )) {
    if (!element.matches(AUDITED)) {
      problem(`${describe(element)}: carries a row marker, but isn't a control`);
    }
  }

  // Without a <summary>, the browser gives a <details> a "Details" control no one owns.
  for (const details of withinRoots(roots, "details")) {
    if (![...details.children].some((child) => child.localName === "summary")) {
      problem(`${describe(details)}: is a disclosure with no <summary> of its own`);
    }
  }

  // A file-input marker binds only a hidden file input; elsewhere it's checked here.
  for (const root of roots) {
    for (const element of [
      ...(root.matches(FILE_INPUT) ? [root] : []),
      ...root.querySelectorAll(FILE_INPUT),
    ]) {
      if (element.matches(AUDITED)) continue;
      const reason = fileInputProblem(element, entryFor);
      if (reason) problem(`${describe(element)}: ${reason}`);
    }
  }

  // A runtime-choice marker names an overlay's option; on anything else it owns nothing.
  for (const root of roots) {
    const choiceLike = `${RUNTIME_CHOICE}, ${OPTION}`;
    for (const element of [
      ...(root.matches(choiceLike) ? [root] : []),
      ...root.querySelectorAll(choiceLike),
    ]) {
      // Interactive ones are checked with the other controls, before any other owner.
      if (element.matches(AUDITED)) continue;
      const reason = runtimeOptionProblem(element, accessibleName(element), entryFor);
      if (reason) problem(`${describe(element)}: ${reason}`);
    }
  }

  for (const root of roots) {
    // Tab lists and panels are checked whether or not they take focus: an owned one's name, and
    // a panel's link to its tab. One that takes no focus and has no owner is left as it was.
    // In page order, which a selector engine needn't keep for a list with a combinator in it.
    const controls = [
      ...(root.matches(AUDITED) ? [root] : []),
      ...root.querySelectorAll(AUDITED),
    ].sort((first, second) =>
      first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1,
    );
    for (const control of controls) {
      // A modal's focus-trap sentinels take focus only to send it back inside; no one uses them.
      if (control.matches(FOCUS_GUARD)) continue;
      // Beside its own trigger, it only holds the trigger's value for the form; no one uses it.
      if (
        control.matches(FORM_VALUE_SELECT) &&
        control.previousElementSibling?.matches("[role=combobox]")
      ) {
        continue;
      }
      const reason = ownershipProblem(control, entryFor);
      if (!reason) continue;
      if (reason === NO_OWNER && !control.matches(INTERACTIVE)) continue;
      findings.push({
        kind: reason === NO_OWNER ? "unowned" : "problem",
        element: control,
        message: `${describe(control)}: ${reason}`,
      });
    }
  }

  for (const entry of expected) {
    const rows = roots.flatMap((root) => [
      ...root.querySelectorAll(`[data-settings-item="${CSS.escape(entry.id)}"]`),
    ]);
    const revealer = settingsRevealer(entry)?.id;
    const standsIn =
      revealer !== undefined &&
      roots.some((root) => root.querySelector(`[data-settings-item="${CSS.escape(revealer)}"]`));
    if (rows.length !== 1 && !(rows.length === 0 && standsIn)) {
      problem(`${entry.id}: renders ${rows.length} rows, expected 1`);
    }
    for (const row of rows) {
      if (!textOf(row).includes(entry.label) && !rowNamedByHeading(row, entry.label)) {
        problem(`${entry.id}: its row doesn't show "${entry.label}"`);
      }
    }
    if (rows.length === 0) continue;
    const driven = drivenStates.get(entry.id) ?? new Set<string>();
    for (const part of Object.keys(entry.parts ?? {})) {
      const presence = entry.presence?.[part];
      // A part shows while its overlay is open and while one of its states is driven: each where
      // its presence names one, and both where it names both.
      const overlayOpen =
        presence === undefined ||
        !("overlay" in presence) ||
        withinRoots(roots, overlayOwnerSelector(entry, presence.overlay)).length > 0;
      const stateDriven =
        presence === undefined ||
        !("states" in presence) ||
        presence.states.some((state) => driven.has(state));
      const required = overlayOpen && stateDriven;
      const rendered = partElements(roots, entry, part);
      const inPlace = partInPlace(roots, entry, part, entryFor);
      if (required && !inPlace) {
        problem(`${entry.id}: part "${part}" doesn't render`);
      } else if (!required && rendered.length > 0) {
        problem(
          `${entry.id}: part "${part}" renders, though none of the states it shows in is driven`,
        );
      } else if (presence && "overlay" in presence) {
        // An overlay's part shows only in the overlay its own opener opens, the nearest one.
        const owner = overlayOwnerValue(entry, presence.overlay);
        if (rendered.some((element) => nearestOverlayOwner(element) !== owner)) {
          problem(`${entry.id}: part "${part}" renders outside its opener's overlay`);
        }
      }
    }
  }
  // A driven state belongs to an entry this render shows, and to one of its parts.
  for (const [id, states] of drivenStates) {
    const entry = expected.find((each) => each.id === id);
    if (!entry) {
      problem(`drives states of ${id}, which this render doesn't show`);
      continue;
    }
    const declared = new Set(
      Object.values(entry.presence ?? {}).flatMap((presence) =>
        "states" in presence ? presence.states : [],
      ),
    );
    for (const state of states) {
      if (!declared.has(state)) problem(`drives ${id}'s state "${state}", which no part declares`);
    }
  }
  return findings;
}

/** The overlay owner value of `opener`: the entry id for its main control, else `entry:part`. */
function overlayOwnerValue(entry: SettingsEntry, opener: string): string {
  return opener === "" ? entry.id : `${entry.id}:${opener}`;
}

function overlayOwnerSelector(entry: SettingsEntry, opener: string): string {
  return `[data-settings-overlay-owner="${CSS.escape(overlayOwnerValue(entry, opener))}"]`;
}

/** Every element matching `selector` in `roots`, a root itself included. */
function withinRoots(roots: readonly Element[], selector: string): Element[] {
  return roots.flatMap((root) => [
    ...(root.matches(selector) ? [root] : []),
    ...root.querySelectorAll(selector),
  ]);
}

/** The overlay an element sits in: the owner of the nearest one around it, itself included. */
function nearestOverlayOwner(element: Element): string | null {
  return (
    element.closest("[data-settings-overlay-owner]")?.getAttribute("data-settings-overlay-owner") ??
    null
  );
}

/** Every place `part` renders in `roots`, wherever it is. */
function partElements(roots: readonly Element[], entry: SettingsEntry, part: string): Element[] {
  return withinRoots(roots, "*").filter((element) => matchesPart(element, entry, part));
}

/**
 * Whether `part` renders where it belongs: an overlay's part in the overlay its opener opens,
 * the nearest one around it; any other part in its entry's row, or an overlay that row opens.
 */
function partInPlace(
  roots: readonly Element[],
  entry: SettingsEntry,
  part: string,
  entryFor: EntryLookup,
): boolean {
  const presence = entry.presence?.[part];
  if (presence && "overlay" in presence) {
    const owner = overlayOwnerValue(entry, presence.overlay);
    return partElements(roots, entry, part).some(
      (element) => nearestOverlayOwner(element) === owner,
    );
  }
  return partRenders(partScopes(roots, entry, entryFor), entry, part);
}

/** Where a part that isn't an overlay's belongs: its entry's row, or an overlay that row opens. */
function partScopes(
  roots: readonly Element[],
  entry: SettingsEntry,
  entryFor: EntryLookup,
): Element[] {
  return [
    ...withinRoots(roots, `[data-settings-item="${CSS.escape(entry.id)}"]`),
    ...withinRoots(roots, "[data-settings-overlay-owner]").filter(
      (overlay) => entryFor(overlay.getAttribute("data-settings-overlay-owner"))?.id === entry.id,
    ),
  ];
}

/** Whether `element` is `part`'s control. */
function matchesPart(element: Element, entry: SettingsEntry, part: string): boolean {
  const value = `${entry.id}:${part}`;
  return element.getAttribute("data-settings-control") === value;
}

/** Whether `part` renders within any of `scopes`, by the rule above. An empty wrapper doesn't. */
function partRenders(scopes: readonly Element[], entry: SettingsEntry, part: string): boolean {
  return scopes.some((scope) =>
    [scope, ...scope.querySelectorAll("*")].some((element) => matchesPart(element, entry, part)),
  );
}

/** The `entry:part` values that render in `roots`, by the same rule the audit checks parts with. */
export function renderedSettingsParts(
  roots: readonly Element[],
  entries: ReadonlyMap<string, SettingsEntry>,
): Set<string> {
  const entryFor: EntryLookup = (value) =>
    value === null
      ? undefined
      : { ...parseControlId(value), entry: entries.get(parseControlId(value).id) };
  const rendered = new Set<string>();
  for (const entry of entries.values()) {
    for (const part of Object.keys(entry.parts ?? {})) {
      if (partInPlace(roots, entry, part, entryFor)) rendered.add(`${entry.id}:${part}`);
    }
  }
  return rendered;
}

/** Entries, and parts of them, that no scenario rendered: audited nowhere. */
export function settingsCoverageProblems(
  entries: readonly SettingsEntry[],
  renderedRows: ReadonlySet<string>,
  renderedParts: ReadonlySet<string>,
): string[] {
  const problems: string[] = [];
  for (const entry of entries) {
    if (!renderedRows.has(entry.id)) problems.push(`${entry.id}: no scenario renders its row`);
    for (const part of Object.keys(entry.parts ?? {})) {
      if (!renderedParts.has(`${entry.id}:${part}`)) {
        problems.push(`${entry.id}: no scenario renders its part "${part}"`);
      }
    }
  }
  return problems;
}

/** The audits of one Settings scenario: a section, rendered in one state. */
export interface SettingsDomScenarioAudit {
  section: string;
  scenario: string;
  findings: readonly SettingsDomFinding[];
}

/** Every finding of every scenario, each under its section and scenario. */
export function domAuditProblems(audits: readonly SettingsDomScenarioAudit[]): string[] {
  return audits.flatMap(({ section, scenario, findings }) =>
    findings.map((finding) => `${section} › ${scenario}: ${finding.message}`),
  );
}

const NO_OWNER = "has no owner in the Settings catalog";

/**
 * Whether `name` is a templated part's name: one of its affixes' exact static text around a
 * runtime value that isn't blank. Which value is right is for the component's own tests.
 */
/** Controls that take a typed value: the only ones a row field may mark. */
const ROW_FIELD =
  "input:not([type=button],[type=submit],[type=reset],[type=image],[type=checkbox],[type=radio]), textarea, select, [role=combobox]";

/**
 * The region a row control sits in, as its catalog entry declares it, or why it can't be: no
 * region, an unknown entry or region, or a region outside its own entry's row.
 */
function declaredRegion(
  control: Element,
  entryFor: EntryLookup,
): { value: string; declared: SettingsRegion; element: Element } | string {
  // In the dialog a row action opened, the dialog's own declaration holds, wherever it renders.
  const dialog = control.closest(ROW_OVERLAY);
  if (dialog) {
    const value = dialog.getAttribute("data-settings-row-overlay") ?? "";
    const declared = rowOverlayDeclaration(value, entryFor);
    return typeof declared === "string" ? declared : { value, declared, element: dialog };
  }
  const element = control.closest("[data-settings-region]");
  if (!element) return "is a row control outside any data region";
  const value = element.getAttribute("data-settings-region") ?? "";
  const region = entryFor(value);
  if (!region?.entry) return `is in a data region of an entry that isn't in the catalog (${value})`;
  const declared = region.part === undefined ? undefined : region.entry.regions?.[region.part];
  if (!declared)
    return `is in "${region.part ?? ""}", which isn't a declared region of ${region.id}`;
  const regionRow = element.closest("[data-settings-item]");
  const row = regionRow?.getAttribute("data-settings-item");
  // A region may also sit in an overlay its own row opens, such as a dialog's list of choices.
  const overlay = regionRow
    ? undefined
    : entryFor(
        element
          .closest("[data-settings-overlay-owner]")
          ?.getAttribute("data-settings-overlay-owner") ?? null,
      );
  if (row !== region.id && overlay?.id !== region.id) {
    return `is in a region of ${region.id} that sits in ${row ?? overlay?.id ?? "no row"}`;
  }
  // A row of another entry nested in the region is that entry's, so it can't borrow the region.
  const controlRow = control.closest("[data-settings-item]");
  if (controlRow !== regionRow) {
    const other = controlRow?.getAttribute("data-settings-item") ?? "no row";
    return `is in ${other}'s row inside a region of ${region.id}`;
  }
  return { value, declared, element };
}

const ROW_OVERLAY = "[data-settings-row-overlay]";

/**
 * The dialog `entry:region:action` names, as that region declares it for that row action, or why
 * it isn't one: an unknown entry or region, or an action that isn't the region's or opens none.
 */
function rowOverlayDeclaration(value: string, entryFor: EntryLookup): SettingsRowOverlay | string {
  const [id = "", region = "", action = "", ...rest] = value.split(":");
  const entry = entryFor(id)?.entry;
  const declared = entry?.regions?.[region];
  const overlay =
    rest.length === 0 && declared?.rowActions?.[action] !== undefined
      ? declared.rowOverlays?.[action]
      : undefined;
  return overlay ?? `is in a row's dialog "${value}" that no region declares`;
}

/**
 * Whether `name` is one of a part's choices (the main control's when `part` is undefined): a
 * static one, or one of its counted choices around a whole number.
 */
function isPartChoice(entry: SettingsEntry, part: string | undefined, name: string): boolean {
  if (part === undefined) return entry.choices?.includes(name) ?? false;
  return (entry.partChoices?.[part] ?? []).some((choice) =>
    settingsChoiceNamed(entry, part, choice, name),
  );
}

/** A tab: a choice of the owned tab list it sits in directly, in that list's own row. */
function tabProblem(tab: Element, name: string, entryFor: EntryLookup): string | null {
  if (tab.hasAttribute("data-settings-control")) {
    return "is a tab with an owner of its own; a tab is a choice of its owned tab list";
  }
  const list = tab.parentElement?.closest(TAB_LIST);
  if (!list) return "is a tab outside any tab list";
  const owner = entryFor(list.getAttribute("data-settings-control"));
  if (!owner) return NO_OWNER;
  if (!owner.entry) return `is a tab of an entry that isn't in the catalog (${owner.id})`;
  const value = owner.part === undefined ? owner.id : `${owner.id}:${owner.part}`;
  if (tab.closest("[data-settings-item]") !== list.closest("[data-settings-item]")) {
    return `is a tab of ${value} in another row`;
  }
  return isPartChoice(owner.entry, owner.part, name)
    ? null
    : `is a tab of ${value} named "${name}", which its entry doesn't list`;
}

/** The tab group an element belongs to: its tabs root, else the nearest one holding a tab list. */
function tabGroupOf(element: Element): Element | null {
  const root = element.closest("[data-slot=tabs]");
  if (root) return root;
  let current = element.parentElement;
  while (current && !current.querySelector(TAB_LIST)) current = current.parentElement;
  return current;
}

/**
 * A tab panel isn't a control of its own: it's covered by the one owned tab that labels it,
 * which names it back with `aria-controls`, in the same tab group and row.
 */
function tabPanelProblem(panel: Element, entryFor: EntryLookup): string | null {
  if (panel.hasAttribute("data-settings-control")) {
    return "is a tab panel with an owner of its own; its tab covers it";
  }
  const page = panel.ownerDocument;
  const labelledBy = (panel.getAttribute("aria-labelledby") ?? "").split(/\s+/).filter(Boolean);
  if (labelledBy.length !== 1) return "is a tab panel not labelled by exactly one tab";
  if (!panel.id) return "is a tab panel without an id for its tab to name";
  if (page.querySelectorAll(`#${CSS.escape(panel.id)}`).length !== 1) {
    return `is a tab panel whose id "${panel.id}" isn't unique`;
  }
  const labels = page.querySelectorAll(`#${CSS.escape(labelledBy[0]!)}`);
  if (labels.length !== 1)
    return `is labelled by "${labelledBy[0]}", which isn't exactly one element`;
  const tab = labels[0]!;
  if (!tab.matches(TAB)) return "is a tab panel labelled by something that isn't a tab";
  const tabReason = tabProblem(tab, accessibleName(tab), entryFor);
  if (tabReason === NO_OWNER) return NO_OWNER;
  if (tabReason) return `is the panel of a tab that ${tabReason}`;
  if (tab.getAttribute("aria-controls") !== panel.id) {
    return "is a tab panel its tab doesn't name with aria-controls";
  }
  if (panel.closest("[data-settings-item]") !== tab.closest("[data-settings-item]")) {
    return "is a tab panel outside its tab's row";
  }
  const list = tab.parentElement!.closest(TAB_LIST)!;
  if (tabGroupOf(panel) !== tabGroupOf(list)) return "is a tab panel of another tab group";
  return null;
}

const CONTENT = "[data-settings-content]";
/** A link out of a document: an `<a>` with a scheme, such as `https:`, that's only a link. */
const CONTENT_LINK = "a[href]:not([role]), a[href][role=link]";
const HAS_SCHEME = /^[a-z][a-z\d+.-]*:/i;

/**
 * A control in a content area: one of its entry's declared areas, in that entry's own row, and
 * itself a plain link out with no owner marker.
 */
/** Why a content area isn't one of its entry's declared areas in that entry's own row, if so. */
function contentAreaProblem(content: Element, entryFor: EntryLookup): string | null {
  const value = content.getAttribute("data-settings-content") ?? "";
  const area = entryFor(value);
  if (!area?.entry || area.part === undefined || !area.entry.contentAreas?.includes(area.part)) {
    return `is in "${value}", which isn't a declared content area`;
  }
  const row = content.closest("[data-settings-item]");
  if (row?.getAttribute("data-settings-item") !== area.id) {
    return `is in a content area of ${area.id} outside its row`;
  }
  return null;
}

function contentProblem(control: Element, content: Element, entryFor: EntryLookup): string | null {
  const placement = contentAreaProblem(content, entryFor);
  if (placement) return placement;
  const area = entryFor(content.getAttribute("data-settings-content"))!;
  const row = content.closest("[data-settings-item]");
  if (control.closest("[data-settings-item]") !== row) {
    return `is in another row inside a content area of ${area.id}`;
  }
  const marked = [...control.attributes].some((attribute) =>
    attribute.name.startsWith("data-settings-"),
  );
  if (
    !control.matches(CONTENT_LINK) ||
    marked ||
    !HAS_SCHEME.test(control.getAttribute("href") ?? "")
  ) {
    return `is in a content area of ${area.id}, which holds only links out`;
  }
  return null;
}

/**
 * What may be a data-named row action: a button, a link, an element that is one by role, or the
 * <summary> that opens its <details>.
 */
const ROW_ACTION_KIND = `button, a[href], [role=button], [role=link], ${DISCLOSURE}`;
const REGION_ROW = "[data-settings-region-row]";

/**
 * A row action named by its row's data: a button or link, named, in a region row of that
 * region, and the only one of its action in that row.
 */
function dataNamedActionProblem(
  control: Element,
  action: string,
  name: string,
  region: { value: string; element: Element },
): string | null {
  if (!control.matches(ROW_ACTION_KIND)) {
    return `row action "${action}" isn't a button, link or disclosure`;
  }
  if (name.trim() === "") return `row action "${action}" has no name from its row's data`;
  const row = control.closest(REGION_ROW);
  if (!row || row.closest("[data-settings-region]") !== region.element) {
    return `row action "${action}" isn't in a row of ${region.value}`;
  }
  // Counted in this row of this region: a nested region's actions are its own.
  const same = [
    ...row.querySelectorAll(`[data-settings-row-action="${CSS.escape(action)}"]`),
  ].filter(
    (other) =>
      other.closest(REGION_ROW) === row &&
      other.closest("[data-settings-region]") === region.element,
  );
  return same.length === 1
    ? null
    : `row action "${action}" shows ${same.length} times in one row of ${region.value}`;
}

/** Whether `name` is the affix's exact static text around a value that isn't blank. */
function matchesAffix({ before, after = "" }: SettingsNameAffix, name: string): boolean {
  if (!name.startsWith(before) || !name.endsWith(after)) return false;
  if (name.length < before.length + after.length) return false;
  return name.slice(before.length, name.length - after.length).trim() !== "";
}

function matchesNameTemplate(
  entry: SettingsEntry,
  part: string | undefined,
  name: string,
): boolean {
  const template = part === undefined ? undefined : entry.nameTemplates?.[part];
  if (!template) return false;
  return [template, ...Object.values(template.variants ?? {})].some((affix) =>
    matchesAffix(affix, name),
  );
}

type EntryLookup = (
  value: string | null,
) => { id: string; part?: string; entry?: SettingsEntry } | undefined;

const RUNTIME_CHOICE = "[data-settings-runtime-choice]";
const FILE_INPUT = "[data-settings-file-input]";

/**
 * Why an element with a file-input marker isn't the hidden file input of a part in its own row;
 * undefined without a marker. It owns nothing else, so it's checked before any other owner.
 */
function fileInputProblem(element: Element, entryFor: EntryLookup): string | null | undefined {
  const marker = element.getAttribute("data-settings-file-input");
  if (marker === null) return undefined;
  if (!element.matches('input[type="file"]')) {
    return "carries a file-input marker, but isn't a file input";
  }
  if (!element.hasAttribute("hidden")) return "is a file input bound to a part, but isn't hidden";
  if (element.getAttribute("tabindex") !== "-1")
    return "is a hidden file input that can take focus";
  const owner = entryFor(marker);
  if (!owner?.entry) return `names an entry that isn't in the catalog (${owner?.id ?? marker})`;
  if (owner.part === undefined || owner.entry.parts?.[owner.part] === undefined) {
    return `is bound to "${owner.part ?? ""}", which isn't a part of ${owner.id}`;
  }
  const row = entryFor(
    element.closest("[data-settings-item]")?.getAttribute("data-settings-item") ?? null,
  );
  return row?.id === owner.id
    ? null
    : `is bound to ${marker} but sits in ${row ? row.id : "no row"}`;
}

/** What a part's overlay declares, by the overlay's owner. */
function overlayPart(owner: ReturnType<EntryLookup>) {
  return owner?.entry?.overlays?.[owner.part ?? ""];
}

/**
 * Why a marked option isn't a runtime choice of its nearest overlay: it must be an option,
 * in the overlay its marker names, of a part that declares runtime choices, with a name.
 */
function runtimeChoiceProblem(
  control: Element,
  name: string,
  marker: string,
  overlay: ReturnType<EntryLookup>,
): string | null {
  if (!control.matches(OPTION)) return "carries a runtime-choice marker, but isn't an option";
  if (!overlay) return `is a runtime choice of ${marker} outside any overlay`;
  const overlayId = overlay.part === undefined ? overlay.id : `${overlay.id}:${overlay.part}`;
  if (overlayId !== marker) {
    return `is a runtime choice of ${marker}, but sits in an overlay of ${overlayId}`;
  }
  if (!overlayPart(overlay)?.runtimeChoices) {
    return `is a runtime choice, but ${overlayId}'s overlay doesn't declare runtime choices`;
  }
  return name.trim() === "" ? "is a runtime choice with no name" : null;
}

/**
 * The runtime-choice contract for one element, before any other way to be owned: a marked
 * element must be a runtime choice of its nearest overlay, and any other option of an
 * overlay of runtime choices one of its fixed names. Undefined when the contract doesn't apply.
 */
function runtimeOptionProblem(
  element: Element,
  name: string,
  entryFor: EntryLookup,
): string | null | undefined {
  const overlay = entryFor(
    element.closest("[data-settings-overlay-owner]")?.getAttribute("data-settings-overlay-owner") ??
      null,
  );
  const marker = element.getAttribute("data-settings-runtime-choice");
  if (marker !== null) return runtimeChoiceProblem(element, name, marker, overlay);
  const runtimeChoices = overlayPart(overlay)?.runtimeChoices;
  if (!runtimeChoices || !element.matches(OPTION)) return undefined;
  return runtimeChoices.fixed?.includes(name)
    ? null
    : `is an option of ${overlay!.id}:${overlay!.part ?? ""} that isn't marked as a runtime choice or listed as fixed`;
}

/**
 * Whether a help button named "About <part name>" is for a part of its own row: one whose
 * control renders, and is owned, in that same nearest row.
 */
function isPartHelp(control: Element, name: string, entryFor: EntryLookup): boolean {
  const rowElement = control.closest("[data-settings-item]");
  const row = entryFor(rowElement?.getAttribute("data-settings-item") ?? null);
  if (!rowElement || !row?.entry || !control.matches("button")) return false;
  const entry = row.entry;
  // Each part's name, and the sub-label a part (or the main control) shows when it differs.
  // The main control's sub-label is keyed "", which is also its control id's part.
  const labels: [string, string][] = [
    ...Object.entries(entry.parts ?? {}),
    ...Object.entries(entry.partLabels ?? {}),
  ];
  return labels.some(([part, label]) => {
    if (name !== `About ${label}`) return false;
    const selector = `[data-settings-control="${CSS.escape(settingsControlId(entry, part))}"]`;
    return [...rowElement.querySelectorAll(selector)].some(
      (partControl) =>
        partControl.matches(INTERACTIVE) &&
        partControl.closest("[data-settings-item]") === rowElement &&
        ownershipProblem(partControl, entryFor) === null,
    );
  });
}

/**
 * Whether `control` is the help button of a row's field label: "About <label>" in a region row
 * whose own field is named by its declared affix around that label.
 */
function isRowFieldHelp(control: Element, name: string, entryFor: EntryLookup): boolean {
  const prefix = "About ";
  const row = control.closest(REGION_ROW);
  if (!row || !control.matches("button") || !name.startsWith(prefix)) return false;
  const region = declaredRegion(control, entryFor);
  if (typeof region === "string") return false;
  const label = name.slice(prefix.length);
  const owned = (field: Element) =>
    field.closest(REGION_ROW) === row && ownershipProblem(field, entryFor) === null;
  return [...row.querySelectorAll("[data-settings-row-field]")].some((candidate) => {
    const affix =
      region.declared.rowFields?.[candidate.getAttribute("data-settings-row-field") ?? ""];
    return (
      affix !== undefined &&
      accessibleName(candidate) === `${affix.before}${label}${affix.after ?? ""}` &&
      owned(candidate)
    );
  });
}

function ownershipProblem(control: Element, entryFor: EntryLookup): string | null {
  const name = accessibleName(control);
  const overlay = entryFor(
    control.closest("[data-settings-overlay-owner]")?.getAttribute("data-settings-overlay-owner") ??
      null,
  );
  const row = entryFor(
    control.closest("[data-settings-item]")?.getAttribute("data-settings-item") ?? null,
  );
  // Checked before any other way to be owned, so a misplaced marker can't pass as one of them.
  const fileInput = fileInputProblem(control, entryFor);
  if (fileInput) return fileInput;
  const runtimeOption = runtimeOptionProblem(control, name, entryFor);
  if (runtimeOption) return runtimeOption;
  // Inside a content area, only its text's own links, whatever else they're marked or valid as.
  const content = control.closest(CONTENT);
  if (content) return contentProblem(control, content, entryFor);
  if (fileInput === null || runtimeOption === null) return null;
  // A tab's and a panel's structure is checked before any owner, so a marker can't excuse it.
  if (control.matches(TAB)) return tabProblem(control, name, entryFor);
  if (control.matches(TAB_PANEL)) return tabPanelProblem(control, entryFor);

  const own = entryFor(control.getAttribute("data-settings-control"));
  if (own) {
    if (!own.entry) return `names an entry that isn't in the catalog (${own.id})`;
    if (own.part !== undefined && own.entry.parts?.[own.part] === undefined) {
      return `"${own.part}" isn't a part of ${own.id}`;
    }
    const wanted = controlName(own.entry, own.part);
    const alternatives = Object.values(
      (own.part === undefined ? undefined : own.entry.nameAlternatives?.[own.part]) ?? {},
    );
    if (
      name !== wanted &&
      !alternatives.includes(name) &&
      !matchesNameTemplate(own.entry, own.part, name)
    ) {
      return `is named "${name}", but ${own.id} names it "${wanted}"`;
    }
    if (overlay) {
      return overlay.id === own.id
        ? null
        : `belongs to ${own.id} but sits in an overlay of ${overlay.id}`;
    }
    if (row?.id !== own.id) {
      return `belongs to ${own.id} but sits in ${row ? row.id : "no row"}`;
    }
    return null;
  }

  if (control.matches(OPTION)) {
    const parentControl = control.parentElement?.closest("[data-settings-control]");
    const parentOwner = entryFor(parentControl?.getAttribute("data-settings-control") ?? null);
    // In a part's own control, such as its radio group, an option is one of that part's choices.
    if (parentOwner?.entry && parentOwner.part !== undefined) {
      return isPartChoice(parentOwner.entry, parentOwner.part, name)
        ? null
        : `is a choice of ${parentOwner.id}:${parentOwner.part} that its part doesn't list`;
    }
    const owner = parentOwner ?? overlay;
    // In the overlay a part opens, such as its select's list, an option is one of that part's.
    if (owner?.entry && owner.part !== undefined) {
      return isPartChoice(owner.entry, owner.part, name)
        ? null
        : `is a choice of ${owner.id}:${owner.part} that its part doesn't list`;
    }
    if (owner?.entry) {
      return owner.entry.choices?.includes(name)
        ? null
        : `is a choice of ${owner.id} that its entry doesn't list`;
    }
  }

  const rowAction = control.getAttribute("data-settings-row-action");
  const rowField = control.getAttribute("data-settings-row-field");
  if (rowAction !== null || rowField !== null) {
    const region = declaredRegion(control, entryFor);
    if (typeof region === "string") return region;
    if (rowAction !== null) {
      const wanted = region.declared.rowActions?.[rowAction];
      if (wanted === undefined)
        return `row action "${rowAction}" isn't declared for ${region.value}`;
      if (typeof wanted !== "string")
        return dataNamedActionProblem(control, rowAction, name, region);
      return name === wanted
        ? null
        : `row action "${rowAction}" is named "${name}", but ${region.value} names it "${wanted}"`;
    }
    const affix = region.declared.rowFields?.[rowField!];
    if (!affix) return `row field "${rowField}" isn't declared for ${region.value}`;
    if (!control.matches(ROW_FIELD))
      return `row field "${rowField}" marks a control that takes no value`;
    return matchesAffix(affix, name)
      ? null
      : `row field "${rowField}" is named "${name}", not ${region.value}'s "${affix.before}…${affix.after ?? ""}"`;
  }

  const dialogAction = control.getAttribute("data-settings-dialog-action");
  if (dialogAction !== null) {
    const actions = overlay?.entry?.overlays?.[overlay.part ?? ""]?.dialogActions ?? [];
    return control.matches("button") && actions.includes(dialogAction)
      ? null
      : `dialog action "${dialogAction}" isn't declared`;
  }
  // The dialog primitive's own close button dismisses whatever owns the dialog.
  if (
    (overlay?.entry || control.closest(ROW_OVERLAY)) &&
    control.matches("[data-slot=dialog-close]")
  ) {
    return null;
  }
  // The row label's help button, shown on phones.
  if (row?.entry && control.matches("button") && name === `About ${row.entry.label}`) return null;
  // A part's own label's help button, beside that part's control in the same row.
  if (isPartHelp(control, name, entryFor)) return null;
  // A row field's own label's help button, beside that field in the same region row.
  if (isRowFieldHelp(control, name, entryFor)) return null;
  return NO_OWNER;
}
