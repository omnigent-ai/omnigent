// The session list's view choices, defined once for the sidebar's filter menu and
// ⌘K: grouping, ordering, which sessions to display, and which fields rows show.
// ⌘K offers each section as a setting showing its choice, and runs the menu's handlers.

import {
  ArrowDownWideNarrowIcon,
  EyeIcon,
  EyeOffIcon,
  LayersIcon,
  ListFilterIcon,
} from "lucide-react";

import type { SessionFilter } from "@/lib/sessionFilterPreferences";
import {
  SIDEBAR_GROUPINGS,
  SIDEBAR_ORDERINGS,
  SIDEBAR_SHOW_FIELDS,
  type SidebarGrouping,
  type SidebarOrdering,
  type SidebarShowField,
  type SidebarViewPreferences,
} from "@/lib/sidebarViewPreferences";
import type { PaletteCommand } from "@/store/paletteCommands";
import { booleanSetting, settingCommand } from "@/store/paletteSettings";

/** The filter menu's labels; grouping's also start the date sections' headings, such as "Created Today". */
export const GROUPING_LABELS: Record<SidebarGrouping, string> = {
  default: "Default",
  status: "Status",
  harness: "Harness",
  created: "Created",
  updated: "Updated",
};
export const ORDERING_LABELS: Record<SidebarOrdering, string> = {
  updated: "Updated",
  created: "Created",
  custom: "Custom",
};
export const SHOW_LABELS: Record<SidebarShowField, string> = {
  harness: "Harness",
  project: "Project",
  updated: "Updated",
  environment: "Environment",
  repo: "Repo",
  branch: "Branch",
};
export const SIDEBAR_FILTERS: readonly { value: SessionFilter; label: string }[] = [
  { value: "all", label: "All sessions" },
  { value: "mine", label: "My sessions" },
  { value: "shared", label: "Shared with me" },
  { value: "archived", label: "Archived sessions" },
];

/** ⌘K's setting names for the menu's sections, with the context its submenus give. */
export const SIDEBAR_SETTING_LABELS = {
  grouping: "Session List: Group by",
  display: "Session List: Display",
  ordering: "Session List: Sort by",
} as const;
/** The row fields as ⌘K names them, e.g. "Session List: Show Last Updated Time". */
const SHOW_FIELD_NAMES: Record<SidebarShowField, string> = {
  harness: "Harness",
  project: "Project",
  updated: "Last Updated Time",
  environment: "Environment",
  repo: "Repo",
  branch: "Branch",
};

/** The Display choices offered here: Shared only where other people's sessions exist. */
export function sidebarFilters(
  multiUser: boolean,
): readonly { value: SessionFilter; label: string }[] {
  return multiUser
    ? SIDEBAR_FILTERS
    : SIDEBAR_FILTERS.filter((filter) => filter.value !== "shared");
}

export interface SidebarViewState {
  filter: SessionFilter;
  multiUser: boolean;
  /** Null while grouping, ordering and fields aren't offered; only Display is. */
  view: SidebarViewPreferences | null;
  onFilterChange: (filter: SessionFilter) => void;
  onViewChange: (patch: Partial<SidebarViewPreferences>) => void;
}

/** One radio choice, a value of its section's setting in the menu and in ⌘K. */
export interface SidebarViewChoice<Value extends string> {
  id: string;
  value: Value;
  menuLabel: string;
  selected: boolean;
  select: () => void;
}

/** One row field the user can show or hide. */
export interface SidebarShowToggle {
  id: string;
  field: SidebarShowField;
  shown: boolean;
  menuLabel: string;
  /** ⌘K's name for the field's switch, e.g. "Session List: Show Repo". */
  settingLabel: string;
  toggle: () => void;
}

export interface SidebarViewChoices {
  /** Empty while grouping, ordering and fields aren't offered. */
  grouping: SidebarViewChoice<SidebarGrouping>[];
  display: SidebarViewChoice<SessionFilter>[];
  ordering: SidebarViewChoice<SidebarOrdering>[];
  show: SidebarShowToggle[];
}

