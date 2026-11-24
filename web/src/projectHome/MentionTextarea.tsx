import { useId, useRef, useState, type ComponentProps, type KeyboardEvent } from "react";
import { UserRoundIcon } from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { insertMention, mentionQueryAt } from "./personaMentions";
import { useProjectIdentities } from "./useProjectIdentities";

const MAX_SUGGESTIONS = 8;

type MentionTextareaProps = Omit<ComponentProps<typeof Textarea>, "value" | "onChange"> & {
  projectId: string;
  value: string;
  onValueChange: (value: string) => void;
};

/** A textarea that offers the project's personas when the user types `@`. */
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
  const needle = query?.query.toLowerCase() ?? "";
  const suggestions = query
    ? (identities.data ?? [])
        .filter((identity) => identity.name.toLowerCase().includes(needle))
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
    onValueChange(next.text);
    setQuery(null);
    requestAnimationFrame(() => {
      target.focus();
      target.setSelectionRange(next.caret, next.caret);
    });
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
        choose(suggestions[Math.min(active, suggestions.length - 1)].name);
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
      {open && (
        <ul
          id={listId}
          role="listbox"
          aria-label="Mention a persona"
          data-testid="persona-mention-suggestions"
          className="absolute left-2 top-full z-50 mt-1 min-w-44 overflow-hidden rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-md"
        >
          {suggestions.map((identity, index) => (
            <li
              key={identity.id}
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
                choose(identity.name);
              }}
              onMouseEnter={() => setActive(index)}
            >
              <UserRoundIcon className="size-3.5 text-muted-foreground" />
              <span className="truncate">{identity.name}</span>
              {identity.builtin && (
                <span className="ml-auto text-xs text-muted-foreground">built in</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
