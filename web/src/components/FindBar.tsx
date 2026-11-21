// Floating find bar shared by the chat thread and the file viewer. Callers own
// the search state; this component renders it and maps keys to commands. An
// editor can add a replace row.

import { shortcutKeys } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { type CSSProperties, useEffect, useId, useRef, type ReactNode } from "react";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  ChevronUpIcon,
  ReplaceAllIcon,
  ReplaceIcon,
  SearchIcon,
  XIcon,
} from "lucide-react";
import {
  KeyboardShortcutTooltipContent,
  MOD_KEY,
  SHIFT_KEY,
  ENTER_KEY,
} from "@/components/KeyboardShortcut";
import { hasCommandModifier } from "@/lib/hotkeys";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { FIND_BAR_ATTRIBUTE } from "@/lib/findBarFocus";
import { cn } from "@/lib/utils";

/** How a find query matches, as the bar's toggles set it. */
export interface FindOptions {
  caseSensitive: boolean;
  wholeWord: boolean;
  regexp: boolean;
}

export const DEFAULT_FIND_OPTIONS: FindOptions = {
  caseSensitive: false,
  wholeWord: false,
  regexp: false,
};

interface FindOptionToggleSpec {
  key: keyof FindOptions;
  label: string;
  glyph: string;
}

const FIND_OPTION_TOGGLES: readonly FindOptionToggleSpec[] = [
  { key: "caseSensitive", label: "Match case", glyph: "Aa" },
  { key: "wholeWord", label: "Match whole word", glyph: "ab" },
  { key: "regexp", label: "Use regular expression", glyph: ".*" },
];

const REPLACE_ALL_KEYS = [MOD_KEY, ENTER_KEY];

/** The replace row an editor adds under the query. */
export interface FindBarReplace {
  value: string;
  onChange: (value: string) => void;
  /** Whether the row shows; its toggle sits before the query. */
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Replaces the selected match and moves to the next one. */
  onReplace: () => void;
  onReplaceAll: () => void;
}

export interface FindBarProps {
  /** What the bar searches, e.g. "Find in conversation"; also the placeholder. */
  label: string;
  query: string;
  onQueryChange: (query: string) => void;
  /** "3 of 12", "No results", or empty without a query. */
  status: string;
  hasMatches: boolean;
  /** Shows a spinner beside the status while matches are still being collected. */
  searching?: boolean;
  onNext: () => void;
  onPrevious: () => void;
  onClose: () => void;
  /** Changes on every open request so the query is focused and selected again. */
  focusRequest: unknown;
  /** Which field an open request focuses; the replace field only while its row shows. */
  focusTarget?: "query" | "replace";
  /** Adds the replace row, for a find bar over editable text. */
  replace?: FindBarReplace;
  /** Shows the case, whole-word, and regular-expression toggles when set. */
  options?: FindOptions;
  onOptionsChange?: (options: FindOptions) => void;
  /** Prefix for `-input` and `-status` test ids, e.g. "thread-find". */
  testIdPrefix: string;
  className?: string;
  /** Extra attributes for the bar's root, such as a marker other code looks for. */
  rootProps?: Record<`data-${string}`, string>;
  /** A note under the query row, such as matches the current view hides. */
  footer?: ReactNode;
  /**
   * One row docked in a pane's title strip rather than floating: `fold` 1 drops the Previous and
   * Next buttons (Enter and ⇧Enter still move), 2 drops the count too.
   */
  docked?: { fold: 0 | 1 | 2 };
}

/**
 * Docked, the bar sits on a terminal's row, which repaints only the base tokens: its field,
 * focus ring and hovered buttons read those too, so a terminal's own theme keeps them legible.
 */
const DOCKED_TOKENS = {
  "--input": "var(--border)",
  "--ring": "var(--muted-foreground)",
  "--accent": "var(--muted)",
  "--accent-foreground": "var(--foreground)",
} as CSSProperties;