/** The filter menu's sections and what each choice does, for the menu and ⌘K alike. */
export function sidebarViewChoices({
  filter,
  multiUser,
  view,
  onFilterChange,
  onViewChange,
}: SidebarViewState): SidebarViewChoices {
  const display = sidebarFilters(multiUser).map((choice) => ({
    id: `sidebar-filter:${choice.value}`,
    value: choice.value,
    menuLabel: choice.label,
    selected: choice.value === filter,
    select: () => onFilterChange(choice.value),
  }));
  if (!view) return { grouping: [], display, ordering: [], show: [] };
  return {
    grouping: SIDEBAR_GROUPINGS.map((grouping) => ({
      id: `sidebar-grouping:${grouping}`,
      value: grouping,
      menuLabel: GROUPING_LABELS[grouping],
      selected: grouping === view.grouping,
      select: () => onViewChange({ grouping }),
    })),
    display,
    ordering: SIDEBAR_ORDERINGS.map((ordering) => ({
      id: `sidebar-ordering:${ordering}`,
      value: ordering,
      menuLabel: ORDERING_LABELS[ordering],
      selected: ordering === view.ordering,
      select: () => onViewChange({ ordering }),
    })),
    show: SIDEBAR_SHOW_FIELDS.map((field) => {
      const shown = view.show.includes(field);
      return {
        id: `sidebar-show:${field}`,
        field,
        shown,
        menuLabel: SHOW_LABELS[field],
        settingLabel: `Session List: Show ${SHOW_FIELD_NAMES[field]}`,
        toggle: () =>
          onViewChange({
            show: SIDEBAR_SHOW_FIELDS.filter((candidate) =>
              candidate === field ? !shown : view.show.includes(candidate),
            ),
          }),
      };
    }),
  };
}

const KEYWORDS = ["sidebar", "session list", "view"];

/** One menu section as a ⌘K setting; none when the section isn't offered. */
function sectionSetting(
  id: string,
  label: string,
  section: SidebarViewChoice<string>[],
  icon: PaletteCommand["icon"],
  keywords: readonly string[],
): PaletteCommand[] {
  const selected = section.find((choice) => choice.selected);
  if (!selected) return [];
  return [
    settingCommand({
      id,
      owner: "sidebar",
      label,
      icon,
      keywords: [...KEYWORDS, ...keywords],
      current: selected.value,
      choices: section.map((choice) => ({ value: choice.value, label: choice.menuLabel })),
      choose: (value) => section.find((choice) => choice.value === value)?.select(),
    }),
  ];
}

/** ⌘K's view settings, one per menu section and row field, in menu order. */
export function sidebarViewCommands(choices: SidebarViewChoices): PaletteCommand[] {
  return [
    ...sectionSetting(
      "sidebar-grouping",
      SIDEBAR_SETTING_LABELS.grouping,
      choices.grouping,
      LayersIcon,
      ["group", "grouping"],
    ),
    ...sectionSetting(
      "sidebar-display",
      SIDEBAR_SETTING_LABELS.display,
      choices.display,
      ListFilterIcon,
      ["filter", "display", "show"],
    ),
    ...sectionSetting(
      "sidebar-ordering",
      SIDEBAR_SETTING_LABELS.ordering,
      choices.ordering,
      ArrowDownWideNarrowIcon,
      ["sort", "order", "ordering"],
    ),
    ...choices.show.map((toggle) =>
      settingCommand(
        booleanSetting({
          id: toggle.id,
          owner: "sidebar",
          label: toggle.settingLabel,
          icon: toggle.shown ? EyeIcon : EyeOffIcon,
          keywords: [...KEYWORDS, "field", "column", "row", toggle.menuLabel],
          enabled: toggle.shown,
          write: (show) => {
            if (show !== toggle.shown) toggle.toggle();
          },
        }),
      ),
    ),
  ];
}
