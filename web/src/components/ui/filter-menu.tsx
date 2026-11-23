// A cascading, filterable "Add filter…" menu and the chips that show the filters it set: one
// submenu per category, each value a checkbox with an icon and a count. Typing searches every
// category's values at once, so a value is one keystroke from a filter. Built on Radix's
// dropdown menu with the shared type-to-filter field, as the app's other filtered menus are.

import * as React from "react";
import * as DropdownMenuPrimitive from "radix-ui/dropdown-menu";
import { CheckIcon, ListFilterIcon, XIcon } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  foldMenuText,
  MENU_FILTER_TARGET_PROPS,
  type MenuFilter,
  MenuFilterField,
  useMenuFilter,
} from "@/components/ui/menu-filter";
import { cn } from "@/lib/utils";

/** One value to check, such as a repository. */
export interface FilterMenuOption {
  id: string;
  label: string;
  icon?: React.ReactNode;
  /** How many items it matches; omitted where nothing is counted. */
  count?: number;
  checked: boolean;
}

/** A submenu of values, such as "Repository" or "Exclude Repository". */
export interface FilterMenuCategory {
  id: string;
  label: string;
  icon?: React.ReactNode;
  options: readonly FilterMenuOption[];
  /** Said in place of the values when there are none to pick. */
  emptyLabel?: string;
  /** Why the category can't be used here; its submenu shows disabled, with this as its reason. */
  disabledReason?: string;
  /** A quiet line under the values, such as how many items can't say yet. */
  note?: string;
  /** Left out of the search across categories, as a second list of the same values is. */
  omitFromSearch?: boolean;
}

export { foldMenuText };

export type FilterMenuToggle = (categoryId: string, optionId: string) => void;

/** Past this many values, a submenu or chip menu gets its own search field. */
const SEARCHABLE_OPTIONS = 8;

/** Whether `text` contains `query`, ignoring case and accents. */
export function filterMenuMatches(text: string, query: string): boolean {
  return foldMenuText(text).includes(foldMenuText(query.trim()));
}

/** One Escape closes a filter menu, a query and all, and focus goes back to its trigger. */
const FILTER_MENU_SEARCH = { escapeClearsQuery: false };

/** Sizes the menus over the shared dropdown surface's defaults. */
const SURFACE = "min-w-56 max-w-80 p-1.5";
const SUBMENU_SURFACE = cn(
  SURFACE,
  "max-h-(--radix-dropdown-menu-content-available-height) overflow-x-hidden overflow-y-auto",
);
const ITEM = cn(
  "relative flex min-w-0 cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-ui outline-hidden select-none",
  "data-highlighted:bg-muted dark:data-highlighted:bg-muted/50 data-disabled:pointer-events-none data-disabled:opacity-50",
  "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4 [&_svg:not([class*='text-'])]:text-muted-foreground",
);

function OptionItem({
  option,
  onToggle,
  prefix,
  marked = false,
}: {
  option: FilterMenuOption;
  onToggle: () => void;
  /** The category, before the value, where values from several are listed together. */
  prefix?: string;
  /** The value Enter in the search field checks. */
  marked?: boolean;
}) {
  return (
    <DropdownMenuPrimitive.CheckboxItem
      {...(marked ? MENU_FILTER_TARGET_PROPS : {})}
      className={cn(ITEM, marked && MENU_FILTER_TARGET_PROPS.className)}
      checked={option.checked}
      onCheckedChange={onToggle}
      // Checking keeps the menu open, so several can be picked.
      onSelect={(event) => event.preventDefault()}
      textValue={prefix ? `${prefix} ${option.label}` : option.label}
      aria-label={[prefix, option.label].filter(Boolean).join(" ")}
    >
      <span
        aria-hidden
        className={cn(
          "flex size-3.5 shrink-0 items-center justify-center rounded-[4px] border",
          option.checked
            ? "border-primary bg-primary text-primary-foreground"
            : "border-muted-foreground/50",
        )}
      >
        <DropdownMenuPrimitive.ItemIndicator>
          <CheckIcon className="size-3 text-primary-foreground" strokeWidth={3} />
        </DropdownMenuPrimitive.ItemIndicator>
      </span>
      {option.icon}
      <span className="min-w-0 flex-1 truncate">
        {prefix && <span className="mr-1.5 text-muted-foreground">{prefix}</span>}
        {option.label}
      </span>
      {option.count !== undefined && (
        <span
          className={cn(
            "shrink-0 pl-3 text-xs tabular-nums text-muted-foreground",
            option.count === 0 && "opacity-50",
          )}
        >
          {option.count}
        </span>
      )}
    </DropdownMenuPrimitive.CheckboxItem>
  );
}

