// The Pull Requests list's filters as chips: a field, how its values combine, and the values.
// Every chip applies; a row whose details aren't known for a chip's field can't be shown to
// match it, so it's left out while that chip applies, and the menu says how many are waiting.

import type { PullRequestListEntry } from "@/shell/PullRequestListRows";

import {
  FILTER_FIELD_BY_ID,
  FILTER_FIELDS,
  type FieldValues,
  type FilterFieldId,
} from "./pullRequestListFields";

/**
 * How a chip's values combine: a pull request has any of them, has all of them (only fields a
 * PR holds several of), or has none of them.
 */
export type FilterOperator = "anyOf" | "allOf" | "noneOf";

export interface ListFilter {
  /** The chip's own, kept through every edit of its values and operator. */
  id?: string;
  field: FilterFieldId;
  operator: FilterOperator;
  /** The values' keys, as the field gives them. */
  values: readonly string[];
}

let chipCount = 0;
/** A new chip's id, unique for this run. */
const newChipId = () => `chip-${(chipCount += 1)}`;

/**
 * The operators a field's chip offers. With one value, any of and all of say the same, so only
 * the one the chip uses is offered, as the reference's chips do.
 */
export function filterOperators(
  field: FilterFieldId,
  chip?: Pick<ListFilter, "operator" | "values">,
): readonly FilterOperator[] {
  if (!FILTER_FIELD_BY_ID[field].multi) return ["anyOf", "noneOf"];
  if (chip && chip.values.length === 1) {
    return [chip.operator === "allOf" ? "allOf" : "anyOf", "noneOf"];
  }
  return ["anyOf", "allOf", "noneOf"];
}

/** How a chip reads `operator` for `count` values: “is”, “is any of”, “is not”, “is all of”. */
export function operatorTitle(operator: FilterOperator, count: number): string {
  const single = count === 1;
  switch (operator) {
    case "anyOf":
      return single ? "is" : "is any of";
    case "allOf":
      return single ? "is" : "is all of";
    case "noneOf":
      return single ? "is not" : "is none of";
  }
}

/** Each field's values for `entry`, read once per entry. */
type EntryValues = Map<FilterFieldId, FieldValues>;

function valuesOf(entry: PullRequestListEntry, cache: WeakMap<object, EntryValues>): EntryValues {
  let values = cache.get(entry);
  if (!values) {
    values = new Map(FILTER_FIELDS.map((field) => [field.id, field.valuesOf(entry)]));
    cache.set(entry, values);
  }
  return values;
}

/** Whether `values` satisfy `filter`; values not known never do. */
function satisfies(values: FieldValues, filter: ListFilter): boolean {
  if (filter.values.length === 0) return true;
  if (!values.known) return false;
  const held = new Set(values.values.map((value) => value.key));
  switch (filter.operator) {
    case "anyOf":
      return filter.values.some((key) => held.has(key));
    case "allOf":
      return filter.values.every((key) => held.has(key));
    case "noneOf":
      return !filter.values.some((key) => held.has(key));
  }
}

function matchesWith(values: EntryValues, filters: readonly ListFilter[]): boolean {
  return filters.every((filter) => satisfies(values.get(filter.field)!, filter));
}

/** The entries every chip lets through, in their order. */
export function filterEntries<Entry extends PullRequestListEntry>(
  entries: readonly Entry[],
  filters: readonly ListFilter[],
): Entry[] {
  if (filters.every((filter) => filter.values.length === 0)) return [...entries];
  const cache = new WeakMap<object, EntryValues>();
  return entries.filter((entry) => matchesWith(valuesOf(entry, cache), filters));
}

/** One value a field's menu offers: how many rows have it, and whether a chip picks it. */
export interface FilterChoice {
  key: string;
  label: string;
  count: number;
  checked: boolean;
}

/** What a field's menu lists, and how many rows can't say yet, or at all. */
export interface FieldChoices {
  field: FilterFieldId;
  choices: FilterChoice[];
  /** Rows whose details for the field are still being read. */
  loading: number;
  /** Rows that won't say: offline, a failed read, or a source that doesn't report the field. */
  unknown: number;
  /** No row's source reports the field at all, such as an older desktop app's assignees. */
  unsupported: boolean;
}

/**
 * Each field's values among `entries`, with counts as the reference's menus count them: among
 * the rows the other chips let through, so a count says what checking it gives. A field's own
 * chip doesn't narrow its counts, or every other value would count 0, except a chip of a field
 * a PR holds several of that requires them: of those labelled `staging`, 3 are labelled `cli`.
 * Values are ordered by how many of all `entries` have them, which checking one doesn't change,
 * and a picked value no row has any more stays listed, with 0.
 */
