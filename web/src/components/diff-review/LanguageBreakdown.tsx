// The toolbar's +N −M: a button that opens the listed files' changed lines by
// language. Choosing a language shows only its files; choosing the one shown
// alone shows every language again.

import { CheckIcon } from "lucide-react";
import { useId } from "react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import type { DiffReviewCommand } from "./diffReviewCommands";
import {
  onlySelectedLanguage,
  type DiffFilterAction,
  type DiffReviewFilters,
  type LanguageLineCounts,
  type LineBreakdown,
} from "./diffReviewModel";

function formatCount(count: number): string {
  return count.toLocaleString();
}

function filesLabel(count: number): string {
  return `${formatCount(count)} ${count === 1 ? "file" : "files"}`;
}

/** The line counts as the toolbar shows them, green added and red removed. */
function LineCounts({ additions, deletions }: { additions: number; deletions: number }) {
  return (
    <span className="flex items-center gap-2 tabular-nums whitespace-nowrap">
      <span className="text-success">+{formatCount(additions)}</span>
      <span className="text-destructive">−{formatCount(deletions)}</span>
    </span>
  );
}

/** A language's share of every changed line, split into its added and removed lines. */
function ShareBar({ entry, changedLines }: { entry: LanguageLineCounts; changedLines: number }) {
  const lines = entry.additions + entry.deletions;
  // A language whose files changed no lines, such as binary ones, keeps an empty track.
  const share = changedLines > 0 ? (lines / changedLines) * 100 : 0;
  const added = lines > 0 ? (entry.additions / lines) * 100 : 0;
  return (
    <span aria-hidden="true" className="flex h-1 w-full overflow-hidden rounded-full bg-muted">
      <span className="flex h-full" style={{ width: `${share}%` }}>
        <span className="h-full bg-success" style={{ width: `${added}%` }} />
        <span className="h-full flex-1 bg-destructive" />
      </span>
    </span>
  );
}

export function LanguageBreakdown({
  command,
  breakdown,
  filters,
  dispatchFilter,
  open,
  onOpenChange,
  restoreFocus = true,
}: {
  /** The shared "Show changes by language" command: its label and availability. */
  command: DiffReviewCommand | undefined;
  breakdown: LineBreakdown;
  filters: DiffReviewFilters;
  dispatchFilter: (action: DiffFilterAction) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Whether closing returns focus to the trigger; not once the review is hidden. */
  restoreFocus?: boolean;
}) {
  const ids = useId();
  const { totals, languages } = breakdown;
  const fileCount = languages.reduce((sum, entry) => sum + entry.files, 0);
  const changedLines = totals.additions + totals.deletions;
  const only = onlySelectedLanguage(filters);
  const unavailable = !command || command.unavailableReason !== undefined;
  const choose = (action: DiffFilterAction) => {
    dispatchFilter(action);
    onOpenChange(false);
  };
  const counts = `${formatCount(totals.additions)} added, ${formatCount(totals.deletions)} removed`;
  return (
    <Popover open={open && !unavailable} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          disabled={unavailable}
          aria-label={command ? `${counts}. ${command.label}` : counts}
          title={command?.unavailableReason ?? command?.label}
          data-testid="diff-review-line-counts"
          className="h-6 px-1.5 font-normal disabled:opacity-100 data-[state=open]:bg-accent"
        >
          <LineCounts additions={totals.additions} deletions={totals.deletions} />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-80 gap-2 p-2"
        aria-label="Changes by language"
        onCloseAutoFocus={(event) => {
          if (!restoreFocus) event.preventDefault();
        }}
      >
        <div className="flex items-center justify-between gap-2 px-2 pt-0.5 text-xs text-muted-foreground">
          <span>{filesLabel(fileCount)}</span>
          <LineCounts additions={totals.additions} deletions={totals.deletions} />
        </div>
        <div
          role="group"
          aria-label="Languages"
          className="flex max-h-[min(20rem,60vh)] flex-col gap-0.5 overflow-y-auto"
        >
          {languages.map((entry, index) => {
            const pressed = only === entry.language;
            const describedBy = `${ids}-${index}`;
            const action = pressed ? "Show all languages" : `Show only ${entry.language}`;
            return (
              <Button
                key={entry.language}
                type="button"
                variant="ghost"
                aria-pressed={pressed}
                aria-describedby={describedBy}
                title={action}
                data-language={entry.language}
                className="h-auto w-full flex-col items-stretch gap-1 px-2 py-1.5 font-normal"
                onClick={() =>
                  choose(
                    pressed
                      ? { type: "showAllLanguages" }
                      : { type: "showOnlyLanguage", language: entry.language },
                  )
                }
              >
                <span className="flex min-w-0 items-center gap-2 text-ui">
                  <CheckIcon
                    aria-hidden="true"
                    className={cn("size-3.5 shrink-0", !pressed && "invisible")}
                  />
                  <span className="min-w-0 flex-1 truncate text-left text-foreground">
                    {entry.language}
                  </span>
                  <span className="text-xs whitespace-nowrap text-muted-foreground">
                    {filesLabel(entry.files)}
                  </span>
                  <span className="text-xs">
                    <LineCounts additions={entry.additions} deletions={entry.deletions} />
                  </span>
                </span>
                <span className="pl-5.5">
                  <ShareBar entry={entry} changedLines={changedLines} />
                </span>
                <span id={describedBy} className="sr-only">
                  {action}
                </span>
              </Button>
            );
          })}
        </div>
        {filters.selectedLanguages !== null && (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            className="self-start"
            onClick={() => choose({ type: "showAllLanguages" })}
          >
            Show all languages
          </Button>
        )}
      </PopoverContent>
    </Popover>
  );
}
