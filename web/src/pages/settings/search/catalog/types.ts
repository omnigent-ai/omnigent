// One searchable Settings row. Settings renders a row's label, description and controls'
// names from its entry, and search reads the same entries, so a row can't exist unsearchable.

import type { ShortcutId } from "@/lib/shortcuts/registry";
import type { SettingsSectionId } from "@/shell/settingsNav";

/** A dialog or popover that one of an entry's parts opens. */
export interface SettingsOverlayPart {
  /** The buttons inside it that only confirm or dismiss it, such as "Cancel". */
  dialogActions?: readonly string[];
  /**
   * Its options are named from runtime data, such as installed apps, and each is marked with
   * `settingsRuntimeChoiceProps`; `fixed` names its static ones. Neither is searchable.
   */
  runtimeChoices?: { readonly fixed?: readonly string[] };
  /**
   * A dialog that keeps the opener it opened from, such as one that installs what removes its
   * opener. Only an origin the page audit proved from that opener, before it opened, may stand
   * in for the opener afterwards; this flag alone proves nothing.
   */
  captured?: true;
}

/** As an opener, the row's main control: the one its label names. */
export const SETTINGS_MAIN_CONTROL = "";

/**
 * When a part shows, for one that isn't always on the page: in some of its row's states (static
 * names such as "saved", never a value), or only inside the overlay `overlay` opens.
 */
export type SettingsPartPresence =
  | { readonly states: readonly string[] }
  | { readonly overlay: string }
  // Inside that overlay, and only in these states, as a form's fields while it's editing.
  | { readonly overlay: string; readonly states: readonly string[] };

/** Static text around a runtime value, such as "Copy " before a command. */
export interface SettingsNameAffix {
  readonly before: string;
  readonly after?: string;
}

/**
 * How a part whose name carries a runtime value (an app, a command) is named: `before`,
 * the value, `after`, or one of its finite `variants`, such as "Copied " for "Copy ".
 * Search reads only the part's static name; the value never reaches the index.
 */
export interface SettingsNameTemplate extends SettingsNameAffix {
  readonly variants?: Readonly<Record<string, SettingsNameAffix>>;
}

/**
 * A choice named around a count, such as "MCP servers · 2": `before`, a whole number, `after`.
 * The count is never searchable; only the part's static `partChoices` are.
 */
export interface SettingsChoiceTemplate {
  readonly before: string;
  readonly after?: string;
  readonly count: true;
}

/** A row action named by its row's data, which is never searchable. */
export interface SettingsDataNamedAction {
  readonly dataName: true;
}

/** Another entry's tab list part, and the static choice of it that leads to this entry. */
export interface SettingsRevealedByChoice {
  readonly entry: string;
  readonly part: string;
  readonly choice: string;
}

/** The entry that reveals `entry` while it's hidden, and the choice of its part, if one. */
export function settingsRevealer(
  entry: SettingsEntry,
): { id: string; part?: string; choice?: string } | undefined {
  const revealedBy = entry.revealedBy;
  if (revealedBy === undefined) return undefined;
  return typeof revealedBy === "string"
    ? { id: revealedBy }
    : { id: revealedBy.entry, part: revealedBy.part, choice: revealedBy.choice };
}

/** A whole number as a count shows it: no sign, no fraction, no leading zero. */
const WHOLE_COUNT = /^(0|[1-9]\d*)$/;

/** Whether `name` is the static `choice` of `entry`'s `part`, or that choice around a count. */
export function settingsChoiceNamed(
  entry: SettingsEntry,
  part: string,
  choice: string,
  name: string,
): boolean {
  if (name === choice) return true;
  const template = entry.choiceTemplates?.[part]?.[choice];
  if (!template) return false;
  const { before, after = "" } = template;
  if (!name.startsWith(before) || !name.endsWith(after)) return false;
  if (name.length < before.length + after.length) return false;
  return WHOLE_COUNT.test(name.slice(before.length, name.length - after.length));
}

/**
 * What's wrong with entries' structured `revealedBy`: a revealer that isn't in `entries`, is in
 * another section, or a part or choice it doesn't declare.
 */
