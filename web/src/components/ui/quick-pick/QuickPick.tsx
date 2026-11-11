import { type KeyboardEvent, type ReactNode, useId, useMemo, useState } from "react";
import { SearchIcon } from "lucide-react";

import { CompactShortcutKeys } from "@/components/ui/kbd";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { cn } from "@/lib/utils";

import { HighlightedText, type MatchHighlight } from "./HighlightedText";
import type { QuickPickItem, QuickPickItemRow, QuickPickRow } from "./model";
import { buildQuickPickRows, type QuickPickSegment, reconcileActiveIndex } from "./presentation";

export interface QuickPickProps<Value> {
  /** Accessible name for the query field and results, such as "Command palette". */
  label: string;
  placeholder?: string;
  /** Runs of items in display order, such as search results and then commands. */
  segments: readonly QuickPickSegment<Value>[];
  query: string;
  onQueryChange: (query: string) => void;
  onAccept: (item: QuickPickItem<Value>) => void;
  /** How rows' matched text stands out, unless a row sets its own. */
  matchHighlight?: MatchHighlight;
  /** Shown when no row is listed, such as "No results found". */
  emptyMessage: ReactNode;
  /** Content above the rows inside the scrolling area, such as a load error with Retry. */
  listStart?: ReactNode;
  /** Content below the rows inside the scrolling area, such as a button that loads more. */
  listEnd?: ReactNode;
  /** Trailing content beside the query field, such as a close button. */
  inputEnd?: ReactNode;
  autoFocus?: boolean;
  /** Grow the results to fill the container, such as a full-screen sheet on phones. */
  fillHeight?: boolean;
  className?: string;
}

interface RowSection<Value> {
  heading?: { key: string; label: string };
  rows: { row: QuickPickItemRow<Value>; index: number }[];
}

// A heading starts a named section for its group's rows; rows outside any
// group, such as ungrouped commands after a group, form an unnamed one.
function sectionsOf<Value>(rows: readonly QuickPickRow<Value>[]): RowSection<Value>[] {
  const sections: RowSection<Value>[] = [];
  rows.forEach((row, index) => {
    if (row.kind === "heading") {
      sections.push({ heading: row, rows: [] });
      return;
    }
    const current = sections.at(-1);
    const belongs = current && (current.heading?.label ?? undefined) === row.item.group;
    if (current && belongs) current.rows.push({ row, index });
    else sections.push({ rows: [{ row, index }] });
  });
  return sections;
}

function resultCountText(resultCount: number): string {
  return resultCount === 1 ? "1 result" : `${resultCount.toLocaleString()} results`;
}

/**
 * Keyboard-first picker: a query field over fuzzy-ranked rows grouped under
 * headings. The caller owns the query and what accepting a row does.
 */
