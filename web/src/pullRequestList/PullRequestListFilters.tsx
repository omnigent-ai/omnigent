// The Pull Requests list's filters beside its search: a Filter button opening the shared
// "Add filter…" menu, and its filters as chips after the state tabs. Each field the list can
// filter by is a submenu of the values its rows hold, with counts; Exclude Label leaves labels
// out, and Exclude Repository is the owner's lasting hide.

import { forwardRef, type ComponentPropsWithoutRef, type ReactNode } from "react";
import {
  EyeOffIcon,
  FolderGit2Icon,
  GitBranchIcon,
  GitPullRequestArrowIcon,
  ListFilterIcon,
  TagIcon,
  TagsIcon,
  UserCheckIcon,
  UserIcon,
  UserRoundSearchIcon,
} from "lucide-react";

import {
  FilterChip,
  FilterMenu,
  type FilterMenuCategory,
  type FilterMenuToggle,
} from "@/components/ui/filter-menu";

import { FILTER_FIELD_BY_ID, type FilterFieldId, isFilterFieldId } from "./pullRequestListFields";
import { type FilterChoice, filterValuesSummary } from "./pullRequestListFilterModel";
import {
  repositoriesSummary,
  type RepositoryChoice,
  type RepositoryExclusion,
} from "./pullRequestListPageModel";
import type { FilterChipModel, FilterFieldMenu } from "./usePullRequestListFilters";

export const EXCLUDE_REPOSITORY_CATEGORY = "exclude-repository";
const EXCLUDE_PREFIX = "exclude:";

/** A field's submenu's id, and the field and kind an id names. */
export const fieldCategoryId = (field: FilterFieldId, exclude: boolean) =>
  exclude ? `${EXCLUDE_PREFIX}${field}` : field;

export function categoryField(id: string): { field: FilterFieldId; exclude: boolean } | null {
  const exclude = id.startsWith(EXCLUDE_PREFIX);
  const field = exclude ? id.slice(EXCLUDE_PREFIX.length) : id;
  return isFilterFieldId(field) ? { field, exclude } : null;
}

const repositoryIcon = <FolderGit2Icon aria-hidden />;

const FIELD_ICONS: Record<FilterFieldId, ReactNode> = {
  repository: repositoryIcon,
  author: <UserIcon aria-hidden />,
  label: <TagIcon aria-hidden />,
  head: <GitBranchIcon aria-hidden />,
  base: <GitPullRequestArrowIcon aria-hidden />,
  assignee: <UserCheckIcon aria-hidden />,
  reviewer: <UserRoundSearchIcon aria-hidden />,
};

const EXCLUDE_ICONS: Partial<Record<FilterFieldId, ReactNode>> = {
  label: <TagsIcon aria-hidden />,
};

/** Fields that name people: two with one login on different hosts say which host. */
const PEOPLE: ReadonlySet<FilterFieldId> = new Set(["author", "assignee", "reviewer"]);

/** The host a person's key names, `provider:host/login` or `user:provider:host/login`. */
function hostOfKey(key: string): string {
  const parts = key.split(":");
  return (parts[parts.length - 1] ?? "").split("/")[0] ?? "";
}

/** Each choice's label, with its host where another of the same name is on another one. */
export function choiceLabels(field: FilterFieldId, choices: readonly FilterChoice[]): string[] {
  if (!PEOPLE.has(field)) return choices.map((choice) => choice.label);
  const seen = new Map<string, number>();
  for (const choice of choices) {
    const name = choice.label.toLowerCase();
    seen.set(name, (seen.get(name) ?? 0) + 1);
  }
  return choices.map((choice) =>
    (seen.get(choice.label.toLowerCase()) ?? 0) > 1
      ? `${choice.label} (${hostOfKey(choice.key)})`
      : choice.label,
  );
}

const plural = (count: number) => `${count} pull request${count === 1 ? "" : "s"}`;

