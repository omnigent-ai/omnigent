// Go to File's text search options: Match case, Whole word, Regex and the file filter as
// switches beside the query field, each also on Alt and its letter, and the include and
// exclude fields under it while the filter is on.

import { type ReactNode, useEffect, useRef } from "react";
import { CaseSensitiveIcon, FilterIcon, RegexIcon, WholeWordIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { ContentSearchInput } from "@/lib/contentSearch";
import { isMacPlatform } from "@/lib/hotkeys";

export type TextSearchOptionId = "case" | "word" | "regex" | "filter";

interface TextSearchOption {
  id: TextSearchOptionId;
  label: string;
  icon: ReactNode;
  /** The key that, with Alt, flips it while the query field has the keyboard. */
  code: string;
  letter: string;
}

export const TEXT_SEARCH_OPTIONS: readonly TextSearchOption[] = [
  { id: "case", label: "Match case", icon: <CaseSensitiveIcon />, code: "KeyC", letter: "C" },
  { id: "word", label: "Match whole word", icon: <WholeWordIcon />, code: "KeyW", letter: "W" },
  { id: "regex", label: "Use regular expression", icon: <RegexIcon />, code: "KeyR", letter: "R" },
  {
    id: "filter",
    label: "Files to include or exclude",
    icon: <FilterIcon />,
    code: "KeyF",
    letter: "F",
  },
];

/** Whether the search is limited to some files. */
export function hasFileFilter(input: ContentSearchInput): boolean {
  return input.include.trim() !== "" || input.exclude.trim() !== "";
}

/** Whether `id` is on; the filter counts as on while it shows or has a glob in it. */
export function textSearchOptionPressed(
  input: ContentSearchInput,
  id: TextSearchOptionId,
  filtering: boolean,
): boolean {
  switch (id) {
    case "case":
      return input.caseSensitive;
    case "word":
      return input.wholeWord === true;
    case "regex":
      return input.regex;
    case "filter":
      return filtering || hasFileFilter(input);
  }
}

/** `input` with `id` flipped; the filter only shows or hides its fields. */
export function toggleTextSearchOption(
  input: ContentSearchInput,
  id: Exclude<TextSearchOptionId, "filter">,
): ContentSearchInput {
  switch (id) {
    case "case":
      return { ...input, caseSensitive: !input.caseSensitive };
    case "word":
      return { ...input, wholeWord: input.wholeWord !== true };
    case "regex":
      return { ...input, regex: !input.regex };
  }
}

export interface TextSearchControlsProps {
  input: ContentSearchInput;
  onInputChange: (input: ContentSearchInput) => void;
  filtering: boolean;
  onFilteringChange: (filtering: boolean) => void;
}

function useLatest<T>(value: T) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}

/**
 * Flips an option from Alt and its letter while focus is in `scope`, the dialog, but not in a
 * filter field, where Option and a letter type a character such as ç on a Mac.
 */
export function useTextSearchOptionKeys(
  scope: () => Element | null,
  props: TextSearchControlsProps,
): void {
  const latest = useLatest({ scope, ...props });
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (!event.altKey || event.metaKey || event.ctrlKey || event.shiftKey || event.repeat) return;
      const option = TEXT_SEARCH_OPTIONS.find((candidate) => candidate.code === event.code);
      const current = latest.current;
      const within = current.scope();
      const focused = document.activeElement;
      if (!option || !within || !within.contains(focused)) return;
      if (focused?.closest("[data-text-search-filters]")) return;
      event.preventDefault();
      event.stopPropagation();
      if (option.id === "filter") current.onFilteringChange(!current.filtering);
      else current.onInputChange(toggleTextSearchOption(current.input, option.id));
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [latest]);
}

/** The switches beside the query field. */
export function TextSearchToggles({
  input,
  onInputChange,
  filtering,
  onFilteringChange,
}: TextSearchControlsProps) {
  const alt = isMacPlatform() ? "⌥" : "Alt";
  const filtered = hasFileFilter(input);
  return (
    <div className="flex shrink-0 items-center gap-0.5" data-testid="text-search-options">
      {TEXT_SEARCH_OPTIONS.map((option) => (
        <Tooltip key={option.id}>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={
                option.id === "filter" && filtered ? `${option.label} (filtering)` : option.label
              }
              aria-pressed={textSearchOptionPressed(input, option.id, filtering)}
              // The query field keeps the keyboard.
              onMouseDown={(event) => event.preventDefault()}
              onClick={() =>
                option.id === "filter"
                  ? onFilteringChange(!filtering)
                  : onInputChange(toggleTextSearchOption(input, option.id))
              }
              className="text-muted-foreground aria-pressed:bg-muted aria-pressed:text-foreground aria-pressed:ring-1 aria-pressed:ring-border"
            >
              {option.icon}
            </Button>
          </TooltipTrigger>
          <TooltipContent shortcut={[alt, option.letter]}>{option.label}</TooltipContent>
        </Tooltip>
      ))}
    </div>
  );
}

/** The include and exclude fields, under the query field while the filter is on. */
export function TextSearchFilters({
  input,
  onInputChange,
}: Pick<TextSearchControlsProps, "input" | "onInputChange">) {
  return (
    <div
      className="grid grid-cols-2 gap-2 border-b border-border px-3 py-2 text-xs"
      data-text-search-filters
    >
      <label className="space-y-1">
        <span className="text-muted-foreground">Files to include</span>
        <Input
          placeholder="e.g. src/**, *.{ts,tsx}"
          maxLength={4096}
          value={input.include}
          onChange={(event) => onInputChange({ ...input, include: event.target.value })}
        />
      </label>
      <label className="space-y-1">
        <span className="text-muted-foreground">Files to exclude</span>
        <Input
          placeholder="e.g. **/tests/**, *.min.js"
          maxLength={4096}
          value={input.exclude}
          onChange={(event) => onInputChange({ ...input, exclude: event.target.value })}
        />
      </label>
      <p className="col-span-2 text-muted-foreground">
        Comma-separated globs. Exclude takes precedence.
      </p>
    </div>
  );
}