export function settingsRevealedByProblems(entries: readonly SettingsEntry[]): string[] {
  const byId = new Map(entries.map((entry) => [entry.id, entry]));
  const problems: string[] = [];
  for (const entry of entries) {
    const revealer = settingsRevealer(entry);
    if (!revealer) continue;
    if (revealer.id === entry.id) {
      problems.push(`${entry.id} is revealed by itself`);
      continue;
    }
    const target = byId.get(revealer.id);
    if (!target) {
      problems.push(`${entry.id} is revealed by ${revealer.id}, which isn't an entry`);
      continue;
    }
    if (target.section !== entry.section) {
      problems.push(`${entry.id} is revealed by ${revealer.id}, in another section`);
    }
    if (revealer.part === undefined) continue;
    if (target.parts?.[revealer.part] === undefined) {
      problems.push(
        `${entry.id} is revealed by ${revealer.id}:${revealer.part}, which isn't a part`,
      );
    } else if (!target.partChoices?.[revealer.part]?.includes(revealer.choice!)) {
      problems.push(
        `${entry.id} is revealed by "${revealer.choice}", which isn't a static choice of ${revealer.id}:${revealer.part}`,
      );
    }
  }
  return problems;
}

/**
 * A list of user data inside a row, such as a server's services: its rows' own actions, by
 * their exact accessible names, and fixed fields named around a row's runtime value. None of it
 * is searchable, and only the one row template its region wraps may use it.
 */
export interface SettingsRegion {
  /**
   * By exact name, or `{ dataName: true }` for an action named by its row's own data, such as a
   * server's name. A data-named action sits once in each row marked `data-settings-region-row`.
   */
  readonly rowActions?: Readonly<Record<string, string | SettingsDataNamedAction>>;
  readonly rowFields?: Readonly<Record<string, SettingsNameAffix>>;
  /**
   * The dialog a row action opens for its row, such as a confirmation, by that action: its own
   * actions and fields, declared and marked as a row's are, and as unsearchable.
   */
  readonly rowOverlays?: Readonly<Record<string, SettingsRowOverlay>>;
}

/** A dialog a region's row action opens: what it shows is its row's data, never searchable. */
export interface SettingsRowOverlay {
  /** By exact name. */
  readonly rowActions?: Readonly<Record<string, string>>;
  readonly rowFields?: Readonly<Record<string, SettingsNameAffix>>;
}

/**
 * What decides whether a row shows on this build, server and account. Each field is a fact the
 * Settings page already reads for its own rows, so search and the page agree.
 */
export interface SettingsAvailability {
  /** The desktop shell offers ⇧⌘Space for New Agent from anywhere. */
  globalShortcut: boolean;
  /** `floating_composer` is on and the shell can change the New Agent panel's shortcut. */
  floatingComposerShortcut: boolean;
  /** The desktop shell can widen the window when the right sidebar opens. */
  widenWindowForRightSidebar: boolean;
  /** The shell has an in-app browser for chat links. */
  inAppBrowser: boolean;
  /** An ordinary desktop browser, which can hand a conversation to the desktop app. */
  desktopAppHandoff: boolean;
  /** Settings shows the Admin view switch: to an admin-eligible viewer, where the server offers it. */
  adminView: boolean;
  /** The server signs people in with its own accounts, which have a password here. */
  accountsEnabled: boolean;
  /** The server offers deleting a session's worktree when it's archived. */
  archiveWorktreeCleanup: boolean;
  /** The server lists managed MCP services in `enabled_connections`. */
  mcpServices: boolean;
  /** Which sandbox account connections the server lists in `enabled_connections`. */
  integrationProviders: { github: boolean; databricks: boolean };
  /** The server turns on project kanban boards. */
  projectKanban: boolean;
  /** The server lets people write their own session title instructions. */
  sessionTitleInstructions: boolean;
  /** The server offers the proactive agent. */
  proactiveAgent: boolean;
}