/** Whether Enter in `filter`'s field would check the first value shown, so it reads as highlighted. */
function marksFirst(filter: MenuFilter | null): boolean {
  return filter !== null && filter.fieldFocused && filter.query.trim() !== "";
}

function EmptyLine({ children }: { children: React.ReactNode }) {
  return (
    <div role="status" className="px-1.5 py-1.5 text-ui text-muted-foreground">
      {children}
    </div>
  );
}

/** A category's values, narrowed by `filter`'s query where the menu has a search field. */
function CategoryList({
  category,
  onToggle,
  filter,
}: {
  category: FilterMenuCategory;
  onToggle: FilterMenuToggle;
  filter: MenuFilter | null;
}) {
  const query = filter?.query.trim() ?? "";
  const shown = query
    ? category.options.filter((option) => filterMenuMatches(option.label, query))
    : category.options;
  const marking = marksFirst(filter);
  return (
    <>
      {shown.map((option, index) => (
        <OptionItem
          key={option.id}
          option={option}
          marked={marking && index === 0}
          onToggle={() => onToggle(category.id, option.id)}
        />
      ))}
      {category.options.length === 0 ? (
        <EmptyLine>{category.emptyLabel ?? "Nothing to filter by yet"}</EmptyLine>
      ) : (
        shown.length === 0 && <EmptyLine>No matches for “{query}”</EmptyLine>
      )}
      {category.note && (
        <div className="mt-1 border-t border-border px-1.5 pt-1.5 pb-0.5 text-xs text-muted-foreground">
          {category.note}
        </div>
      )}
    </>
  );
}

/** A category's search field and values, on a surface whose content spreads `filter`. */
function CategoryBody({
  category,
  onToggle,
  filter,
}: {
  category: FilterMenuCategory;
  onToggle: FilterMenuToggle;
  filter: MenuFilter | null;
}) {
  return (
    <>
      {filter && (
        <MenuFilterField
          filter={filter}
          role="searchbox"
          label={`Search ${category.label}`}
          placeholder={`Search ${category.label.toLowerCase()}…`}
        />
      )}
      <CategoryList category={category} onToggle={onToggle} filter={filter} />
    </>
  );
}

function CategorySubmenu({
  category,
  onToggle,
}: {
  category: FilterMenuCategory;
  onToggle: FilterMenuToggle;
}) {
  const filter = useMenuFilter(FILTER_MENU_SEARCH);
  const searchable = category.options.length > SEARCHABLE_OPTIONS;
  const checked = category.options.filter((option) => option.checked).length;
  if (category.disabledReason) {
    // Nothing to open: the item says why, as a disabled command does.
    return (
      <DropdownMenuPrimitive.Item
        className={ITEM}
        disabled
        textValue={category.label}
        title={category.disabledReason}
      >
        {category.icon}
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate">{category.label}</span>
          <span className="truncate text-xs text-muted-foreground">{category.disabledReason}</span>
        </span>
      </DropdownMenuPrimitive.Item>
    );
  }
  return (
    <DropdownMenuSub>
      <DropdownMenuSubTrigger textValue={category.label}>
        {category.icon}
        <span className="min-w-0 flex-1 truncate">{category.label}</span>
        {checked > 0 && (
          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{checked}</span>
        )}
      </DropdownMenuSubTrigger>
      {/* Without a field, the submenu keeps Radix's own typeahead. */}
      <DropdownMenuSubContent
        {...(searchable ? filter.contentProps : {})}
        sideOffset={4}
        alignOffset={-6}
        className={SUBMENU_SURFACE}
      >
        <CategoryBody category={category} onToggle={onToggle} filter={searchable ? filter : null} />
      </DropdownMenuSubContent>
    </DropdownMenuSub>
  );
}

/** Every category's searchable values that match `query`, each with its category. */
function searchMatches(categories: readonly FilterMenuCategory[], query: string) {
  return categories.flatMap((category) =>
    (category.omitFromSearch || category.disabledReason ? [] : category.options)
      .filter((option) => filterMenuMatches(`${category.label} ${option.label}`, query))
      .map((option) => ({ category, option })),
  );
}

