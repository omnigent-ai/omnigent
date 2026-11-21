// A "Type to filter…" field inside a Radix context or dropdown menu's own content. Radix keeps the
// root, items, item focus, Escape and close; this layer only adds the field and the few keys that
// cross between it and the items, so typing filters and never fights Radix's typeahead.

import { SearchIcon } from "lucide-react";
import {
  type FocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { isImeCompositionKeyEvent } from "@/lib/ime";
import { pickListInputProps, readlineListStep } from "@/lib/listNavigationKeys";

const ENABLED_ITEMS =
  ':is([role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"]):not([data-disabled])';

function enabledItems(within: Element | null): HTMLElement[] {
  const content = within?.closest<HTMLElement>("[data-radix-menu-content]");
  return [...(content?.querySelectorAll<HTMLElement>(ENABLED_ITEMS) ?? [])];
}

/** `value` as menu searches compare it: lowercase, without accents. */
export function foldMenuText(value: string): string {
  return value
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
}

const isTypedCharacter = (event: ReactKeyboardEvent) =>
  event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey;

const words = (value: string) =>
  foldMenuText(value)
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean);

/**
 * Whether each word of `query` starts a word of `text`, in any order, ignoring case and accents,
 * so "copy b" finds "Copy branch name" but not "Copy GitHub link". An empty query keeps all.
 */
export function menuFilterMatches(text: string, query: string): boolean {
  const textWords = words(text);
  return words(query).every((word) => textWords.some((textWord) => textWord.startsWith(word)));
}

export interface MenuFilter {
  query: string;
  /** Whether the field has focus, so the item Enter runs can be marked. */
  fieldFocused: boolean;
  /** Spread on the menu's Radix content, before its own `onCloseAutoFocus`. */
  contentProps: {
    onFocus: (event: FocusEvent<HTMLElement>) => void;
    onEscapeKeyDown: (event: KeyboardEvent) => void;
    onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void;
  };
  /** @internal The field's state, for `MenuFilterField`. */
  field: {
    inputRef: RefObject<HTMLInputElement | null>;
    setQuery: (query: string) => void;
    setFocused: (focused: boolean) => void;
  };
}

/**
 * A menu's filter, owned by the menu's surface; the field resets it as the menu closes. Without
 * `escapeClearsQuery`, Escape closes the menu at once, its query and all.
 */
export function useMenuFilter({
  escapeClearsQuery = true,
}: { escapeClearsQuery?: boolean } = {}): MenuFilter {
  const [query, setQuery] = useState("");
  const [fieldFocused, setFocused] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const contentProps = useMemo<MenuFilter["contentProps"]>(
    () => ({
      // Radix focuses its content as it opens and as the pointer leaves an item; the field takes
      // that focus, so typing always lands there.
      // Its default prevented, a keyboard opening skips Radix's move on to the first item.
      onFocus: (event) => {
        if (event.target !== event.currentTarget) return;
        event.preventDefault();
        inputRef.current?.focus();
      },
      onEscapeKeyDown: (event) => {
        // Escape during composition cancels the composition, not the query or the menu.
        if (isImeCompositionKeyEvent({ nativeEvent: event })) {
          event.preventDefault();
          return;
        }
        // A query clears first; only an empty field lets Escape close the menu.
        const field = inputRef.current;
        if (!escapeClearsQuery || !field?.value) return;
        event.preventDefault();
        setQuery("");
        field.focus();
      },
      // Runs before Radix's own handler, which a default prevented here skips.
      onKeyDown: (event) => {
        const field = inputRef.current;
        if (!field || event.target === field || isImeCompositionKeyEvent(event)) return;
        // A key in a submenu reaches this content through React's tree; that submenu keeps it.
        const target = event.target as Element;
        if (target.closest("[data-radix-menu-content]") !== event.currentTarget) return;
        // A character typed on an item goes to the field instead of Radix's typeahead, unless the
        // item took it, as Space selects it.
        if (isTypedCharacter(event) && !event.defaultPrevented) {
          event.preventDefault();
          setQuery((current) => current + event.key);
          field.focus();
          return;
        }
        // Up from the first item goes back to the field.
        if (event.key === "ArrowUp" && enabledItems(field)[0] === event.target) {
          event.preventDefault();
          field.focus();
        }
      },
    }),
    [escapeClearsQuery],
  );
  const field = useMemo(() => ({ inputRef, setQuery, setFocused }), []);
  return { query, fieldFocused, contentProps, field };
}

