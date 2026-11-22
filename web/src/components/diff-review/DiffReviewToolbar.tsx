// The diff review's toolbar: the file tree toggle, how many files and lines
// changed, and the view controls. Each control runs a shared review command.
// A narrow review gathers the view controls into one menu, and anything that
// still doesn't fit wraps onto a second row instead of being clipped.

import { useRef, type ReactNode } from "react";
import { Loader2Icon, MoreHorizontalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { FilterChip } from "@/components/ui/filter-menu";
import { PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { type DiffReviewBase, DiffReviewBaseMenu } from "./DiffReviewBaseMenu";
import type { DiffReviewCommand, DiffReviewCommandId } from "./diffReviewCommands";
import { LanguageBreakdown } from "./LanguageBreakdown";
import {
  hasActiveFilters,
  testFileViewLabel,
  type DiffFilterAction,
  type DiffReviewFilters,
  type DiffTestFileView,
  type LineBreakdown,
} from "./diffReviewModel";
import { GENERATED_FILES_NOTICE } from "./fileClassification";

/** Commands with their own toolbar button, in order after the filter menu. */
const VIEW_BUTTONS: readonly DiffReviewCommandId[] = [
  "cycle-test-files",
  "toggle-layout",
  "toggle-whitespace",
  "toggle-wrap",
  "toggle-collapse-all",
];

export type { DiffReviewBase, DiffReviewBaseChoice } from "./DiffReviewBaseMenu";

export interface DiffReviewToolbarProps {
  commands: readonly DiffReviewCommand[];
  fileCount: number;
  base?: DiffReviewBase | null;
  filters: DiffReviewFilters;
  dispatchFilter: (action: DiffFilterAction) => void;
  languages: readonly { language: string; count: number }[];
  testCount: number;
  generatedCount: number;
  filtersOpen: boolean;
  setFiltersOpen: (open: boolean) => void;
  /** The listed files' line counts, in all and by language. */
  breakdown: LineBreakdown;
  languagesOpen: boolean;
  setLanguagesOpen: (open: boolean) => void;
  /** The review is the one on screen; a hidden one's breakdown closes without taking focus. */
  reviewActive?: boolean;
  /** The source's own controls, after the review's. */
  extra?: ReactNode;
  /**
   * The source's progress, just left of the line counts: they and everything after them stay
   * put as it comes and goes. See `DiffReviewToolbarProgress`.
   */
  status?: ReactNode;
  /** Gathers the view controls into a "More view options" menu, for a narrow review. */
  compact?: boolean;
  /** The tree opens as a popover from its toggle instead of docking beside the diff. */
  treePopover?: boolean;
  /** Shown in place of the base menu, such as Changes review's compared revisions. */
  comparison?: ReactNode;
  /** Commands that keep no toolbar button here; ⌘K still offers them. */
  omit?: readonly DiffReviewCommandId[];
}

export function DiffReviewToolbar({
  commands,
  fileCount,
  base,
  filters,
  dispatchFilter,
  languages,
  testCount,
  generatedCount,
  filtersOpen,
  setFiltersOpen,
  breakdown,
  languagesOpen,
  setLanguagesOpen,
  reviewActive = true,
  extra,
  status,
  compact = false,
  treePopover = false,
  comparison,
  omit,
}: DiffReviewToolbarProps) {
  const byId = new Map(
    commands
      .filter((command) => !omit?.includes(command.id))
      .map((command) => [command.id, command]),
  );
  const tree = byId.get("toggle-tree");
  const filter = byId.get("filter-files");
  const ownedFiles = byId.get("toggle-owned-files");
  const filterButton = useRef<HTMLButtonElement>(null);
  const viewCommands = VIEW_BUTTONS.flatMap((id) => byId.get(id) ?? []);
  return (
    <div
      role="toolbar"
      aria-label="Diff review"
      className="flex min-h-9 shrink-0 flex-wrap items-center gap-x-1 gap-y-0.5 border-b border-border px-2 py-0.5 text-ui"
    >
      {tree && <TreeToggle command={tree} popover={treePopover} />}
      <span
        className="whitespace-nowrap text-muted-foreground"
        data-testid="diff-review-file-count"
      >
        {fileCount} {fileCount === 1 ? "file" : "files"}
      </span>
      {comparison ?? (base && <DiffReviewBaseMenu base={base} />)}
      {ownedFiles?.pressed && (
        <FilterChip
          field="Owned by you"
          icon={<ownedFiles.icon aria-hidden />}
          removeLabel="Remove Owned by you filter"
          removeOnHover
          onRemove={() => {
            ownedFiles.setting?.choose("all");
            // The chip goes away with its ×, so focus stays in the toolbar.
            filterButton.current?.focus();
          }}
        />
      )}
      <span className="ml-auto flex items-center gap-2 pr-1 tabular-nums whitespace-nowrap">
        {status}
        <LanguageBreakdown
          command={byId.get("show-language-breakdown")}
          breakdown={breakdown}
          filters={filters}
          dispatchFilter={dispatchFilter}
          open={languagesOpen}
          onOpenChange={setLanguagesOpen}
          restoreFocus={reviewActive}
        />
      </span>
      {filter && (
        <DropdownMenu open={filtersOpen} onOpenChange={setFiltersOpen}>
          <Tooltip>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild>
                <Button
                  ref={filterButton}
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label={filter.label}
                  data-state={filter.pressed ? "on" : "off"}
                  className="data-[state=on]:bg-accent data-[state=on]:text-accent-foreground"
                >
                  <filter.icon />
                </Button>
              </DropdownMenuTrigger>
            </TooltipTrigger>
            <TooltipContent>{filter.label}</TooltipContent>
          </Tooltip>
          <FilterMenuContent
            filters={filters}
            dispatchFilter={dispatchFilter}
            languages={languages}
            testCount={testCount}
            generatedCount={generatedCount}
            ownedFiles={ownedFiles}
          />
        </DropdownMenu>
      )}
      {compact ? (
        <ViewOptionsMenu commands={viewCommands} />
      ) : (
        viewCommands.map((command) => <ToolbarButton key={command.id} command={command} />)
      )}
      {/* The source's controls wrap as one group, and within it once the group can't fit a row. */}
      {extra && (
        <span className="flex max-w-full min-w-0 flex-wrap items-center justify-end gap-x-1 gap-y-0.5">
          {extra}
        </span>
      )}
    </div>
  );
}

function FilterMenuContent({
  filters,
  dispatchFilter,
  languages,
  testCount,
  generatedCount,
  ownedFiles,
}: Pick<
  DiffReviewToolbarProps,
  "filters" | "dispatchFilter" | "languages" | "testCount" | "generatedCount"
> & {
  /** The shared owned-files command; absent while the reader owns no file. */
  ownedFiles?: DiffReviewCommand;
}) {
  const ownedOnly = ownedFiles?.setting?.choices.find((choice) => choice.value === "only");
  const filtered = hasActiveFilters(filters);
  return (
    <DropdownMenuContent align="end" className="w-64">
      <DropdownMenuLabel className="flex items-center justify-between">
        Languages
        <button
          type="button"
          className="text-xs font-normal text-muted-foreground hover:text-foreground"
          onClick={() =>
            dispatchFilter({
              type: filters.selectedLanguages === null ? "hideAllLanguages" : "showAllLanguages",
            })
          }
        >
          {filters.selectedLanguages === null ? "Select none" : "Select all"}
        </button>
      </DropdownMenuLabel>
      {languages.map(({ language, count }) => (
        <DropdownMenuCheckboxItem
          key={language}
          checked={filters.selectedLanguages?.includes(language) ?? true}
          onSelect={(event) => event.preventDefault()}
          onCheckedChange={() => dispatchFilter({ type: "toggleLanguage", language })}
        >
          <span className="flex-1">{language}</span>
          <span className="text-xs text-muted-foreground tabular-nums">{count}</span>
        </DropdownMenuCheckboxItem>
      ))}
      <DropdownMenuSeparator />
      <DropdownMenuLabel>Test files ({testCount})</DropdownMenuLabel>
      <DropdownMenuRadioGroup
        value={filters.testFileView}
        onValueChange={(value) =>
          dispatchFilter({ type: "setTestFileView", value: value as DiffTestFileView })
        }
      >
        {(["all", "skipTests", "onlyTests"] as const).map((view) => (
          <DropdownMenuRadioItem
            key={view}
            value={view}
            onSelect={(event) => event.preventDefault()}
          >
            {testFileViewLabel(view)}
          </DropdownMenuRadioItem>
        ))}
      </DropdownMenuRadioGroup>
      <DropdownMenuSeparator />
      <DropdownMenuCheckboxItem
        checked={filters.showGeneratedFiles}
        title={GENERATED_FILES_NOTICE}
        onSelect={(event) => event.preventDefault()}
        onCheckedChange={(checked) =>
          dispatchFilter({ type: "setShowGeneratedFiles", value: checked === true })
        }
      >
        <span className="flex-1">Show generated files</span>
        <span className="text-xs text-muted-foreground tabular-nums">{generatedCount}</span>
      </DropdownMenuCheckboxItem>
      {ownedFiles?.setting && ownedOnly && (
        <DropdownMenuCheckboxItem
          checked={ownedFiles.pressed}
          onSelect={(event) => event.preventDefault()}
          onCheckedChange={(checked) =>
            ownedFiles.setting?.choose(checked === true ? "only" : "all")
          }
        >
          {ownedOnly.label}
        </DropdownMenuCheckboxItem>
      )}
      {filtered && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => dispatchFilter({ type: "clearFilters" })}>
            Clear filters
          </DropdownMenuItem>
        </>
      )}
    </DropdownMenuContent>
  );
}

