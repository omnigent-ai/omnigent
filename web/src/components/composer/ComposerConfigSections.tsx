import { type KeyboardEvent, type ReactNode, useLayoutEffect, useRef, useState } from "react";
import { ChevronRightIcon } from "lucide-react";
import {
  DropdownMenuCheckboxItem,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { type NativeModelLabelFields, nativeModelLabel } from "@/lib/composerModelLabel";
import {
  MODEL_CATALOG_BROWSE_MIN,
  groupByModelCreator,
  matchesModelSearch,
  modelCreator,
} from "@/lib/modelCreators";
import { cn } from "@/lib/utils";
import { PickerSectionHeader } from "./HarnessMenuRow";

/** One checkbox row in a Models or Effort section. */
export interface ComposerConfigChoice {
  key: string;
  label: ReactNode;
  checked: boolean;
  disabled?: boolean;
  // Omitted for a static, non-selectable row (e.g. the disabled "(current)"
  // model). When present it drives the checkbox's change handler.
  onSelect?: () => void;
  testId?: string;
  title?: string;
  className?: string;
  // Extra data-* attributes (e.g. data-model-id / data-effort-level).
  data?: Record<string, string | undefined>;
  // The catalog row a model choice selects. Long catalogs search and group by
  // its creator; rows without one (Default, "(current)") stay ungrouped.
  modelOption?: NativeModelLabelFields;
}

/** A single labeled section (Models or Effort) of the harness config menu. */
export interface ComposerConfigSection {
  testId: string;
  header: ReactNode;
  // Rendered between the header and the choices, e.g. a provider label or a
  // loading/empty note. Page-local because it varies per surface.
  leading?: ReactNode;
  choices: ComposerConfigChoice[];
}

function ConfigChoices({ choices }: { choices: readonly ComposerConfigChoice[] }) {
  return (
    <>
      {choices.map((choice) => (
        <DropdownMenuCheckboxItem
          key={choice.key}
          checked={choice.checked}
          disabled={choice.disabled}
          onSelect={(event) => event.preventDefault()}
          onCheckedChange={choice.onSelect ? () => choice.onSelect?.() : undefined}
          data-testid={choice.testId}
          title={choice.title}
          className={choice.className}
          {...choice.data}
        >
          {choice.label}
        </DropdownMenuCheckboxItem>
      ))}
    </>
  );
}

type ModelChoice = ComposerConfigChoice & { modelOption: NativeModelLabelFields };

const choiceLabel = (choice: ModelChoice) =>
  typeof choice.label === "string" ? choice.label : nativeModelLabel(choice.modelOption);

const enabledMenuItems = (root: HTMLElement | null) =>
  Array.from(root?.querySelectorAll<HTMLElement>('[role^="menuitem"]:not([data-disabled])') ?? []);

/** Append the serving host (`amazon-bedrock` in `amazon-bedrock/...`) to rows that share a label. */
function withHostsForSharedLabels(choices: readonly ComposerConfigChoice[]) {
  const labelCounts = new Map<string, number>();
  for (const choice of choices) {
    if (!choice.modelOption) continue;
    const label = choiceLabel(choice as ModelChoice);
    labelCounts.set(label, (labelCounts.get(label) ?? 0) + 1);
  }
  return choices.map((choice) => {
    const id = choice.modelOption?.id;
    if (!id?.includes("/") || labelCounts.get(choiceLabel(choice as ModelChoice))! < 2) {
      return choice;
    }
    return {
      ...choice,
      label: (
        <span className="flex min-w-0 items-baseline gap-1.5">
          <span className="min-w-0">{choice.label}</span>{" "}
          <span className="shrink-0 text-xs text-muted-foreground">{id.split("/")[0]}</span>
        </span>
      ),
    };
  });
}

function GroupHeading({ label, count }: { label: string; count: number }) {
  return (
    <>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      <span className="text-xs font-normal text-muted-foreground tabular-nums">{count}</span>
    </>
  );
}

/**
 * Model rows with search and collapsible creator groups once the catalog is
 * long. The selected model's group starts expanded and scrolled into view; a
 * search shows every match regardless of collapse state. Same-named rows from
 * different hosts show the host so they stay distinguishable.
 */
function ModelCatalogChoices({
  testId,
  choices,
}: {
  testId: string;
  choices: readonly ComposerConfigChoice[];
}) {
  const [query, setQuery] = useState("");
  // Creator ids the user has expanded; null until the first toggle so the
  // selected group follows a catalog that loads after the menu opens.
  const [expanded, setExpanded] = useState<ReadonlySet<string> | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const revealedSelection = useRef(false);
  useLayoutEffect(() => {
    if (revealedSelection.current) return;
    const selected = listRef.current?.querySelector<HTMLElement>('[aria-checked="true"]');
    if (!selected) return;
    revealedSelection.current = true;
    const reveal = () => selected.scrollIntoView?.({ block: "nearest" });
    reveal();
    // The menu's height cap applies only once Radix has positioned it.
    requestAnimationFrame(() => requestAnimationFrame(reveal));
  });
  useLayoutEffect(() => {
    // A list scrolled while browsing could otherwise hide the first matches.
    if (!query.trim()) return;
    listRef.current
      ?.querySelector<HTMLElement>('[role^="menuitem"], [data-model-group-heading]')
      ?.scrollIntoView?.({ block: "nearest" });
  }, [query]);
  const displayed = withHostsForSharedLabels(choices);
  const rows = displayed.filter((choice): choice is ModelChoice => !!choice.modelOption);
  if (rows.length < MODEL_CATALOG_BROWSE_MIN) return <ConfigChoices choices={displayed} />;
  const firstModel = displayed.findIndex((choice) => choice.modelOption);
  const leadingRows = displayed.slice(0, firstModel);
  const trailingRows = displayed.slice(firstModel).filter((choice) => !choice.modelOption);
  const term = query.trim();
  const matches = term
    ? rows.filter((row) => matchesModelSearch(row.modelOption, choiceLabel(row), term))
    : rows;
  const grouped = groupByModelCreator(rows, (row) => row.modelOption).length > 1;
  const open =
    expanded ??
    new Set(rows.filter((row) => row.checked).map((row) => modelCreator(row.modelOption).id));
  const toggle = (creatorId: string) => {
    const next = new Set(open);
    if (!next.delete(creatorId)) next.add(creatorId);
    setExpanded(next);
  };
  const onSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    // Typed characters must not reach the menu's typeahead; Tab stays trapped.
    if (event.key !== "Tab") event.stopPropagation();
    if (event.key === "ArrowDown") {
      event.preventDefault();
      enabledMenuItems(listRef.current)[0]?.focus();
    }
  };
  return (
    <>
      {/* Pinned over the menu's top padding; rows keep a matching scroll margin. */}
      <div className="sticky -top-2 z-10 bg-popover py-1">
        <Input
          ref={searchRef}
          aria-label="Search models"
          placeholder="Search models…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onSearchKeyDown}
          data-testid={`${testId}-search`}
        />
      </div>
      <div
        ref={listRef}
        className="[&_*]:scroll-mt-10"
        onKeyDownCapture={(event) => {
          if (event.key !== "ArrowUp" || event.target !== enabledMenuItems(listRef.current)[0])
            return;
          event.preventDefault();
          event.stopPropagation();
          searchRef.current?.focus();
        }}
      >
        <ConfigChoices choices={leadingRows} />
        {grouped ? (
          groupByModelCreator(matches, (row) => row.modelOption).map(({ creator, items }) => {
            const expandedGroup = term !== "" || open.has(creator.id);
            return (
              <div
                key={creator.id}
                role="group"
                aria-label={creator.label}
                data-testid={`${testId}-group-${creator.id}`}
              >
                {term ? (
                  <div
                    data-model-group-heading=""
                    className="flex min-h-8 items-center gap-1.5 px-2 text-xs leading-5 text-muted-foreground"
                  >
                    <GroupHeading label={creator.label} count={items.length} />
                  </div>
                ) : (
                  <DropdownMenuItem
                    aria-expanded={expandedGroup}
                    textValue={creator.label}
                    onSelect={(event) => {
                      event.preventDefault();
                      toggle(creator.id);
                    }}
                    className="min-h-8 text-13 font-medium"
                  >
                    <ChevronRightIcon
                      aria-hidden="true"
                      className={cn("size-3.5 transition-transform", expandedGroup && "rotate-90")}
                    />
                    <GroupHeading label={creator.label} count={items.length} />
                  </DropdownMenuItem>
                )}
                {expandedGroup && (
                  // The chevron plus the menu-item gap, so row names line up with the label.
                  <div className="pl-5.5">
                    <ConfigChoices choices={items} />
                  </div>
                )}
              </div>
            );
          })
        ) : (
          <ConfigChoices choices={matches} />
        )}
        {term && matches.length === 0 && (
          <div className="px-2 py-1 text-xs text-muted-foreground" role="status">
            No matching models
          </div>
        )}
        <ConfigChoices choices={trailingRows} />
      </div>
    </>
  );
}