/** The line under a field's values: rows still being read, and those that can't say. */
export function fieldMenuNote(menu: Pick<FilterFieldMenu, "loading" | "unknown">): string | null {
  const parts = [
    menu.loading > 0 ? `Details still loading for ${plural(menu.loading)}.` : null,
    menu.unknown > 0 ? `Not known for ${plural(menu.unknown)}.` : null,
  ].filter((part): part is string => part !== null);
  return parts.length > 0 ? parts.join(" ") : null;
}

/** Why a field can't be used: nothing the list read reports it. */
export const UNSUPPORTED_FIELD_REASON = "Update the desktop app to filter by this";

function categoryOf(menu: FilterFieldMenu): FilterMenuCategory {
  const field = FILTER_FIELD_BY_ID[menu.field];
  const label = menu.exclude ? `Exclude ${field.label}` : field.label;
  const icon = (menu.exclude ? EXCLUDE_ICONS[menu.field] : undefined) ?? FIELD_ICONS[menu.field];
  const labels = choiceLabels(menu.field, menu.choices);
  return {
    id: fieldCategoryId(menu.field, menu.exclude),
    label,
    icon,
    emptyLabel:
      menu.loading > 0
        ? "Still reading the pull requests…"
        : `No ${field.label.toLowerCase()} values listed yet`,
    options: menu.choices.map((choice, index) => ({
      id: choice.key,
      label: labels[index]!,
      icon: FIELD_ICONS[menu.field],
      count: choice.count,
      checked: choice.checked,
    })),
    ...(menu.unsupported ? { disabledReason: UNSUPPORTED_FIELD_REASON } : {}),
    ...(fieldMenuNote(menu) ? { note: fieldMenuNote(menu)! } : {}),
    // Typing finds values to require; excluding one takes its own submenu.
    ...(menu.exclude ? { omitFromSearch: true } : {}),
  };
}

/** Exclude Repository: what's hidden first, checked, then what the rows name. */
function exclusionCategory(
  exclusion: RepositoryExclusion,
  choices: readonly RepositoryChoice[],
): FilterMenuCategory {
  const hidden = new Set(exclusion.hidden);
  return {
    id: EXCLUDE_REPOSITORY_CATEGORY,
    label: "Exclude Repository",
    icon: <EyeOffIcon aria-hidden />,
    emptyLabel: "No repositories listed yet",
    options: [
      ...exclusion.hidden.map((key) => ({
        id: key,
        label: key,
        icon: repositoryIcon,
        // GitHub counts hidden PRs per search, not per repository, so none is shown here.
        checked: true,
      })),
      ...(exclusion.candidates ?? choices)
        .filter((choice) => !hidden.has(choice.key))
        .map((choice) => ({
          id: choice.key,
          label: choice.repository,
          icon: repositoryIcon,
          count: choice.count,
          checked: false,
        })),
    ],
  };
}

/**
 * The menu's categories: each field the list filters by, Exclude Label beside Label, then the
 * owner's Exclude Repository where it keeps one.
 */
export function filterMenuCategories({
  menus,
  exclusion,
  repositoryChoices,
}: {
  /** The fields' submenus; none where the list filters by none. */
  menus: readonly FilterFieldMenu[];
  exclusion: RepositoryExclusion | undefined;
  /** The rows' repositories, as Exclude Repository keys them. */
  repositoryChoices: readonly RepositoryChoice[];
}): FilterMenuCategory[] {
  return [
    ...menus.map(categoryOf),
    ...(exclusion ? [exclusionCategory(exclusion, repositoryChoices)] : []),
  ];
}

/** The list toolbar's Filter button, styled as Display beside it. */
const FilterTrigger = forwardRef<
  HTMLButtonElement,
  ComponentPropsWithoutRef<"button"> & { active: boolean }
>(function FilterTrigger({ active, className, ...props }, ref) {
  return (
    <button
      ref={ref}
      type="button"
      aria-label="Add Filter"
      title="Add Filter (F)"
      className={`pr-list-display-trigger${active ? " modified" : ""}${className ? ` ${className}` : ""}`}
      {...props}
    >
      <ListFilterIcon aria-hidden />
      <span>Filter</span>
    </button>
  );
});