/** The view controls of a narrow review; toggles keep the menu open to show their new state. */
function ViewOptionsMenu({ commands }: { commands: readonly DiffReviewCommand[] }) {
  const label = "More view options";
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={label}
              data-state={commands.some((command) => command.pressed) ? "on" : "off"}
              className="data-[state=on]:bg-accent data-[state=on]:text-accent-foreground"
            >
              <MoreHorizontalIcon />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="end" className="min-w-52">
        {commands.map((command) => (
          <DropdownMenuItem
            key={command.id}
            disabled={command.unavailableReason !== undefined}
            onSelect={(event) => {
              if (command.pressed !== undefined) event.preventDefault();
              command.run();
            }}
          >
            <command.icon />
            {command.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** A pressed toggle while the tree is docked; the trigger its popover returns focus to otherwise. */
function TreeToggle({ command, popover }: { command: DiffReviewCommand; popover: boolean }) {
  // The trigger names its popover (expanded, controls); a docked tree is no popup.
  const dockedState = popover
    ? {}
    : {
        "aria-pressed": command.pressed,
        "aria-haspopup": undefined,
        "aria-expanded": undefined,
        "aria-controls": undefined,
      };
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={command.label}
            {...dockedState}
            disabled={command.unavailableReason !== undefined}
            data-state={command.pressed ? "on" : "off"}
            className="data-[state=on]:bg-accent data-[state=on]:text-accent-foreground"
            onClick={(event) => {
              if (popover) return;
              // Docking is the review's to toggle; skip the trigger's popover toggle.
              event.preventDefault();
              command.run();
            }}
          >
            <command.icon />
          </Button>
        </PopoverTrigger>
      </TooltipTrigger>
      <TooltipContent>{command.label}</TooltipContent>
    </Tooltip>
  );
}

function ToolbarButton({ command }: { command: DiffReviewCommand }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={command.label}
          aria-pressed={command.pressed}
          disabled={command.unavailableReason !== undefined}
          data-state={command.pressed ? "on" : "off"}
          className="data-[state=on]:bg-accent data-[state=on]:text-accent-foreground"
          onClick={command.run}
        >
          <command.icon />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{command.label}</TooltipContent>
    </Tooltip>
  );
}

/**
 * A spinner for work the review is still doing, with `label` in its tooltip and announced
 * politely. Its space is kept while idle, so the toolbar wraps the same either way, and the
 * live region stays mounted, so the label is read as it appears.
 */
export function DiffReviewToolbarProgress({ label, active }: { label: string; active: boolean }) {
  return (
    <span
      role="status"
      data-testid="diff-review-toolbar-status"
      className="flex size-3.5 shrink-0 items-center justify-center text-muted-foreground"
    >
      {active && (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="flex items-center" data-testid="diff-review-toolbar-progress">
              <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
              <span className="sr-only">{label}</span>
            </span>
          </TooltipTrigger>
          <TooltipContent>{label}</TooltipContent>
        </Tooltip>
      )}
    </span>
  );
}