/**
 * The shared Models + Effort menu sections rendered inside both harness pickers
 * — the in-session composer (ChatPage) and the landing dialog (NewChatDialog).
 *
 * Each page supplies the option data, labels, callbacks, and any loading slot;
 * the section structure (header, separator, checkbox rows, model search and
 * creator groups) lives here so the surfaces render the same composed menu
 * instead of drifting into separate page-local copies. Pass a section as
 * undefined to omit it; `browseModels={false}` renders the model rows flat.
 */
export function ComposerConfigSections({
  sdk,
  models,
  efforts,
  extra,
  browseModels = true,
}: {
  sdk?: ComposerConfigSection;
  models?: ComposerConfigSection;
  efforts?: ComposerConfigSection;
  // Additional sections rendered after Models/Effort — e.g. Devin Fusion's
  // Lead / Effort / Sidekick selectors. Each gets its own separator + header.
  extra?: readonly ComposerConfigSection[];
  browseModels?: boolean;
}) {
  const sections = [
    ...(sdk ? [sdk] : []),
    ...(models ? [models] : []),
    ...(efforts ? [efforts] : []),
    ...(extra ?? []),
  ];
  return (
    <>
      {sections.map((section, index) => (
        <div key={section.testId} data-testid={section.testId}>
          {index > 0 && <DropdownMenuSeparator />}
          <PickerSectionHeader>{section.header}</PickerSectionHeader>
          {section.leading}
          {section === models && browseModels ? (
            <ModelCatalogChoices testId={section.testId} choices={section.choices} />
          ) : (
            <ConfigChoices choices={section.choices} />
          )}
        </div>
      ))}
    </>
  );
}