/**
 * The "Add filter…" menu. `trigger` is the button that opens it, a ref-forwarding element
 * Radix's trigger renders as its child; `shortcut` names the key the caller binds to open it.
 */
export function FilterMenu({
  categories,
  onToggle,
  open,
  onOpenChange,
  trigger,
  placeholder = "Add filter…",
  shortcut,
  category: only,
}: {
  categories: readonly FilterMenuCategory[];
  onToggle: FilterMenuToggle;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  trigger: React.ReactElement<Record<string, unknown>>;
  placeholder?: string;
  shortcut?: string;
  /**
   * Opens on one category's values alone, by id, such as a command for one field: the same
   * menu on the same trigger, its search taking focus, and focus going back to the trigger.
   */
  category?: string | null;
}) {
  const filter = useMenuFilter(FILTER_MENU_SEARCH);
  const shown = only ? categories.find((candidate) => candidate.id === only) : undefined;
  const query = filter.query.trim();
  const matches = query && !shown ? searchMatches(categories, query) : [];
  const marking = marksFirst(filter);
  return (
    // Not modal: the chips and list behind change as values are checked, and stay readable.
    <DropdownMenu open={open} onOpenChange={onOpenChange} modal={false}>
      <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
      <DropdownMenuContent
        {...filter.contentProps}
        sideOffset={6}
        collisionPadding={8}
        className={SURFACE}
      >
        {shown ? (
          <CategoryBody category={shown} onToggle={onToggle} filter={filter} />
        ) : (
          <>
            <MenuFilterField
              filter={filter}
              role="searchbox"
              label="Filter by"
              placeholder={placeholder}
              shortcut={shortcut}
            />
            {query ? (
              <>
                {matches.map(({ category, option }, index) => (
                  <OptionItem
                    key={`${category.id}:${option.id}`}
                    option={option}
                    prefix={category.label}
                    marked={marking && index === 0}
                    onToggle={() => onToggle(category.id, option.id)}
                  />
                ))}
                {matches.length === 0 && <EmptyLine>No filters match “{query}”</EmptyLine>}
              </>
            ) : (
              categories.map((category) => (
                <CategorySubmenu key={category.id} category={category} onToggle={onToggle} />
              ))
            )}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** One category's values, as a chip's value opens them, so they change in place. */
export function FilterOptionsMenu({
  category,
  onToggle,
  open,
  onOpenChange,
  trigger,
}: {
  category: FilterMenuCategory;
  onToggle: FilterMenuToggle;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  trigger: React.ReactElement<Record<string, unknown>>;
}) {
  const filter = useMenuFilter(FILTER_MENU_SEARCH);
  const searchable = category.options.length > SEARCHABLE_OPTIONS;
  return (
    // Not modal: the chips and list behind change as values are checked, and stay readable.
    <DropdownMenu open={open} onOpenChange={onOpenChange} modal={false}>
      <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
      <DropdownMenuContent
        {...(searchable ? filter.contentProps : {})}
        sideOffset={6}
        collisionPadding={8}
        className={SURFACE}
      >
        <CategoryBody category={category} onToggle={onToggle} filter={searchable ? filter : null} />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** A chip's values, which open its category's values to change in place. */
interface FilterChipValues {
  operator: string;
  /**
   * How else the chip can combine its values, with `operatorId` the one it uses: given, its
   * operator opens a menu to pick another, as "is" / "is not".
   */
  operators?: readonly { id: string; label: string }[];
  operatorId?: string;
  onOperatorChange?: (id: string) => void;
  /** What the chip's values say, e.g. "acme/app" or "3 repositories". */
  values: string;
  category: FilterMenuCategory;
  onToggle: FilterMenuToggle;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

type FilterChipProps = {
  /** The filter's field, or the whole label for a chip without values, e.g. "Owned by you". */
  field: string;
  icon?: React.ReactNode;
  onRemove: () => void;
  removeLabel: string;
  /** Shows × only while the chip is hovered or holds focus; it stays tabbable and keeps its room. */
  removeOnHover?: boolean;
} & (FilterChipValues | { [Key in keyof FilterChipValues]?: never });

/**
 * An active filter: its field, how it applies and its values, which open its category's values
 * to change in place, and × to remove it. Without values, just its label and ×.
 */
export function FilterChip(props: FilterChipProps) {
  const { field, icon, onRemove, removeLabel, removeOnHover = false } = props;
  const withValues = props.category !== undefined ? props : null;
  return (
    <span
      data-slot="filter-chip"
      className="group/filter-chip inline-flex h-6 max-w-full min-w-0 items-center overflow-hidden rounded-md border border-border bg-background text-xs text-foreground"
    >
      <span
        className={cn(
          "flex min-w-0 items-center gap-1 pr-1 pl-1.5 [&_svg]:size-3.5 [&_svg]:shrink-0",
          withValues ? "shrink-0 text-muted-foreground" : "[&_svg]:text-muted-foreground",
        )}
      >
        {icon}
        <span className="truncate">{field}</span>
      </span>
      {withValues && (
        <>
          {withValues.operators &&
          withValues.operators.length > 1 &&
          withValues.onOperatorChange ? (
            <ChipOperatorMenu
              field={field}
              operator={withValues.operator}
              operators={withValues.operators}
              current={withValues.operatorId}
              onChange={withValues.onOperatorChange}
            />
          ) : (
            <span className="shrink-0 border-l border-border px-1.5 text-muted-foreground">
              {withValues.operator}
            </span>
          )}
          <FilterOptionsMenu
            category={withValues.category}
            onToggle={withValues.onToggle}
            open={withValues.open}
            onOpenChange={withValues.onOpenChange}
            trigger={
              <button
                type="button"
                aria-label={`${field} ${withValues.operator} ${withValues.values}`}
                className="h-full min-w-0 truncate border-l border-border px-1.5 hover:bg-muted focus-visible:bg-muted focus-visible:outline-hidden"
              >
                {withValues.values}
              </button>
            }
          />
        </>
      )}
      <button
        type="button"
        aria-label={removeLabel}
        title={removeLabel}
        onClick={onRemove}
        className={cn(
          "flex h-full shrink-0 items-center border-l border-border px-1 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:bg-muted focus-visible:outline-hidden",
          removeOnHover &&
            "opacity-0 transition-opacity group-focus-within/filter-chip:opacity-100 group-hover/filter-chip:opacity-100",
        )}
      >
        <XIcon className="size-3" aria-hidden />
      </button>
    </span>
  );
}

/** A chip's operator, which opens the other ways it can combine its values. */
function ChipOperatorMenu({
  field,
  operator,
  operators,
  current,
  onChange,
}: {
  field: string;
  operator: string;
  operators: readonly { id: string; label: string }[];
  current: string | undefined;
  onChange: (id: string) => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`${field} ${operator}, change how it matches`}
          className="h-full shrink-0 border-l border-border px-1.5 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:bg-muted focus-visible:outline-hidden data-[state=open]:bg-muted"
        >
          {operator}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent sideOffset={6} collisionPadding={8} className="min-w-36 p-1.5">
        <DropdownMenuPrimitive.RadioGroup value={current} onValueChange={onChange}>
          {operators.map((option) => (
            <DropdownMenuPrimitive.RadioItem key={option.id} value={option.id} className={ITEM}>
              <span aria-hidden className="flex size-3.5 shrink-0 items-center justify-center">
                <DropdownMenuPrimitive.ItemIndicator>
                  <CheckIcon className="size-3.5" />
                </DropdownMenuPrimitive.ItemIndicator>
              </span>
              <span className="min-w-0 flex-1 truncate">{option.label}</span>
            </DropdownMenuPrimitive.RadioItem>
          ))}
        </DropdownMenuPrimitive.RadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** The usual trigger: a quiet "Filter" button, icon-only where `compact`. */
export const FilterMenuButton = React.forwardRef<
  HTMLButtonElement,
  React.ComponentPropsWithoutRef<"button"> & { active?: boolean; compact?: boolean }
>(function FilterMenuButton({ active, compact, className, ...props }, ref) {
  return (
    <button
      ref={ref}
      type="button"
      aria-label="Add Filter"
      className={cn(
        "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-border px-2 text-ui text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-hidden data-[state=open]:bg-muted",
        active && "border-primary/40 text-foreground",
        compact && "w-7 justify-center px-0",
        className,
      )}
      {...props}
    >
      <ListFilterIcon className="size-4" aria-hidden />
      {!compact && <span>Filter</span>}
    </button>
  );
});