export interface SettingsEntry {
  /** Stable, and the URL hash that reveals the row: `#<id>`, or `#<id>:<part>` for a part. */
  id: string;
  section: SettingsSectionId;
  /** The row's name, as Settings shows it and as its main control is named. */
  label: string;
  /** Plain text, searchable; a row may render a richer description beside it. */
  description: string;
  keywords?: readonly string[];
  /** The other controls of this one setting, each by its accessible name, which is searchable. */
  parts?: Readonly<Record<string, string>>;
  /** Static value names of the main control, such as System, Light and Dark; searchable. */
  choices?: readonly string[];
  /** Static value names of a part's control, such as a tab list's tabs, by part; searchable. */
  partChoices?: Readonly<Record<string, readonly string[]>>;
  /**
   * A part's choices named around a count, by part and then by the static choice each counts,
   * such as "Plugins" for "Plugins · 2"; not searchable.
   */
  choiceTemplates?: Readonly<Record<string, Readonly<Record<string, SettingsChoiceTemplate>>>>;
  /**
   * A part's finite static names besides its own, by key, such as "Changing…" while a button is
   * busy. Only that part may show them; they aren't searchable.
   */
  nameAlternatives?: Readonly<Record<string, Readonly<Record<string, string>>>>;
  /** Parts named with a runtime value, keyed by part; the rest are named by `parts` alone. */
  nameTemplates?: Readonly<Record<string, SettingsNameTemplate>>;
  /** Parts that show only in some states or inside an overlay, keyed by part; the rest always show. */
  presence?: Readonly<Record<string, SettingsPartPresence>>;
  /**
   * Parts search reveals through another control of the row, such as a form field through the
   * control that opens the form. An overlay's parts are revealed through their overlay's opener.
   */
  reachedBy?: Readonly<Record<string, string>>;
  /**
   * A part's visible sub-label where it differs from the control's name, such as "Contrast"
   * beside a slider named "Theme contrast", keyed by part ("" for the main control). Only its
   * help button reads this; it names no control and isn't searchable.
   */
  partLabels?: Readonly<Record<string, string>>;
  /** Lists of user data inside the row, by region name. */
  regions?: Readonly<Record<string, SettingsRegion>>;
  /** Text inside the row that isn't Settings, such as a document, whose only controls are links. */
  contentAreas?: readonly string[];
  /** Overlays the row's parts open, keyed by part ("" for the main control). */
  overlays?: Readonly<Record<string, SettingsOverlayPart>>;
  /** The part that takes focus when the row is revealed; the first control otherwise. */
  focus?: string;
  /**
   * A visible entry to reveal instead while the current state hides this one: by id, or one of
   * its tab list's static choices, such as a page's Plugins tab for a plugin's own page.
   */
  revealedBy?: string | SettingsRevealedByChoice;
  /** The harness details page the row lives on: /settings/harnesses/<harness>. */
  harness?: string;
  /** For a keyboard shortcut row, the registry binding it lists. */
  shortcutId?: ShortcutId;
  /** Whether the row shows on this build, server and account; a row without it always shows. */
  available?: (availability: SettingsAvailability) => boolean;
}

type DeclaredPart<Entry> = Entry extends { parts: infer Parts } ? keyof Parts & string : never;
type PresenceKey<Entry> = Entry extends { presence: infer Presence } ? keyof Presence : never;
type ReachedKey<Entry> = Entry extends { reachedBy: infer Reached } ? keyof Reached : never;
type TemplateKey<Entry> = Entry extends { nameTemplates: infer Templates }
  ? keyof Templates
  : never;
type OverlayKey<Entry> = Entry extends { overlays: infer Overlays } ? keyof Overlays : never;
type PartLabelKey<Entry> = Entry extends { partLabels: infer Labels } ? keyof Labels : never;
type AlternativeKey<Entry> = Entry extends { nameAlternatives: infer Alternatives }
  ? keyof Alternatives
  : never;
type PartChoiceKey<Entry> = Entry extends { partChoices: infer Choices } ? keyof Choices : never;
type ChoiceTemplateKey<Entry> = Entry extends { choiceTemplates: infer Templates }
  ? keyof Templates
  : never;
/** An opener: the main control, or a part that always shows and isn't redirected itself. */
type Opener<Entry, Self> =
  | typeof SETTINGS_MAIN_CONTROL
  | Exclude<DeclaredPart<Entry>, Self | PresenceKey<Entry> | ReachedKey<Entry>>;
/** A part shown only inside an overlay, which can't be the origin of a captured dialog. */
type OverlayPresenceKey<Entry> = Entry extends { presence: infer Presence }
  ? {
      [Key in keyof Presence]: Presence[Key] extends { overlay: string } ? Key : never;
    }[keyof Presence]
  : never;
