import {
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ComponentProps,
  type CSSProperties,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { PersonaAvatar } from "@/components/PersonaAvatar";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { insertMention, mentionQueryAt } from "./personaMentions";
import { useProjectIdentities } from "./useProjectIdentities";
import { useProjectPeople } from "./useProjectPeople";

const MAX_SUGGESTIONS = 8;

type MentionTextareaProps = Omit<ComponentProps<typeof Textarea>, "value" | "onChange"> & {
  projectId: string;
  value: string;
  onValueChange: (value: string) => void;
};

/** One autocomplete row: a persona, or a project member to cc. */
interface MentionOption {
  key: string;
  name: string;
  insert: string;
  detail?: string;
  builtin?: boolean;
}

/** A textarea that offers the project's personas and members when the user types `@`. */
export function MentionTextarea({
  projectId,
  value,
  onValueChange,
  onKeyDown,
  ...props
}: MentionTextareaProps) {
  const listId = useId();
  // Textarea doesn't forward refs, so find it through the wrapper.
  const containerRef = useRef<HTMLDivElement>(null);
  const identities = useProjectIdentities(projectId);
  const [query, setQuery] = useState<{ start: number; query: string } | null>(null);
  const [active, setActive] = useState(0);
  const pendingCaret = useRef<number | null>(null);
  useLayoutEffect(() => {
    const target = containerRef.current?.querySelector("textarea");
    if (pendingCaret.current === null || !target) return;
    target.focus();
    target.setSelectionRange(pendingCaret.current, pendingCaret.current);
    pendingCaret.current = null;
  }, [value]);
  const personas = identities.data ?? [];
  const people = useProjectPeople(
    projectId,
    personas.map((identity) => identity.name),
  );
  const options: MentionOption[] = [
    ...personas.map((identity) => ({
      key: `persona:${identity.id}`,
      name: identity.name,
      insert: identity.name,
      builtin: identity.builtin,
    })),
    ...people.map((person) => ({
      key: `person:${person.userId}`,
      name: person.name,
      insert: person.mention,
      detail: person.userId,
    })),
  ];
  const needle = query?.query.toLowerCase() ?? "";
  const matches = (option: MentionOption) =>
    option.name.toLowerCase().includes(needle) ||
    (option.detail?.toLowerCase().includes(needle) ?? false);
  const suggestions = query
    ? options
        .filter(matches)
        .sort(
          (a, b) =>
            Number(!a.name.toLowerCase().startsWith(needle)) -
            Number(!b.name.toLowerCase().startsWith(needle)),
        )
        .slice(0, MAX_SUGGESTIONS)
    : [];
  const open = suggestions.length > 0;

  const track = (target: HTMLTextAreaElement) => {
    const next = mentionQueryAt(target.value, target.selectionStart ?? target.value.length);
    setQuery(next);
    if (next?.query !== query?.query) setActive(0);
  };
  const choose = (name: string) => {
    const target = containerRef.current?.querySelector("textarea");
    if (!target || !query) return;
    const caret = target.selectionStart ?? value.length;
    const next = insertMention(value, caret, query.start, name);
    // Placed in the same commit as the new value, before any further keystroke lands.
    pendingCaret.current = next.caret;
    onValueChange(next.text);
    setQuery(null);
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (open && !event.metaKey && !event.ctrlKey) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        setActive((current) => (current + step + suggestions.length) % suggestions.length);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        event.stopPropagation();
        choose(suggestions[Math.min(active, suggestions.length - 1)].insert);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setQuery(null);
        return;
      }
    }
    onKeyDown?.(event);
  };

  return (
    <div ref={containerRef} className="relative">
      <Textarea
        {...props}
        value={value}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        onChange={(event) => {
          onValueChange(event.target.value);
          track(event.target);
        }}
        onSelect={(event) => track(event.currentTarget)}
        onBlur={(event) => {
          setQuery(null);
          props.onBlur?.(event);
        }}
        onKeyDown={handleKeyDown}
      />
      {/* Portaled and fixed above the box: the composer's scrolling footer would clip it. */}
      {open &&
        createPortal(
          <ul
            id={listId}
            role="listbox"
            aria-label="Mention a persona or person"
            data-testid="persona-mention-suggestions"
            style={listPosition(containerRef.current)}
            className="fixed z-50 min-w-44 overflow-hidden rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-md"
          >
            {suggestions.map((option, index) => (
              <li
                key={option.key}
                role="option"
                aria-selected={index === active}
                data-testid="persona-mention-option"
                className={cn(
                  "flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm",
                  index === active && "bg-accent text-accent-foreground",
                )}
                // Keep focus in the textarea so the caret position survives the click.
                onMouseDown={(event) => {
                  event.preventDefault();
                  choose(option.insert);
                }}
                onMouseEnter={() => setActive(index)}
              >
                <PersonaAvatar name={option.name} size="xs" />
                <span className="truncate">{option.name}</span>
                {option.builtin && (
                  <span className="ml-auto text-xs text-muted-foreground">built in</span>
                )}
                {option.detail && (
                  <span className="ml-auto truncate text-xs text-muted-foreground">
                    {option.detail}
                  </span>
                )}
              </li>
            ))}
          </ul>,
          document.body,
        )}
    </div>
  );
}

function listPosition(anchor: HTMLElement | null): CSSProperties {
  const rect = anchor?.getBoundingClientRect();
  if (!rect) return {};
  return { left: rect.left + 8, bottom: window.innerHeight - rect.top + 4 };
}
