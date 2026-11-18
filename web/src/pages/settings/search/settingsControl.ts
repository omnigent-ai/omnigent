// How a Settings control takes its name from its catalog entry, and how a URL names the row
// (and part) to reveal.

import { ownValue } from "@/lib/toolValue";
import { settingsSectionPath } from "@/shell/settingsNav";

import { SETTINGS_MAIN_CONTROL, type SettingsEntry } from "./catalog/types";

type PartOf<Entry extends SettingsEntry> =
  Entry["parts"] extends Readonly<Record<infer Part, string>> ? Part & string : never;

export function settingsLabelId(entry: SettingsEntry): string {
  return `settings-${entry.id}-label`;
}

export function settingsDescriptionId(entry: SettingsEntry): string {
  return `settings-${entry.id}-description`;
}

/**
 * The value of `data-settings-control`: `:part` after the entry id, where `""` names the main
 * control. With no part, the bare id names the row, not a control.
 */
export function settingsControlId(entry: SettingsEntry, part?: string): string {
  return part === undefined ? entry.id : `${entry.id}:${part}`;
}

/** Reads `key` only when the record declares it, so `constructor` from a hash finds nothing. */
function declared<Value>(
  record: Readonly<Record<string, Value>> | undefined,
  key: string,
): Value | undefined {
  return record === undefined ? undefined : ownValue(record, key);
}

export interface SettingsControlProps {
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
  "aria-label"?: string;
  "data-settings-control": string;
}

/**
 * Props for a hidden file input that a part opens, such as Choose file: it isn't a control of
 * its own, so it's bound to that part and kept out of the tab order.
 */
export function settingsFileInputProps<Entry extends SettingsEntry>(
  entry: Entry,
  part: PartOf<Entry>,
): { "data-settings-file-input": string; tabIndex: -1 } {
  return { "data-settings-file-input": settingsControlId(entry, part), tabIndex: -1 };
}

/** The alternative-name keys `entry` declares for `Part`, such as `busy` on `submit`. */
type AlternativeOf<Entry extends SettingsEntry, Part> = Entry extends {
  nameAlternatives: infer Alternatives;
}
  ? Part extends keyof Alternatives
    ? keyof Alternatives[Part] & string
    : never
  : never;

/** One of a part's declared static alternative names, by key, such as `busy` for "Changing…". */
export interface SettingsAlternativeName<Key extends string = string> {
  alternative: Key;
}

/** The runtime value a templated part's name carries, and which finite variant to use. */
export interface SettingsRuntimeName {
  runtime: string;
  variant?: string;
}

/**
 * A templated part's name for `runtime`: the template's (or a variant's) static text around
 * it. A blank runtime, or an unknown variant, falls back to the part's static name.
 */
export function settingsTemplatedName(
  entry: SettingsEntry,
  part: string,
  { runtime, variant }: SettingsRuntimeName,
): string | undefined {
  const template = declared(entry.nameTemplates, part);
  const affix = variant === undefined ? template : declared(template?.variants, variant);
  if (!affix || runtime.trim() === "") return declared(entry.parts, part);
  return `${affix.before}${runtime}${affix.after ?? ""}`;
}

/**
 * Props for one of `entry`'s controls. The main control is labelled by the row's label and
 * description; a part is named by its declared name, which search also matches, or, for a
 * templated part, by its template around `name.runtime`.
 */
export function settingsControlProps<
  Entry extends SettingsEntry,
  Part extends PartOf<Entry> = PartOf<Entry>,
>(
  entry: Entry,
  part?: Part,
  name?: SettingsRuntimeName | SettingsAlternativeName<AlternativeOf<Entry, Part>>,
): SettingsControlProps {
  if (part === undefined) {
    return {
      "aria-labelledby": settingsLabelId(entry),
      "aria-describedby": settingsDescriptionId(entry),
      "data-settings-control": settingsControlId(entry, SETTINGS_MAIN_CONTROL),
    };
  }
  const label =
    name === undefined
      ? declared(entry.parts, part)
      : "alternative" in name
        ? settingsAlternativeName(entry, part, name.alternative)
        : settingsTemplatedName(entry, part, name);
  return { "aria-label": label, "data-settings-control": settingsControlId(entry, part) };
}

/** A part's declared alternative name; a key the part doesn't declare is a programming error. */
function settingsAlternativeName(entry: SettingsEntry, part: string, key: string): string {
  const alternative = declared(declared(entry.nameAlternatives, part), key);
  if (alternative === undefined) {
    throw new Error(`"${key}" is not a declared alternative name of ${entry.id}:${part}`);
  }
  return alternative;
}

/**
 * The part a search result for `part` reveals: the part itself, or the control that leads to
 * it (an overlay's opener, or an explicit `reachedBy`). The main control is `""`.
 */
export function settingsRevealPart(entry: SettingsEntry, part?: string): string | undefined {
  if (part === undefined) return undefined;
  const presence = declared(entry.presence, part);
  if (presence && "overlay" in presence) return presence.overlay;
  return declared(entry.reachedBy, part) ?? part;
}

/**
 * Where `entry` lives, opened so its row (or `part`) is revealed. `#id` reveals the row and
 * focuses its default control; `#id:` (part `""`) names its main control; `#id:part` a part.
 */
export function settingsItemPath(entry: SettingsEntry, part?: string): string {
  const page =
    entry.harness === undefined
      ? settingsSectionPath(entry.section)
      : `${settingsSectionPath(entry.section)}/${encodeURIComponent(entry.harness)}`;
  // Each segment is encoded alone, so an encoded separator can't be mistaken for one.
  const id = encodeURIComponent(entry.id);
  return part === undefined ? `${page}#${id}` : `${page}#${id}:${encodeURIComponent(part)}`;
}

export interface SettingsRevealTarget {
  id: string;
  part?: string;
}

/** The row and part a location hash asks to reveal, if any. */
export function parseSettingsHash(hash: string): SettingsRevealTarget | null {
  const value = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!value) return null;
  // Split on the raw separator first, then decode each segment.
  const separator = value.indexOf(":");
  const id = decodeSegment(separator === -1 ? value : value.slice(0, separator));
  if (!id) return null;
  if (separator === -1) return { id };
  const part = decodeSegment(value.slice(separator + 1));
  // An empty part (`#id:`) is the row's main control, not the row.
  return part === null ? null : { id, part };
}

function decodeSegment(segment: string): string | null {
  try {
    return decodeURIComponent(segment);
  } catch {
    return null;
  }
}