export function QuickPick<Value>({
  label,
  placeholder,
  segments,
  query,
  onQueryChange,
  onAccept,
  matchHighlight = "emphasis",
  emptyMessage,
  listStart,
  listEnd,
  inputEnd,
  autoFocus = true,
  fillHeight = false,
  className,
}: QuickPickProps<Value>) {
  const baseId = useId();
  const listId = `${baseId}-list`;
  const optionId = (index: number) => `${baseId}-option-${index}`;

  const rows = useMemo(() => buildQuickPickRows({ segments, query }), [segments, query]);
  const sections = useMemo(() => sectionsOf(rows), [rows]);
  const resultCount = rows.filter((row) => row.kind === "item").length;

  // Until the user picks a row for this query, the top row stays active as results arrive.
  // Any query change forgets the pick, so returning to an earlier query starts at the top.
  const [activeChoice, setActiveChoice] = useState<{ query: string; id: string | null }>({
    query,
    id: null,
  });
  if (activeChoice.query !== query) setActiveChoice({ query, id: null });
  const activeIndex = reconcileActiveIndex(
    rows,
    activeChoice.query === query ? activeChoice.id : null,
  );
  const activeRow = activeIndex === -1 ? null : (rows[activeIndex] as QuickPickItemRow<Value>);

  const accept = (row: QuickPickItemRow<Value>) => {
    if (!row.item.disabledReason) onAccept(row.item);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    // Enter that confirms an IME composition belongs to the composition.
    if (isImeCompositionKeyEvent(event)) return;
    if (event.key === "Enter") {
      event.preventDefault();
      if (activeRow) accept(activeRow);
    }
  };

  const renderOption = (row: QuickPickItemRow<Value>, index: number) => {
    const { item } = row;
    const isActive = index === activeIndex;
    const disabled = Boolean(item.disabledReason);
    const highlight = item.matchHighlight ?? matchHighlight;
    return (
      <div
        key={row.key}
        id={optionId(index)}
        role="option"
        aria-selected={isActive}
        aria-disabled={disabled || undefined}
        data-active={isActive || undefined}
        // Hovering an enabled row makes it the user's pick, as in a menu, even when it
        // is already the top row, so a result arriving above it doesn't take over.
        onPointerMove={() => {
          if (!disabled && activeChoice.id !== item.id) setActiveChoice({ query, id: item.id });
        }}
        // Keep focus in the query field.
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => accept(row)}
        className={cn(
          "flex cursor-pointer items-start gap-2 rounded-lg px-2 py-1.5 text-ui select-none",
          "data-active:bg-muted data-active:text-foreground",
          disabled && "cursor-default opacity-50",
        )}
      >
        {item.icon ? (
          <span className="flex size-4 shrink-0 items-center justify-center self-center text-muted-foreground [&_svg]:size-4">
            {item.icon}
          </span>
        ) : null}
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span data-slot="quick-pick-label" className="truncate">
            <HighlightedText text={item.label} ranges={row.labelRanges} highlight={highlight} />
          </span>
          {item.description && (
            <span className="truncate text-sm text-muted-foreground">
              <HighlightedText
                text={item.description}
                ranges={row.descriptionRanges}
                highlight={highlight}
              />
            </span>
          )}
        </span>
        {item.disabledReason && (
          <span
            data-slot="quick-pick-disabled-reason"
            title={item.disabledReason}
            className="min-w-0 shrink truncate self-center text-sm text-muted-foreground"
          >
            {item.disabledReason}
          </span>
        )}
        {item.accessory && (
          <span className="flex shrink-0 items-center gap-2 self-center">{item.accessory}</span>
        )}
        {item.shortcut && (
          <CompactShortcutKeys keys={item.shortcut} className="shrink-0 self-center" />
        )}
      </div>
    );
  };

  return (
    <div
      data-slot="quick-pick"
      className={cn(
        "flex min-h-0 flex-col bg-popover text-popover-foreground",
        fillHeight && "flex-1",
        className,
      )}
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <SearchIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
        <input
          role="combobox"
          aria-label={label}
          aria-expanded
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeIndex === -1 ? undefined : optionId(activeIndex)}
          autoFocus={autoFocus}
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          placeholder={placeholder}
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          onKeyDown={handleKeyDown}
          className="h-8 min-w-0 flex-1 bg-transparent text-ui outline-hidden placeholder:text-muted-foreground"
        />
        {inputEnd}
      </div>
      <div
        data-slot="quick-pick-scroll"
        className={cn("overflow-y-auto p-1", fillHeight ? "min-h-0 flex-1" : "max-h-72")}
      >
        {listStart}
        <div id={listId} role="listbox" aria-label={label}>
          {sections.map((section, sectionIndex) => {
            const headingId = section.heading ? `${baseId}-heading-${sectionIndex}` : undefined;
            return (
              <div
                key={section.heading?.key ?? `section-${sectionIndex}`}
                role="group"
                aria-labelledby={headingId}
                className="pb-1"
              >
                {section.heading && (
                  <div
                    id={headingId}
                    role="presentation"
                    className="px-2 py-1.5 text-sm font-medium text-muted-foreground"
                  >
                    {section.heading.label}
                  </div>
                )}
                {section.rows.map(({ row, index }) => renderOption(row, index))}
              </div>
            );
          })}
        </div>
        {/* One live region: the visible empty message, or the result count for screen readers. */}
        <div
          role="status"
          aria-live="polite"
          data-slot={resultCount === 0 ? "quick-pick-empty" : undefined}
          className={resultCount === 0 ? "py-6 text-center text-ui" : "sr-only"}
        >
          {resultCount === 0 ? emptyMessage : resultCountText(resultCount)}
        </div>
        {listEnd}
      </div>
    </div>
  );
}