export function fieldChoices(
  entries: readonly PullRequestListEntry[],
  filters: readonly ListFilter[],
  field: FilterFieldId,
): FieldChoices {
  const { multi } = FILTER_FIELD_BY_ID[field];
  const narrowing = filters.filter(
    (filter) => filter.field !== field || (multi && filter.operator !== "noneOf"),
  );
  const cache = new WeakMap<object, EntryValues>();
  const counts = new Map<string, number>();
  const order = new Map<string, number>();
  // The spellings rows give a value, so a label shows as most rows write it.
  const spellings = new Map<string, Map<string, number>>();
  let loading = 0;
  let unknown = 0;
  let reported = false;
  for (const entry of entries) {
    const values = valuesOf(entry, cache);
    const own = values.get(field)!;
    if (!own.known) {
      if (own.reason === "loading") loading += 1;
      else unknown += 1;
      if (own.reason !== "unsupported") reported = true;
      continue;
    }
    reported = true;
    const narrowed = matchesWith(values, narrowing);
    for (const value of own.values) {
      order.set(value.key, (order.get(value.key) ?? 0) + 1);
      if (narrowed) counts.set(value.key, (counts.get(value.key) ?? 0) + 1);
      const written = spellings.get(value.key) ?? new Map<string, number>();
      written.set(value.label, (written.get(value.label) ?? 0) + 1);
      spellings.set(value.key, written);
    }
  }
  const picked = new Map<string, string>();
  for (const filter of filters) {
    if (filter.field !== field) continue;
    for (const key of filter.values) picked.set(key, key);
  }
  const labelOf = (key: string) => {
    const written = spellings.get(key);
    if (!written) return FILTER_FIELD_BY_ID[field].labelOfKey(picked.get(key) ?? key);
    return [...written].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]![0];
  };
  const keys = [...new Set([...order.keys(), ...picked.keys()])].sort(
    (a, b) => (order.get(b) ?? 0) - (order.get(a) ?? 0) || labelOf(a).localeCompare(labelOf(b)),
  );
  return {
    field,
    choices: keys.map((key) => ({
      key,
      label: labelOf(key),
      count: counts.get(key) ?? 0,
      checked: picked.has(key),
    })),
    loading,
    unknown,
    unsupported: !reported && entries.length > 0,
  };
}

/**
 * `filters` with `key` checked or unchecked in `field`'s chip: the chip holding it, else the
 * field's first chip of that kind, else a new one; a chip left with no values goes. `exclude`
 * picks the kind, for a field with a submenu for each (Label, Exclude Label); left out, any of
 * the field's chips will do, whatever its operator, so one field has one chip.
 */
export function toggleFilterValue(
  filters: readonly ListFilter[],
  field: FilterFieldId,
  key: string,
  exclude?: boolean,
): ListFilter[] {
  const chips = filters.filter(
    (filter) =>
      filter.field === field &&
      (exclude === undefined || (filter.operator === "noneOf") === exclude),
  );
  const holder = chips.find((filter) => filter.values.includes(key)) ?? chips[0];
  if (!holder) {
    return [
      ...filters,
      { id: newChipId(), field, operator: exclude ? "noneOf" : "anyOf", values: [key] },
    ];
  }
  return toggleChipValue(filters, holder, key);
}

/** `key` checked or unchecked in `chip` alone, as its own menu does; an emptied chip goes. */
export function toggleChipValue(
  filters: readonly ListFilter[],
  chip: ListFilter,
  key: string,
): ListFilter[] {
  const values = chip.values.includes(key)
    ? chip.values.filter((value) => value !== key)
    : [...chip.values, key];
  return values.length === 0
    ? filters.filter((filter) => filter !== chip)
    : filters.map((filter) => (filter === chip ? { ...chip, values } : filter));
}

/** `chip` combining its values another way, where its field offers it. */
export function setFilterOperator(
  filters: readonly ListFilter[],
  chip: ListFilter,
  operator: FilterOperator,
): ListFilter[] {
  if (!filterOperators(chip.field).includes(operator)) return [...filters];
  return filters.map((filter) => (filter === chip ? { ...filter, operator } : filter));
}

export function removeFilter(filters: readonly ListFilter[], chip: ListFilter): ListFilter[] {
  return filters.filter((filter) => filter !== chip);
}

/**
 * `filters` narrowed to one value of `field`, as an author's header or a single repository
 * pick asks: the field's any-of chips give way to one holding `key`; null removes them.
 */
export function onlyFilterValue(
  filters: readonly ListFilter[],
  field: FilterFieldId,
  key: string | null,
): ListFilter[] {
  const others = filters.filter((filter) => filter.field !== field || filter.operator === "noneOf");
  return key === null
    ? others
    : [...others, { id: newChipId(), field, operator: "anyOf", values: [key] }];
}

/** The single value of `field` the chips narrow to, where one any-of chip holds exactly one. */
export function onlyValueOf(filters: readonly ListFilter[], field: FilterFieldId): string | null {
  const chips = filters.filter((filter) => filter.field === field);
  return chips.length === 1 && chips[0]!.operator === "anyOf" && chips[0]!.values.length === 1
    ? chips[0]!.values[0]!
    : null;
}

const VALUE_NOUNS: Record<FilterFieldId, string> = {
  repository: "repositories",
  author: "authors",
  label: "labels",
  head: "branches",
  base: "branches",
  assignee: "assignees",
  reviewer: "reviewers",
};

/** What a chip's values say: the one value's name, else how many, e.g. "3 labels". */
export function filterValuesSummary(field: FilterFieldId, labels: readonly string[]): string {
  return labels.length === 1 ? labels[0]! : `${labels.length} ${VALUE_NOUNS[field]}`;
}