/** A part whose overlay is captured: it may show only in some states, as its dialog outlives it. */
type CapturedOpener<Entry, Self> = Entry extends { overlays: infer Overlays }
  ? Exclude<
      {
        [Key in keyof Overlays]: Overlays[Key] extends { captured: true } ? Key : never;
      }[keyof Overlays] &
        DeclaredPart<Entry>,
      Self | ReachedKey<Entry> | OverlayPresenceKey<Entry>
    >
  : never;

/** `presence` and `reachedBy` name only the entry's own parts, and point only at real openers. */
interface CheckedEntry<Entry> {
  partChoices?: {
    [Part in PartChoiceKey<Entry>]: Part extends DeclaredPart<Entry> ? readonly string[] : never;
  };
  choiceTemplates?: {
    [Part in ChoiceTemplateKey<Entry>]: Part extends DeclaredPart<Entry>
      ? Readonly<Record<string, SettingsChoiceTemplate>>
      : never;
  };
  nameAlternatives?: {
    [Part in AlternativeKey<Entry>]: Part extends DeclaredPart<Entry>
      ? Readonly<Record<string, string>>
      : never;
  };
  partLabels?: {
    [Part in PartLabelKey<Entry>]: Part extends DeclaredPart<Entry> | typeof SETTINGS_MAIN_CONTROL
      ? string
      : never;
  };
  overlays?: {
    [Part in OverlayKey<Entry>]: Part extends DeclaredPart<Entry> | typeof SETTINGS_MAIN_CONTROL
      ? SettingsOverlayPart
      : never;
  };
  nameTemplates?: {
    [Part in TemplateKey<Entry>]: Part extends DeclaredPart<Entry> ? SettingsNameTemplate : never;
  };
  presence?: {
    [Part in PresenceKey<Entry>]: Part extends DeclaredPart<Entry>
      ? // A states-only presence has no overlay, so an overlay's opener is always checked.
        | { readonly states: readonly string[]; readonly overlay?: never }
        | { readonly overlay: Opener<Entry, Part> | CapturedOpener<Entry, Part> }
        | {
            readonly overlay: Opener<Entry, Part> | CapturedOpener<Entry, Part>;
            readonly states: readonly string[];
          }
      : never;
  };
  reachedBy?: {
    [Part in ReachedKey<Entry>]: Part extends DeclaredPart<Entry>
      ? // An overlay's part is already revealed through its overlay's opener.
        Entry extends { presence: Record<Part, { overlay: string }> }
        ? never
        : Opener<Entry, Part>
      : never;
  };
}

/** Declares an entry; the identity keeps its literal type for the catalog's checks. */
export function settingsEntry<const Entry extends SettingsEntry>(
  entry: Entry & CheckedEntry<Entry>,
): Entry {
  return entry;
}

/**
 * The families `settingsEntryFamily` made, and their subsets. A copy or a look-alike object
 * isn't one of them, whatever it carries, so it can't stand in for a family.
 */
const CREATED_FAMILIES = new WeakSet<object>();

/** Which member of a family each of its entries was built for. */
const ENTRY_MEMBERS = new WeakMap<SettingsEntry, string>();

/** The family member `entry` was built for, or undefined for an entry outside any family. */
export function settingsEntryMember(entry: SettingsEntry): string | undefined {
  return ENTRY_MEMBERS.get(entry);
}

/**
 * One catalog entry per member of a fixed list, such as one per harness the app knows, built
 * the same way. `entry(member)` finds a member's entry, and throws for anything else.
 */
export interface SettingsEntryFamily<Member extends string, Entry extends SettingsEntry> {
  readonly key: string;
  readonly members: readonly Member[];
  readonly entries: readonly Entry[];
  readonly entry: (member: Member) => Entry;
  /** The same entries for some members only, such as the harnesses the server can install. */
  readonly subset: <const Subset extends Member>(
    key: string,
    members: readonly Subset[],
  ) => SettingsEntryFamily<Subset, Entry>;
}