export function PullRequestListFilterMenu({
  categories,
  onToggle,
  active,
  open,
  onOpenChange,
  field,
  onFieldClose,
}: {
  categories: readonly FilterMenuCategory[];
  onToggle: FilterMenuToggle;
  /** Some filter applies, so the button says so. */
  active: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** One field's values to open alone, from ⌘K; null for the whole menu. */
  field: string | null;
  onFieldClose: () => void;
}) {
  // A field that can't be used opens nothing.
  const shown =
    field && categories.some((category) => category.id === field && !category.disabledReason)
      ? field
      : null;
  return (
    <FilterMenu
      categories={categories}
      onToggle={onToggle}
      open={open || shown !== null}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) onFieldClose();
      }}
      shortcut="F"
      category={shown}
      trigger={<FilterTrigger active={active} />}
    />
  );
}

/** The active filters after the state tabs, each removable; nothing when none applies. */
export function PullRequestListFilterChips({
  chips,
  onToggleChip,
  onOperatorChange,
  onRemove,
  exclusion,
  exclusionCategory: excluded,
  onToggle,
  excludedMenuOpen,
  onExcludedMenuOpenChange,
}: {
  chips: readonly FilterChipModel[];
  onToggleChip: (chip: FilterChipModel, key: string) => void;
  onOperatorChange: (chip: FilterChipModel, operator: string) => void;
  onRemove: (chip: FilterChipModel) => void;
  exclusion: RepositoryExclusion | undefined;
  /** Exclude Repository's values, which its chip changes in place. */
  exclusionCategory: FilterMenuCategory | undefined;
  onToggle: FilterMenuToggle;
  excludedMenuOpen: boolean;
  onExcludedMenuOpenChange: (open: boolean) => void;
}) {
  const hidden = exclusion?.hidden ?? [];
  if (chips.length === 0 && hidden.length === 0) return null;
  return (
    <div className="pr-list-active-filters" role="group" aria-label="Active filters">
      {chips.map((model) => {
        const field = FILTER_FIELD_BY_ID[model.chip.field];
        const labels = choiceLabels(model.chip.field, model.choices);
        const category: FilterMenuCategory = {
          id: `chip:${model.id}`,
          label: field.label,
          icon: FIELD_ICONS[model.chip.field],
          options: model.choices.map((choice, index) => ({
            id: choice.key,
            label: labels[index]!,
            icon: FIELD_ICONS[model.chip.field],
            count: choice.count,
            checked: choice.checked,
          })),
        };
        // The chip names its values as its menu does, hosts and all.
        const checked = labels.filter((_, index) => model.choices[index]!.checked);
        const values =
          model.chip.field === "repository" && checked.length > 1
            ? repositoriesSummary(checked)
            : filterValuesSummary(model.chip.field, checked);
        return (
          <FilterChip
            key={model.id}
            field={field.label}
            icon={FIELD_ICONS[model.chip.field]}
            operator={model.operator}
            operators={model.operators}
            operatorId={model.chip.operator}
            onOperatorChange={(operator) => onOperatorChange(model, operator)}
            values={values}
            category={category}
            onToggle={(_category, key) => onToggleChip(model, key)}
            onRemove={() => onRemove(model)}
            removeLabel={`Remove ${field.label} Filter`}
          />
        );
      })}
      {excluded && exclusion && hidden.length > 0 && (
        <FilterChip
          field="Repository"
          operator={hidden.length === 1 ? "is not" : "is none of"}
          values={repositoriesSummary(hidden)}
          icon={<EyeOffIcon aria-hidden />}
          category={excluded}
          onToggle={onToggle}
          onRemove={exclusion.clear}
          removeLabel="Show All Hidden Repositories"
          open={excludedMenuOpen}
          onOpenChange={onExcludedMenuOpenChange}
        />
      )}
    </div>
  );
}
