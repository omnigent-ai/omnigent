import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Command, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SMART_ROUTING_LABEL } from "@/lib/agentLabels";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { cn } from "@/lib/utils";
import { ChevronDownIcon } from "lucide-react";

// Sentinel Select values for the Model row. Radix requires a non-empty value,
// so the two "no explicit model" choices ride on reserved tokens rather than
// "": DEFAULT = the harness's own configured model (no override), SMART = the
// intelligent router picks per turn.
export const MODEL_SELECT_DEFAULT = "__default__";
export const MODEL_SELECT_SMART = "__smart__";
// Sentinel for the "no explicit effort" (—) choice, same reasoning.
export const EFFORT_SELECT_NONE = "__none__";
// Shown in the frozen Effort row when the router picks the model per turn, so
// no effort can apply. Rendered as the Select's placeholder (value "").
export const EFFORT_UNAVAILABLE_PLACEHOLDER = "—";

// Catalog length above which the composer agent-config model menus show a
// search field. Kept in sync with RoutingModelSelect below.
export const MODEL_MENU_SEARCH_THRESHOLD = 15;

/** One entry in the Model row's harness-model list. */
export interface RoutingModelOption {
  id: string;
  label: string;
}

/** The native-catalog fields the Model row's copy is built from. */
interface NativeModelLabelFields {
  id: string;
  displayName?: string;
  isDefault?: boolean;
}

/** A catalog row's user-facing name: what the harness advertises, else its id. */
export function nativeModelLabel(option: NativeModelLabelFields): string {
  return option.displayName ?? option.id;
}

/**
 * Label for the Model row's "Default" choice, naming the model it resolves to
 * when the catalog marks one.
 *
 * Shared by the landing dialog and the in-session composer: read from one place
 * so the same session can't read "Default" in one gear and
 * "Default (GPT-5.6-Luna)" in the other.
 *
 * @param options Harness catalog rows; at most one is marked default.
 * @returns ``Default (<name>)``, or plain ``Default`` when unmarked.
 */
export function defaultModelLabel(options: readonly NativeModelLabelFields[]): string {
  const dflt = options.find((option) => option.isDefault);
  return dflt ? `Default (${nativeModelLabel(dflt)})` : "Default";
}

/**
 * The Model row's searchable picker: the Smart Routing sentinel (when offered),
 * the harness's own "Default", then the harness's models. Uses the same
 * Popover + cmdk pattern as the landing pi picker so long catalogs are
 * searchable, while short catalogs fall back to a plain list. The composer
 * config menus render their own searchable lists; this row now serves the
 * fork dialog (and any modal surface wanting the same row), keeping the
 * sentinels and their copy from drifting.
 *
 * @param value Selected value — a model id or one of the sentinels.
 * @param onValueChange Selection callback.
 * @param offerSmartRouting Whether to list the Smart Routing sentinel. False on
 *   harnesses/sessions the router can't route.
 * @param testId Trigger test id.
 * @param ariaLabel Accessible name for the trigger (the visible ConfigRow label
 *   is visual-only, so pass it here to name the control for AT).
 * @param models Harness models, listed after "Default". Empty when the harness
 *   resolves its own catalog and only the two sentinels are expressible.
 * @param defaultLabel Label for the "no explicit model" row. Defaults to
 *   "Default"; pass the resolved form (e.g. `Default (gpt-5.5)`) where the
 *   catalog names which model that is.
 * @param activeModelId Model marked `data-active` in the list, when any.
 * @param contentClassName Extra classes for the dropdown popup.
 * @param children Rendered below the model list, e.g. a loading/empty note.
 */