/** The field, first in the menu's content. */
export function MenuFilterField({
  filter,
  label = "Filter actions",
  placeholder = "Type to filter…",
  role,
  shortcut,
}: {
  filter: MenuFilter;
  label?: string;
  placeholder?: string;
  /** "searchbox" where the field searches values rather than filtering actions. */
  role?: "searchbox";
  /** The key that opens the menu, shown at the field's end. */
  shortcut?: string;
}) {
  const { inputRef, setQuery, setFocused } = filter.field;
  const reset = useCallback(() => {
    setQuery("");
    setFocused(false);
  }, [setQuery, setFocused]);
  // The content unmounts once the menu has closed: the next opening starts empty.
  useEffect(() => reset, [reset]);
  return (
    <div className="mb-1 flex items-center gap-2 border-b border-border px-1.5 pb-1.5">
      <SearchIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
      <input
        ref={inputRef}
        value={filter.query}
        role={role}
        aria-label={label}
        placeholder={placeholder}
        spellCheck={false}
        autoComplete="off"
        // An open list's field: app-wide Ctrl-N/Ctrl-P chords leave those steps to the menu.
        {...pickListInputProps(true)}
        className="h-6 min-w-0 flex-1 bg-transparent text-ui text-foreground outline-hidden placeholder:text-muted-foreground"
        onChange={(event) => setQuery(event.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={(event) => {
          // Keys that compose text (Enter confirming it, arrows choosing a candidate) belong to the
          // input method: they neither run, move through nor filter the menu.
          if (isImeCompositionKeyEvent(event)) {
            event.stopPropagation();
            return;
          }
          // Characters and editing keys are the field's, not Radix's typeahead.
          if (isTypedCharacter(event) || event.key === "Backspace" || event.key === "Delete") {
            event.stopPropagation();
            return;
          }
          // Left and Right move the caret through a query, rather than closing a submenu.
          if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && filter.query) {
            event.stopPropagation();
            return;
          }
          // Ctrl-N and Ctrl-P step as ArrowDown and ArrowUp: down to the first enabled item, and
          // nowhere up from the top, though held so neither the app nor Print takes them.
          const step = readlineListStep(event);
          if (event.key === "ArrowDown" || step === "next") {
            event.preventDefault();
            enabledItems(event.currentTarget)[0]?.focus();
            return;
          }
          if (step === "previous") {
            event.preventDefault();
            return;
          }
          // Enter runs the first match, through the item's own select and close.
          if (event.key === "Enter") {
            event.preventDefault();
            enabledItems(event.currentTarget)[0]?.click();
          }
        }}
      />
      {shortcut && (
        <kbd
          aria-hidden
          className="rounded border border-border px-1 font-sans text-xs text-muted-foreground"
        >
          {shortcut}
        </kbd>
      )}
    </div>
  );
}

/** Spread on the item Enter in the field runs, so it reads as highlighted while the field has focus. */
export const MENU_FILTER_TARGET_PROPS = {
  "data-menu-filter-target": "",
  className: "bg-muted text-foreground dark:bg-muted/50",
} as const;

/** What the menu shows when nothing matches. */
export function MenuFilterEmpty({ query }: { query: string }) {
  return (
    <div role="status" className="px-1.5 py-1.5 text-ui text-muted-foreground">
      No actions match “{query.trim()}”
    </div>
  );
}
