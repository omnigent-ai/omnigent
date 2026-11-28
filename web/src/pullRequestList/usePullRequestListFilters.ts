// The Pull Requests list's filter chips as the page holds them for this run, and what its
// Filter menu, chips and ⌘K show for them: each field's values among the view's rows, and the
// edits a menu, chip or command makes. The matching itself is the filter model's.

import { useCallback, useMemo } from "react";

import type { PullRequestListEntry } from "@/shell/PullRequestListRows";

import { FILTER_FIELDS, type FilterFieldId } from "./pullRequestListFields";
import {
  type FieldChoices,
  type FilterChoice,
  fieldChoices,
  filterEntries,
  filterOperators,
  type FilterOperator,
  type ListFilter,
  onlyFilterValue,
  onlyValueOf,
  operatorTitle,
  removeFilter,
  setFilterOperator,
  toggleChipValue,
  toggleFilterValue,
} from "./pullRequestListFilterModel";

/** One submenu of the Filter menu: a field's values to require, or to leave out. */
export interface FilterFieldMenu extends Omit<FieldChoices, "choices"> {
  /** The none-of submenu, such as Exclude Label. */
  exclude: boolean;
  choices: FilterChoice[];
}

/** One chip, as it shows and what its own menus change. */
export interface FilterChipModel {
  /** Stable while the chip lasts, for keys. */
  id: string;
  chip: ListFilter;
  operator: string;
  operators: readonly { id: FilterOperator; label: string }[];
  /** The field's values, checked where this chip has them, to change in place. */
  choices: FilterChoice[];
}

const NO_FILTERS: readonly ListFilter[] = [];

/** Fields with a second submenu that leaves their values out, as the reference does for labels. */
const EXCLUDABLE: ReadonlySet<FilterFieldId> = new Set(["label"]);

export interface PullRequestListFilters {
  filters: readonly ListFilter[];
  /** Some chip applies. */
  active: boolean;
  /** The entries every chip lets through. */
  keep: <Entry extends PullRequestListEntry>(entries: readonly Entry[]) => Entry[];
  menus: FilterFieldMenu[];
  chips: FilterChipModel[];
  toggle: (field: FilterFieldId, key: string, exclude?: boolean) => void;
  toggleChip: (chip: ListFilter, key: string) => void;
  setOperator: (chip: ListFilter, operator: FilterOperator) => void;
  remove: (chip: ListFilter) => void;
  clear: () => void;
  /** Narrow to one value of `field`, or none; and the one it narrows to now. */
  only: (field: FilterFieldId, key: string | null) => void;
  onlyValue: (field: FilterFieldId) => string | null;
}

export function usePullRequestListFilters({
  entries,
  filters = NO_FILTERS,
  update,
}: {
  /** The view's rows' entries, before any chip, the state chip or the search. */
  entries: readonly PullRequestListEntry[];
  filters: readonly ListFilter[] | undefined;
  update: (change: (current: readonly ListFilter[]) => readonly ListFilter[]) => void;
}): PullRequestListFilters {
  const menus = useMemo(
    () =>
      FILTER_FIELDS.flatMap((field) => {
        const choices = fieldChoices(entries, filters, field.id);
        // A field with one submenu checks what any of its chips holds, whatever its operator;
        // Label's two keep requiring and leaving out apart.
        const separate = EXCLUDABLE.has(field.id);
        const checkedIn = (exclude: boolean) =>
          new Set(
            filters
              .filter(
                (filter) =>
                  filter.field === field.id &&
                  (!separate || (filter.operator === "noneOf") === exclude),
              )
              .flatMap((filter) => filter.values),
          );
        const menuOf = (exclude: boolean): FilterFieldMenu => {
          const checked = checkedIn(exclude);
          return {
            ...choices,
            exclude,
            choices: choices.choices.map((choice) => ({
              ...choice,
              checked: checked.has(choice.key),
            })),
          };
        };
        return EXCLUDABLE.has(field.id) ? [menuOf(false), menuOf(true)] : [menuOf(false)];
      }),
    [entries, filters],
  );
  const chips = useMemo(
    () =>
      filters.map((chip, index): FilterChipModel => {
        const all = menus.find((menu) => menu.field === chip.field && !menu.exclude)!;
        const choices = all.choices.map((choice) => ({
          ...choice,
          checked: chip.values.includes(choice.key),
        }));
        return {
          // Kept through its own edits, so a changed chip keeps its place and focus.
          id: chip.id ?? `chip-at-${index}`,
          chip,
          operator: operatorTitle(chip.operator, chip.values.length),
          operators: filterOperators(chip.field, chip).map((operator) => ({
            id: operator,
            label: operatorTitle(operator, chip.values.length),
          })),
          choices,
        };
      }),
    [filters, menus],
  );
  const keep = useCallback(
    <Entry extends PullRequestListEntry>(shown: readonly Entry[]) => filterEntries(shown, filters),
    [filters],
  );
  const toggle = useCallback(
    (field: FilterFieldId, key: string, exclude = false) =>
      update((current) =>
        // Only Label has a submenu for each kind; another field's values go to its own chip.
        toggleFilterValue(current, field, key, EXCLUDABLE.has(field) ? exclude : undefined),
      ),
    [update],
  );
  // A chip's own edits find it again by its id: an edit since may have replaced the object.
  const byPlace = useCallback(
    (
      change: (current: readonly ListFilter[], chip: ListFilter) => ListFilter[],
      chip: ListFilter,
    ) =>
      update((current) => {
        const now = chip.id
          ? current.find((candidate) => candidate.id === chip.id)
          : current[filters.indexOf(chip)];
        return now && now.field === chip.field ? change(current, now) : [...current];
      }),
    [filters, update],
  );
  const toggleChip = useCallback(
    (chip: ListFilter, key: string) =>
      byPlace((current, now) => toggleChipValue(current, now, key), chip),
    [byPlace],
  );
  const setOperator = useCallback(
    (chip: ListFilter, operator: FilterOperator) =>
      byPlace((current, now) => setFilterOperator(current, now, operator), chip),
    [byPlace],
  );
  const remove = useCallback(
    (chip: ListFilter) => byPlace((current, now) => removeFilter(current, now), chip),
    [byPlace],
  );
  const clear = useCallback(() => update(() => NO_FILTERS), [update]);
  const only = useCallback(
    (field: FilterFieldId, key: string | null) =>
      update((current) => onlyFilterValue(current, field, key)),
    [update],
  );
  const onlyValue = useCallback((field: FilterFieldId) => onlyValueOf(filters, field), [filters]);
  // One value while nothing changes, so the list's rows aren't worked out again every render.
  return useMemo(
    () => ({
      filters,
      active: filters.some((filter) => filter.values.length > 0),
      keep,
      menus,
      chips,
      toggle,
      toggleChip,
      setOperator,
      remove,
      clear,
      only,
      onlyValue,
    }),
    [chips, clear, filters, keep, menus, only, onlyValue, remove, setOperator, toggle, toggleChip],
  );
}