export function RoutingModelSelect({
  value,
  onValueChange,
  offerSmartRouting,
  testId,
  ariaLabel = "Model",
  models,
  defaultLabel = "Default",
  activeModelId,
  contentClassName,
  triggerClassName,
  componentId,
  children,
}: {
  value: string;
  onValueChange: (value: string) => void;
  offerSmartRouting: boolean;
  testId: string;
  ariaLabel?: string;
  models?: readonly RoutingModelOption[];
  defaultLabel?: string;
  activeModelId?: string | null;
  contentClassName?: string;
  // Extra classes for the trigger, e.g. a caller that wants a smaller font.
  triggerClassName?: string;
  // Opt-in analytics id. Model values are a bounded catalog + the "smart"/
  // "default" sentinels, so the value is reported (valueHasNoPii) for pattern
  // analysis of model choice.
  componentId?: string;
  children?: ReactNode;
}) {
  const { trackValueChange } = useOmnigentAnalytics();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const modelList = models ?? [];

  const selectedLabel =
    value === MODEL_SELECT_SMART
      ? SMART_ROUTING_LABEL
      : value === MODEL_SELECT_DEFAULT
        ? defaultLabel
        : (modelList.find((m) => m.id === value)?.label ?? value);

  const select = (nextValue: string) => {
    if (componentId) {
      // valueHasNoPii assumes a bounded catalog; drop it if reused for typed values.
      trackValueChange(componentId, "select", nextValue, { valueHasNoPii: true });
    }
    onValueChange(nextValue);
    setOpen(false);
  };

  // Show the search box only for long catalogs. Claude/codex ship ~5-10
  // models, where a plain list is faster; pi exposes ~90 models and needs
  // filtering.
  const showSearch = modelList.length > MODEL_MENU_SEARCH_THRESHOLD;
  // Filtering is done here (shouldFilter={false} on the Command), not by
  // cmdk: that keeps the row order stable (cmdk reorders by match score) and
  // the empty state honest (cmdk does not count force-mounted sentinels, so
  // its own Empty fired even while they were visible).
  const filteredModels = showSearch
    ? modelList.filter((m) => modelQueryMatches(m.id, m.label, query))
    : modelList;
  const noResults = showSearch && query.trim().length > 0 && filteredModels.length === 0;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          aria-label={ariaLabel}
          data-testid={testId}
          className={cn("h-8 w-full justify-between gap-2 px-2.5 font-normal", triggerClassName)}
        >
          <span className="min-w-0 truncate">{selectedLabel}</span>
          <ChevronDownIcon className="size-4 shrink-0 text-muted-foreground" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className={cn(
          "max-h-[var(--radix-popover-content-available-height)] w-[var(--radix-popover-trigger-width)] min-w-0 overflow-hidden p-0",
          contentClassName,
        )}
      >
        <Command className="h-auto min-h-0" shouldFilter={false}>
          {showSearch && (
            <CommandInput
              placeholder="Search models…"
              data-testid={`${testId}-search`}
              value={query}
              onValueChange={setQuery}
            />
          )}
          <CommandList
            className="max-h-72 min-h-0 overflow-y-auto overscroll-contain"
            onWheel={(event) => event.stopPropagation()}
          >
            {offerSmartRouting && (
              <CommandItem
                value={MODEL_SELECT_SMART}
                data-checked={value === MODEL_SELECT_SMART ? "true" : undefined}
                onSelect={() => select(MODEL_SELECT_SMART)}
              >
                <span className="min-w-0 truncate">{SMART_ROUTING_LABEL}</span>
              </CommandItem>
            )}
            <CommandItem
              value={MODEL_SELECT_DEFAULT}
              data-checked={value === MODEL_SELECT_DEFAULT ? "true" : undefined}
              onSelect={() => select(MODEL_SELECT_DEFAULT)}
            >
              <span className="min-w-0 truncate">{defaultLabel}</span>
            </CommandItem>
            {filteredModels.map((m) => (
              <CommandItem
                key={m.id}
                value={m.id}
                title={m.label}
                data-model-id={m.id}
                data-active={activeModelId === m.id ? "true" : undefined}
                data-checked={value === m.id ? "true" : undefined}
                onSelect={() => select(m.id)}
              >
                <span className="min-w-0 truncate">{m.label}</span>
              </CommandItem>
            ))}
            {noResults && (
              <div className="px-2 py-1 text-xs text-muted-foreground">No models found</div>
            )}
            {children}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

/**
 * Whitespace-AND matcher shared by every model-search surface: the query is
 * split on whitespace and every term must appear in the display label or the
 * id (case-insensitive). One semantics everywhere, so the same query gives
 * the same results in any picker.
 */
export function modelQueryMatches(id: string, label: string, query: string): boolean {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  const idLower = id.toLowerCase();
  const labelLower = label.toLowerCase();
  return terms.every((term) => labelLower.includes(term) || idLower.includes(term));
}

/** One option in a DropdownMenu-based model catalog (composer config menus). */
export interface ModelMenuFilterOption {
  id: string;
  displayName?: string;
  label?: string;
}

/** Return value of {@link useModelMenuFilter}. */
export interface UseModelMenuFilterResult<T extends ModelMenuFilterOption> {
  query: string;
  setQuery: (query: string) => void;
  showSearch: boolean;
  filteredOptions: readonly T[];
  noResults: boolean;
  inputRef: React.RefObject<HTMLInputElement | null>;
  inputProps: Omit<React.ComponentProps<"input">, "ref" | "type"> & {
    "data-testid": "composer-agent-models-search";
    placeholder: "Search models…";
    "aria-label": "Search models";
    value: string;
    onChange: (event: React.ChangeEvent<HTMLInputElement>) => void;
    onKeyDown: (event: React.KeyboardEvent<HTMLInputElement>) => void;
  };
  focusInput: () => void;
}

/**
 * Filter state for a DropdownMenu model list. Shows a search field only for
 * long catalogs (> MODEL_MENU_SEARCH_THRESHOLD); the query is split on
 * whitespace and every term must match the option's display label or id —
 * the same semantics as the pre-launch agent menu's pi search.
 */
export function useModelMenuFilter<T extends ModelMenuFilterOption>(
  options: readonly T[],
): UseModelMenuFilterResult<T> {
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const filteredOptions = useMemo(
    () =>
      query.trim()
        ? options.filter((option) =>
            modelQueryMatches(option.id, option.displayName ?? option.label ?? option.id, query),
          )
        : options,
    // Keyed on the query string, not a derived terms array (a fresh array
    // per render would defeat the memo entirely).
    [options, query],
  );

  const showSearch = options.length > MODEL_MENU_SEARCH_THRESHOLD;
  // Not gated on showSearch: a caller may render the search field regardless
  // of catalog size (the pre-launch pi picker does). An unrendered field
  // can't carry a query, so this stays false there naturally.
  const noResults = query.trim().length > 0 && filteredOptions.length === 0;

  const focusInput = useCallback(() => {
    // Defer so the input is present in the DOM when Radix fires onOpenAutoFocus.
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  const inputProps = useMemo(
    () => ({
      "data-testid": "composer-agent-models-search" as const,
      placeholder: "Search models…" as const,
      "aria-label": "Search models" as const,
      value: query,
      onChange: (event: React.ChangeEvent<HTMLInputElement>) => setQuery(event.target.value),
      // Typing must not drive Radix's menu typeahead, but the keys that MOVE
      // focus must: ArrowDown steps into the item list (otherwise keyboard
      // users can filter but never reach a row — menus swallow Tab), and
      // Escape is let through so the menu's dismiss layer still closes it.
      onKeyDown: (event: React.KeyboardEvent<HTMLInputElement>) => {
        if (event.key === "Escape") return;
        if (event.key === "ArrowDown") {
          const menu = inputRef.current?.closest('[role="menu"]');
          const firstItem = menu?.querySelector<HTMLElement>(
            '[role="menuitemcheckbox"], [role="menuitemradio"], [role="menuitem"]',
          );
          if (firstItem) {
            event.stopPropagation();
            firstItem.focus();
            return;
          }
        }
        event.stopPropagation();
      },
    }),
    [query],
  );

  return {
    query,
    setQuery,
    showSearch,
    filteredOptions,
    noResults,
    inputRef,
    inputProps,
    focusInput,
  };
}

/** Search input rendered at the top of a model menu. */
export function ModelMenuSearch<T extends ModelMenuFilterOption>({
  filter,
}: {
  filter: UseModelMenuFilterResult<T>;
}): ReactNode {
  const { inputRef, inputProps, focusInput, showSearch } = filter;

  useEffect(() => {
    if (showSearch) focusInput();
  }, [showSearch, focusInput]);

  return <Input ref={inputRef} type="search" className="mb-1 h-8 text-xs" {...inputProps} />;
}

// Claude-native reasoning-effort options for the new-session / scheduled-task
// model+effort pickers. There is deliberately no hardcoded effort default: an
// unselected picker omits `reasoning_effort`, so Claude Code falls back to its
// own configured effort — the same "no override" semantics the in-session
// picker's `null` state uses. Mirrors ANTHROPIC_EFFORTS server-side. Lives here
// (a leaf module, no heavy imports) so both NewChatDialog and the scheduled-task
// dialog can share the single source of truth.
export const CLAUDE_NATIVE_EFFORTS: { value: string; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "xHigh" },
  { value: "max", label: "Max" },
];

/** Pi thinking level options for the new-session picker. Mirrors PI_EFFORTS server-side. */
export const PI_NATIVE_EFFORTS: { value: string; label: string }[] = [
  { value: "none", label: "None" },
  { value: "minimal", label: "Minimal" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "xHigh" },
  { value: "max", label: "Max" },
];

/**
 * A labeled configuration row: bold label + muted sub-description on the left,
 * the control on the right. Mirrors the "Configure …" modal layout.
 */
export function ConfigRow({
  label,
  description,
  children,
  controlClassName,
}: {
  label: string;
  description?: string;
  children: ReactNode;
  controlClassName?: string;
}) {
  return (
    // Stacked on mobile (label above a full-width control) so the label never
    // gets squeezed into a narrow column and wraps hard; side-by-side from sm+
    // with the control pinned to a fixed width.
    <div className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
      <div className="min-w-0 sm:pt-1">
        <div className="text-ui font-medium">{label}</div>
        {description && <div className="text-sm text-muted-foreground">{description}</div>}
      </div>
      <div className={cn("w-full sm:w-52 sm:shrink-0", controlClassName)}>{children}</div>
    </div>
  );
}

/**
 * A config-modal Select whose options carry descriptions. The description of
 * the hovered / focused option (falling back to the selected one) shows in a
 * footer line pinned at the bottom of the OPEN dropdown. The popup is pinned to
 * the trigger width and the footer wraps, so the dropdown never changes width
 * as you hover across options.
 *
 * @param value Selected option value.
 * @param onValueChange Selection callback.
 * @param options Value/label/description triples.
 * @param testId Trigger test id.
 * @param ariaLabel Accessible name for the trigger (the visible ConfigRow
 *   label is visual-only, so pass it here to name the control for AT).
 * @param disabled Locks the control: the value shows but the list can't open.
 *   Used for a row whose value isn't the user's to choose (yet).
 */
export function DescribedSelect({
  value,
  onValueChange,
  options,
  testId,
  ariaLabel,
  disabled,
  triggerClassName,
  contentClassName,
  componentId,
}: {
  value: string;
  onValueChange: (value: string) => void;
  options: readonly { value: string; label: string; description: string }[];
  testId: string;
  ariaLabel: string;
  disabled?: boolean;
  // Extra classes for the trigger, e.g. a caller that wants a smaller font.
  triggerClassName?: string;
  // Extra classes for the dropdown content, e.g. to shrink the option font.
  contentClassName?: string;
  // Opt-in analytics id. Options are a fixed enum (permission / approval modes),
  // so the selected value is reported (valueHasNoPii).
  componentId?: string;
}) {
  const [previewed, setPreviewed] = useState<string | null>(null);
  const detail = options.find((o) => o.value === (previewed ?? value))?.description;
  return (
    <Select
      value={value}
      onValueChange={onValueChange}
      componentId={componentId}
      // valueHasNoPii assumes fixed option enums; drop it if reused for free text.
      valueHasNoPii
      disabled={disabled}
      // Reset the preview when the list closes so the next open starts on the
      // selected option's blurb.
      onOpenChange={(next) => {
        if (!next) setPreviewed(null);
      }}
    >
      <SelectTrigger
        className={cn("w-full", triggerClassName)}
        data-testid={testId}
        aria-label={ariaLabel}
      >
        <SelectValue />
      </SelectTrigger>
      {/* Pin the popup to the trigger width so a long blurb wraps in the footer
      instead of widening the list as you hover across options. */}
      <SelectContent
        position="popper"
        align="start"
        className={cn(
          "w-(--radix-select-trigger-width) [&_[data-slot=select-item]]:pl-2.5",
          contentClassName,
        )}
      >
        {options.map((o) => (
          <SelectItem
            key={o.value}
            value={o.value}
            onPointerEnter={() => setPreviewed(o.value)}
            onFocus={() => setPreviewed(o.value)}
          >
            {o.label}
          </SelectItem>
        ))}
        {/* Footer blurb pinned inside the dropdown, tracking the hovered row.
        min-h reserves a line so the popup height doesn't jump as it changes. */}
        <SelectSeparator />
        <p
          data-testid={`${testId}-detail`}
          className="min-h-8 px-2.5 pt-0.5 pb-1 text-sm leading-snug text-muted-foreground"
        >
          {detail}
        </p>
      </SelectContent>
    </Select>
  );
}