function familyOf<Member extends string, Entry extends SettingsEntry>(
  key: string,
  byMember: ReadonlyMap<Member, Entry>,
): SettingsEntryFamily<Member, Entry> {
  const members = [...byMember.keys()];
  for (const [member, entry] of byMember) ENTRY_MEMBERS.set(entry, member);
  const family: SettingsEntryFamily<Member, Entry> = {
    key,
    members,
    entries: [...byMember.values()],
    entry(member) {
      const entry = byMember.get(member);
      if (!entry) throw new Error(`${member} isn't a member of the ${key} Settings family`);
      return entry;
    },
    subset(subsetKey, subsetMembers) {
      const missing = subsetMembers.filter((member) => !byMember.has(member));
      if (missing.length > 0) {
        throw new Error(`${subsetKey} names ${missing.join(", ")}, which ${key} doesn't have`);
      }
      return familyOf(
        subsetKey,
        new Map(subsetMembers.map((member) => [member, byMember.get(member)!])),
      );
    },
  };
  CREATED_FAMILIES.add(Object.freeze(family));
  return family;
}

/** Declares a family of entries, one per member, each checked like a `settingsEntry`. */
export function settingsEntryFamily<
  const Member extends string,
  const Entry extends SettingsEntry,
>({
  key,
  members,
  build,
}: {
  key: string;
  members: readonly Member[];
  build: (member: Member) => Entry & CheckedEntry<Entry>;
}): SettingsEntryFamily<Member, Entry> {
  if (new Set(members).size !== members.length) {
    throw new Error(`The ${key} Settings family lists a member twice`);
  }
  return familyOf(key, new Map(members.map((member) => [member, build(member)])));
}

/** Whether `value` is itself a family `settingsEntryFamily` made, or one of its subsets. */
export function isSettingsEntryFamily(
  value: unknown,
): value is SettingsEntryFamily<string, SettingsEntry> {
  return typeof value === "object" && value !== null && CREATED_FAMILIES.has(value);
}

/**
 * What's wrong with an entry's conditional parts, for entries built without `settingsEntry`:
 * a presence or redirect for a part it doesn't declare, or an opener that isn't the main control
 * or one of its always-shown, unredirected parts.
 */