export function FindBar({
  label,
  query,
  onQueryChange,
  status,
  hasMatches,
  searching = false,
  onNext,
  onPrevious,
  onClose,
  focusRequest,
  focusTarget = "query",
  replace,
  options,
  onOptionsChange,
  testIdPrefix,
  className,
  rootProps,
  footer,
  docked,
}: FindBarProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const replaceInputRef = useRef<HTMLInputElement>(null);
  const statusId = useId();
  const replaceRowId = useId();
  const replaceOpen = replace?.open === true;

  useEffect(() => {
    const input =
      focusTarget === "replace" && replaceInputRef.current
        ? replaceInputRef.current
        : inputRef.current;
    input?.focus();
    input?.select();
    // Only a new request moves focus; the target travels with it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusRequest]);

  // Esc closes from any control in the bar, not only the query field.
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key !== "Escape" || event.nativeEvent.isComposing) return;
    event.preventDefault();
    event.stopPropagation();
    onClose();
  };
  // Buttons keep the caret in their field so typing can continue.
  const refocus = (action: () => void) => () => {
    action();
    inputRef.current?.focus({ preventScroll: true });
  };
  const refocusReplace = (action: () => void) => () => {
    action();
    replaceInputRef.current?.focus({ preventScroll: true });
  };

  return (
    <div
      {...rootProps}
      {...{ [FIND_BAR_ATTRIBUTE]: "" }}
      role="search"
      aria-label={label}
      onKeyDown={onKeyDown}
      style={docked ? DOCKED_TOKENS : undefined}
      className={cn(
        docked
          ? "pointer-events-auto h-6 w-full overflow-hidden text-foreground"
          : "@container pointer-events-auto w-[28rem] max-w-full overflow-hidden rounded-lg border border-border bg-popover text-popover-foreground shadow-md dark:backdrop-blur-xl dark:backdrop-saturate-150",
        className,
      )}
    >
      {/* The query is the widest part, with its match toggles inside it. The field never
          shrinks below its contents, about 20 characters of text and its toggles, so the
          count gives way first; too narrow for that, the count and buttons move under it.
          Docked, it's one row that gives way by `docked.fold` instead. */}
      <div
        className={
          docked
            ? "flex h-6 items-center gap-0.5"
            : "flex items-center gap-0.5 p-1 @max-[24rem]:flex-wrap"
        }
      >
        <InputGroup
          data-testid={`${testIdPrefix}-field`}
          className={
            docked
              ? "h-6 min-w-0 flex-1 rounded-sm dark:bg-transparent"
              : "h-7 min-w-min flex-1 dark:bg-transparent @max-[24rem]:min-w-0 @max-[24rem]:basis-full"
          }
        >
          <InputGroupAddon>
            {replace ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <InputGroupButton
                    size="icon-xs"
                    aria-label="Replace"
                    aria-expanded={replaceOpen}
                    aria-controls={replaceRowId}
                    onClick={() => replace.onOpenChange(!replaceOpen)}
                  >
                    <ChevronRightIcon
                      className={cn("transition-transform", replaceOpen && "rotate-90")}
                    />
                  </InputGroupButton>
                </TooltipTrigger>
                <KeyboardShortcutTooltipContent
                  label={replaceOpen ? "Hide replace" : "Show replace"}
                  keys={shortcutKeys("file.findAndReplace") ?? []}
                />
              </Tooltip>
            ) : (
              <SearchIcon aria-hidden="true" />
            )}
          </InputGroupAddon>
          <InputGroupInput
            ref={inputRef}
            className={docked ? "min-w-0 text-xs" : "min-w-44 @max-[24rem]:min-w-0"}
            value={query}
            placeholder={label}
            aria-label={label}
            aria-describedby={statusId}
            data-testid={`${testIdPrefix}-input`}
            spellCheck={false}
            autoComplete="off"
            onChange={(event) => onQueryChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === "Enter") {
                event.preventDefault();
                if (event.shiftKey) onPrevious();
                else onNext();
              }
            }}
          />
          {options && onOptionsChange && (
            <InputGroupAddon align="inline-end" className="gap-0">
              {FIND_OPTION_TOGGLES.map((toggle) => (
                <FindOptionToggle
                  key={toggle.key}
                  label={toggle.label}
                  glyph={toggle.glyph}
                  pressed={options[toggle.key]}
                  onToggle={refocus(() =>
                    onOptionsChange({ ...options, [toggle.key]: !options[toggle.key] }),
                  )}
                />
              ))}
            </InputGroupAddon>
          )}
        </InputGroup>
        <span
          id={statusId}
          aria-live="polite"
          data-testid={`${testIdPrefix}-status`}
          title={status || undefined}
          className={cn(
            "flex min-w-0 shrink items-center gap-1 truncate px-1 text-xs tabular-nums whitespace-nowrap text-muted-foreground",
            docked ? docked.fold === 2 && "sr-only" : "@max-[24rem]:flex-1",
          )}
        >
          {searching && <Spinner aria-hidden="true" role={undefined} className="size-3 shrink-0" />}
          <span className="truncate">{status}</span>
        </span>
        <span className="flex shrink-0 items-center">
          {(!docked || docked.fold === 0) && (
            <>
              <FindBarButton
                label="Previous match"
                keys={[SHIFT_KEY, ENTER_KEY]}
                disabled={!hasMatches}
                onClick={refocus(onPrevious)}
              >
                <ChevronUpIcon />
              </FindBarButton>
              <FindBarButton
                label="Next match"
                keys={[ENTER_KEY]}
                disabled={!hasMatches}
                onClick={refocus(onNext)}
              >
                <ChevronDownIcon />
              </FindBarButton>
            </>
          )}
          <FindBarButton label="Close find" tooltip="Close" keys={["Esc"]} onClick={onClose}>
            <XIcon />
          </FindBarButton>
        </span>
      </div>
      {replace && replaceOpen && (
        <div id={replaceRowId} className="flex items-center gap-0.5 px-1 pb-1">
          <InputGroup className="h-7 min-w-min flex-1 dark:bg-transparent @max-[24rem]:min-w-0">
            {/* Lines the field up with the query, past the toggle beside it. */}
            <InputGroupAddon>
              <span aria-hidden="true" className="inline-block size-6" />
            </InputGroupAddon>
            <InputGroupInput
              ref={replaceInputRef}
              className="min-w-44 @max-[24rem]:min-w-0"
              value={replace.value}
              placeholder="Replace"
              aria-label="Replace with"
              data-testid={`${testIdPrefix}-replace-input`}
              spellCheck={false}
              autoComplete="off"
              onChange={(event) => replace.onChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.nativeEvent.isComposing || event.key !== "Enter") return;
                event.preventDefault();
                if (hasCommandModifier(event.nativeEvent)) replace.onReplaceAll();
                else replace.onReplace();
              }}
            />
          </InputGroup>
          <span className="flex shrink-0 items-center">
            <FindBarButton
              label="Replace match"
              tooltip="Replace"
              keys={[ENTER_KEY]}
              disabled={!hasMatches}
              onClick={refocusReplace(replace.onReplace)}
            >
              <ReplaceIcon />
            </FindBarButton>
            <FindBarButton
              label="Replace all"
              keys={REPLACE_ALL_KEYS}
              disabled={!hasMatches}
              onClick={refocusReplace(replace.onReplaceAll)}
            >
              <ReplaceAllIcon />
            </FindBarButton>
          </span>
        </div>
      )}
      {footer && (
        <div
          data-testid={`${testIdPrefix}-footer`}
          className="flex items-center gap-2 border-t border-border px-3 py-2 text-xs text-muted-foreground"
        >
          {footer}
        </div>
      )}
    </div>
  );
}

function FindBarButton({
  label,
  tooltip = label,
  keys,
  disabled,
  onClick,
  children,
}: {
  label: string;
  tooltip?: string;
  keys: string[];
  disabled?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <InputGroupButton size="icon-xs" aria-label={label} disabled={disabled} onClick={onClick}>
          {children}
        </InputGroupButton>
      </TooltipTrigger>
      <KeyboardShortcutTooltipContent label={tooltip} keys={keys} />
    </Tooltip>
  );
}

function FindOptionToggle({
  label,
  glyph,
  pressed,
  onToggle,
}: {
  label: string;
  glyph: string;
  pressed: boolean;
  onToggle: () => void;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <InputGroupButton
          size="icon-xs"
          aria-label={label}
          aria-pressed={pressed}
          onClick={onToggle}
          className={cn(
            "font-mono text-[11px] tracking-tighter",
            pressed && "bg-accent text-accent-foreground",
          )}
        >
          <span aria-hidden="true" className={glyph === "ab" ? "underline underline-offset-2" : ""}>
            {glyph}
          </span>
        </InputGroupButton>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
