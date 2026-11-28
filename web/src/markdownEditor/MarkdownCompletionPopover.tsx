// The `@` and `#` suggestions beside the caret, drawn with the shared popover and command list.
// The textarea keeps focus and its selection throughout: the list never takes focus, its arrows
// and picks come from the textarea's keys, and a pointer pick doesn't move the caret.

import { UsersIcon } from "lucide-react";
import { type RefObject, useEffect, useMemo, useState } from "react";

import { Command, CommandEmpty, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import { PullRequestStateIcon } from "@/shell/PullRequestListRows";
import { pullRequestListState } from "@/shell/pullRequestListModel";

import { textareaCaretRect } from "./textareaCaret";
import type { MarkdownCompletion, MarkdownCompletionItem } from "./useMarkdownCompletion";

function ItemContent({ item }: { item: MarkdownCompletionItem }) {
  if (item.kind === "person") {
    const { person } = item;
    return (
      <>
        {person.kind === "team" && <UsersIcon aria-hidden className="size-3.5 shrink-0" />}
        <span className="truncate font-medium">@{person.login}</span>
        {person.name && <span className="truncate text-muted-foreground">{person.name}</span>}
      </>
    );
  }
  const { pullRequest } = item;
  return (
    <>
      <PullRequestStateIcon
        state={pullRequestListState({ state: pullRequest.state, is_draft: pullRequest.isDraft })}
      />
      <span className="shrink-0 font-medium tabular-nums">#{pullRequest.number}</span>
      <span className="truncate text-muted-foreground">{pullRequest.title}</span>
    </>
  );
}

const EMPTY_TEXT = {
  person: "No one matches",
  pullRequest: "No pull requests match",
} as const;

/** The list's element ids, for the textarea's `aria-controls` and `aria-activedescendant`. */
export interface CompletionListbox {
  id: string;
  activeId: string | undefined;
}

export function MarkdownCompletionPopover({
  completion,
  textareaRef,
  onListbox,
}: {
  completion: MarkdownCompletion;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  /** Told the list's ids as they change, and null once it closes. */
  onListbox: (listbox: CompletionListbox | null) => void;
}) {
  // The popover's content mounts a commit after it opens, so its list is found once it's there.
  const [content, setContent] = useState<HTMLDivElement | null>(null);
  const { token, items, loading, active } = completion;
  // Anchored at the trigger, so the list stays put as the query grows.
  const anchor = useMemo(
    () => ({
      current: {
        getBoundingClientRect: () => {
          const textarea = textareaRef.current;
          return textarea && token ? textareaCaretRect(textarea, token.start) : new DOMRect();
        },
      },
    }),
    [textareaRef, token],
  );
  // The command list names its own ids and marks the active item once it has settled on it, so
  // the textarea's pointers to them follow those marks.
  useEffect(() => {
    const list = content?.querySelector<HTMLElement>("[cmdk-list]");
    if (!list) return;
    const report = () =>
      onListbox({
        id: list.id,
        activeId: list.querySelector<HTMLElement>('[cmdk-item][data-selected="true"]')?.id,
      });
    report();
    const observer = new MutationObserver(report);
    observer.observe(list, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["data-selected"],
    });
    return () => {
      observer.disconnect();
      onListbox(null);
    };
  }, [content, onListbox]);
  return (
    <Popover open={token !== null}>
      <PopoverAnchor virtualRef={anchor} />
      <PopoverContent
        align="start"
        side="bottom"
        sideOffset={20}
        className="w-80 p-0"
        data-testid="markdown-completion"
        onOpenAutoFocus={(event) => event.preventDefault()}
        onCloseAutoFocus={(event) => event.preventDefault()}
        // A click in the textarea moves the caret; it doesn't close what the caret still ends.
        onInteractOutside={(event) => {
          if (event.target === textareaRef.current) event.preventDefault();
          else completion.dismiss();
        }}
        onEscapeKeyDown={(event) => event.preventDefault()}
      >
        <div ref={setContent} className="contents">
          <Command
            shouldFilter={false}
            value={active}
            onValueChange={completion.setActive}
            className="rounded-lg! p-1"
          >
            <CommandList label={token?.kind === "person" ? "People" : "Pull requests"}>
              {items.map((item) => (
                <CommandItem
                  key={item.key}
                  value={item.key}
                  // Keeps the textarea's focus and caret, so the pick lands where the token is.
                  onMouseDown={(event) => event.preventDefault()}
                  onSelect={() => completion.pick(item)}
                  className="gap-2"
                >
                  <ItemContent item={item} />
                </CommandItem>
              ))}
              {items.length === 0 &&
                (loading ? (
                  <div className="flex items-center gap-2 px-2 py-2 text-ui text-muted-foreground">
                    <Spinner className="size-3.5" />
                    Searching…
                  </div>
                ) : (
                  token && <CommandEmpty className="py-2">{EMPTY_TEXT[token.kind]}</CommandEmpty>
                ))}
            </CommandList>
          </Command>
        </div>
      </PopoverContent>
    </Popover>
  );
}