export function settingsEntryProblems(entry: SettingsEntry): string[] {
  const parts = new Set(Object.keys(entry.parts ?? {}));
  const conditional = new Set([
    ...Object.keys(entry.presence ?? {}),
    ...Object.keys(entry.reachedBy ?? {}),
  ]);
  const problems: string[] = [];
  if (entry.id.trim() === "" || entry.id.includes(":")) {
    problems.push(`"${entry.id}" isn't a usable id: it must be non-blank and contain no ":"`);
  }
  // A captured dialog may open from a part that shows only in some states: it keeps its origin.
  // That opener is still a part shown on the page, not one inside an overlay or reached another way.
  const capturedOpener = (opener: string) => {
    const presence = entry.presence?.[opener];
    return (
      entry.overlays?.[opener]?.captured === true &&
      entry.reachedBy?.[opener] === undefined &&
      (presence === undefined || !("overlay" in presence))
    );
  };
  // Only an overlay may open from a captured origin; a redirect needs an opener that always shows.
  const checkOpener = (part: string, opener: string, allowCaptured: boolean) => {
    if (opener === SETTINGS_MAIN_CONTROL) return;
    if (opener === part) problems.push(`${entry.id}:${part} is its own opener`);
    else if (!parts.has(opener))
      problems.push(`${entry.id}:${part}'s opener "${opener}" isn't a part`);
    else if (conditional.has(opener) && !(allowCaptured && capturedOpener(opener))) {
      problems.push(`${entry.id}:${part}'s opener "${opener}" doesn't always show`);
    }
  };
  for (const [part, presence] of Object.entries(entry.presence ?? {})) {
    if (!parts.has(part))
      problems.push(`${entry.id} gives a presence to "${part}", which isn't a part`);
    if ("overlay" in presence) checkOpener(part, presence.overlay, true);
    if ("states" in presence && presence.states.length === 0) {
      problems.push(`${entry.id}:${part} shows in no state`);
    }
  }
  const meaningful = (text: string) => text.trim() !== "";
  for (const [part, template] of Object.entries(entry.nameTemplates ?? {})) {
    if (!parts.has(part))
      problems.push(`${entry.id} names "${part}" by a template, but it isn't a part`);
    for (const affix of [template, ...Object.values(template.variants ?? {})]) {
      if (!meaningful(`${affix.before}${affix.after ?? ""}`)) {
        problems.push(`${entry.id}:${part}'s name template has no static text around its value`);
      }
    }
  }
  for (const [part, alternatives] of Object.entries(entry.nameAlternatives ?? {})) {
    if (!parts.has(part))
      problems.push(`${entry.id} gives other names to "${part}", which isn't a part`);
    for (const [key, alternative] of Object.entries(alternatives)) {
      if (!meaningful(alternative)) problems.push(`${entry.id}:${part}'s "${key}" name is blank`);
    }
  }
  const rowNameProblems = (
    owner: string,
    declared: {
      rowActions?: SettingsRegion["rowActions"];
      rowFields?: SettingsRegion["rowFields"];
    },
  ) => {
    for (const [action, actionName] of Object.entries(declared.rowActions ?? {})) {
      const named = typeof actionName === "string" ? meaningful(actionName) : actionName.dataName;
      if (!named) problems.push(`${owner}'s row action "${action}" has no name`);
    }
    for (const [field, affix] of Object.entries(declared.rowFields ?? {})) {
      if (!meaningful(`${affix.before}${affix.after ?? ""}`)) {
        problems.push(`${owner}'s row field "${field}" has no static text`);
      }
    }
  };
  for (const [region, declared] of Object.entries(entry.regions ?? {})) {
    rowNameProblems(`${entry.id}:${region}`, declared);
    for (const [action, overlay] of Object.entries(declared.rowOverlays ?? {})) {
      if (declared.rowActions?.[action] === undefined) {
        problems.push(
          `${entry.id}:${region} gives a dialog to "${action}", which isn't a row action`,
        );
      }
      rowNameProblems(`${entry.id}:${region}:${action}`, overlay);
    }
  }
  if (entry.choices?.some((choice) => !meaningful(choice))) {
    problems.push(`${entry.id} lists a blank choice`);
  }
  for (const [part, choices] of Object.entries(entry.partChoices ?? {})) {
    if (!parts.has(part))
      problems.push(`${entry.id} gives choices to "${part}", which isn't a part`);
    if (choices.some((choice) => !meaningful(choice))) {
      problems.push(`${entry.id}:${part} lists a blank choice`);
    }
  }
  for (const [part, templates] of Object.entries(entry.choiceTemplates ?? {})) {
    if (!parts.has(part)) {
      problems.push(`${entry.id} gives counted choices to "${part}", which isn't a part`);
    }
    for (const [key, template] of Object.entries(templates)) {
      if (!entry.partChoices?.[part]?.includes(key)) {
        problems.push(`${entry.id}:${part} counts "${key}", which isn't one of its static choices`);
      }
      if (!meaningful(`${template.before}${template.after ?? ""}`)) {
        problems.push(`${entry.id}:${part}'s counted choice "${key}" has no static text`);
      }
    }
  }
  const areas = entry.contentAreas ?? [];
  if (areas.some((area) => !meaningful(area)) || new Set(areas).size !== areas.length) {
    problems.push(`${entry.id}'s content areas must be named, each once`);
  }
  for (const [part, label] of Object.entries(entry.partLabels ?? {})) {
    if (part !== SETTINGS_MAIN_CONTROL && !parts.has(part)) {
      problems.push(`${entry.id} labels "${part}", which isn't a part`);
    }
    if (!meaningful(label)) problems.push(`${entry.id}:${part}'s label is blank`);
  }
  for (const [part, overlay] of Object.entries(entry.overlays ?? {})) {
    if (part !== SETTINGS_MAIN_CONTROL && !parts.has(part)) {
      problems.push(`${entry.id} gives an overlay to "${part}", which isn't a part`);
    }
    if (overlay.runtimeChoices?.fixed?.some((name) => !meaningful(name))) {
      problems.push(`${entry.id}:${part}'s overlay lists a blank fixed choice`);
    }
  }
  for (const [part, opener] of Object.entries(entry.reachedBy ?? {})) {
    if (!parts.has(part)) problems.push(`${entry.id} redirects "${part}", which isn't a part`);
    const presence = entry.presence?.[part];
    if (presence && "overlay" in presence) {
      problems.push(`${entry.id}:${part} is in an overlay, so its overlay's opener reveals it`);
    }
    checkOpener(part, opener, false);
  }
  return problems;
}
