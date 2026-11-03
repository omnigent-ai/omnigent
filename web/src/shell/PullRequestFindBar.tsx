// Find in the fetched patch, for the Changes toolbar's navigation slot. Matches are
// computed by the tab (they depend on the visible files); this bar shows the
// query, the position and the hidden-file hint, and forwards the reader's actions.

import { useEffect, useRef } from "react";
import { ChevronDownIcon, ChevronUpIcon, SearchIcon, XIcon } from "lucide-react";
import type { FindState } from "./useChangesViewState";
import { IconButton } from "./PullRequestFileSection";
import { cn } from "@/lib/utils";

export interface PullRequestFindBarProps {
  find: FindState;
  inputRef: React.RefObject<HTMLInputElement | null>;
  /** 1-based position of the active match among the visible matches; 0 when there is none. */
  position: number;
  total: number;
  /** The visible matches hit the search cap. */
  truncated: boolean;
  invalid: boolean;
  /** Matches in files the filters hide. */
  hiddenCount: number;
  hiddenTruncated: boolean;
  onOpen: () => void;
  onClose: () => void;
  onQueryChange: (query: string) => void;
  onToggleOption: (option: "matchCase" | "regex") => void;
  onStep: (direction: 1 | -1) => void;
  onShowHidden: () => void;
}

function OptionToggle({
  label,
  pressed,
  onClick,
  children,
}: {
  label: string;
  pressed: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={pressed}
      title={label}
      onClick={onClick}
      className={cn(
        "shrink-0 rounded px-1 font-mono text-xs",
        pressed ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted",
      )}
    >
      {children}
    </button>
  );
}

function findStatus({
  query,
  invalid,
  total,
  position,
  truncated,
}: {
  query: string;
  invalid: boolean;
  total: number;
  position: number;
  truncated: boolean;
}): string {
  if (!query.trim()) return "";
  if (invalid) return "Invalid";
  if (total === 0) return "No results";
  const count = `${total}${truncated ? "+" : ""}`;
  // Position is 0 for the moment between a query change and the viewport seeding its match.
  return position === 0 ? `${count} matches` : `${position} of ${count}`;
}

export function PullRequestFindBar({
  find,
  inputRef,
  position,
  total,
  truncated,
  invalid,
  hiddenCount,
  hiddenTruncated,
  onOpen,
  onClose,
  onQueryChange,
  onToggleOption,
  onStep,
  onShowHidden,
}: PullRequestFindBarProps) {
  const { open, query } = find;
  const trigger = useRef<HTMLSpanElement>(null);
  // Closing removes the input, so a reader's close returns focus to the trigger, keeping
  // Cmd/Ctrl+F inside the Changes tab.
  const restoreFocus = useRef(false);
  // Focus follows the reader opening the bar, not a remount that finds it already open.
  const wasOpen = useRef(open);
  useEffect(() => {
    const opened = open && !wasOpen.current;
    wasOpen.current = open;
    if (opened) inputRef.current?.focus();
    else if (!open && restoreFocus.current) {
      restoreFocus.current = false;
      trigger.current?.querySelector("button")?.focus();
    }
  }, [open, inputRef]);
  const close = () => {
    restoreFocus.current = true;
    onClose();
  };

  if (!open) {
    return (
      <span ref={trigger}>
        <IconButton
          label="Find in changes"
          onClick={onOpen}
          className="size-5 text-muted-foreground"
        >
          <SearchIcon className="size-3.5" />
        </IconButton>
      </span>
    );
  }

  const status = findStatus({ query, invalid, total, position, truncated });

  return (
    <div className="flex min-w-0 flex-1 items-center gap-1.5 text-ui">
      <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
      <input
        ref={inputRef}
        type="text"
        aria-label="Find in changes"
        value={query}
        onChange={(event) => onQueryChange(event.target.value)}
        onKeyDown={(event) => {
          // IME composition keys (CJK input) confirm text, not find.
          if (event.nativeEvent.isComposing) return;
          if (event.key === "Enter") {
            event.preventDefault();
            onStep(event.shiftKey ? -1 : 1);
          } else if (event.key === "Escape") {
            event.preventDefault();
            close();
          }
        }}
        placeholder="Find in changes…"
        className="min-w-0 flex-1 bg-transparent text-sm outline-none"
      />
      <span role="status" className="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
        {status}
      </span>
      {hiddenCount > 0 && (
        <span className="flex shrink-0 items-center gap-1 whitespace-nowrap text-xs text-muted-foreground">
          +{hiddenCount}
          {hiddenTruncated ? "+" : ""} in hidden files
          <button type="button" className="text-foreground underline" onClick={onShowHidden}>
            Show
          </button>
        </span>
      )}
      <OptionToggle
        label="Match case"
        pressed={find.matchCase}
        onClick={() => onToggleOption("matchCase")}
      >
        Aa
      </OptionToggle>
      <OptionToggle
        label="Regular expression"
        pressed={find.regex}
        onClick={() => onToggleOption("regex")}
      >
        .*
      </OptionToggle>
      <IconButton
        label="Previous match"
        onClick={() => onStep(-1)}
        disabled={total === 0}
        className="size-5 text-muted-foreground"
      >
        <ChevronUpIcon className="size-3.5" />
      </IconButton>
      <IconButton
        label="Next match"
        onClick={() => onStep(1)}
        disabled={total === 0}
        className="size-5 text-muted-foreground"
      >
        <ChevronDownIcon className="size-3.5" />
      </IconButton>
      <IconButton label="Close find" onClick={close} className="size-5 text-muted-foreground">
        <XIcon className="size-3.5" />
      </IconButton>
    </div>
  );
}
