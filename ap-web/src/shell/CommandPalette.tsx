// Global command palette (⌘K). Two command groups:
//
//   • Actions — static app commands (new chat, navigate, toggle panels,
//     keyboard shortcuts). Filtered client-side against the live query.
//   • Sessions — fuzzy session switching from the SAME server-search source the
//     sidebar uses (`useConversations(query)` → `GET /v1/sessions?search_query=`),
//     debounced. Not a static first page: a user with hundreds of sessions must
//     find any of them, which client-side filtering over one page cannot do.
//
// cmdk's own filtering is disabled (`shouldFilter={false}`): the server filters
// sessions, and we filter the (tiny, static) action list ourselves so both
// groups react to the same input.

import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "@/lib/routing";
import { useConversations } from "@/hooks/useConversations";
import { openKeyboardShortcuts } from "@/components/KeyboardShortcutsDialog";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { conversationDisplayLabel, getConversationAgentType } from "./sidebarNav";

export interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Flip the left (Conversations) sidebar — owned by AppShell. */
  onToggleLeftSidebar: () => void;
  /** Flip the right (Workspace) sidebar — owned by AppShell. */
  onToggleRightSidebar: () => void;
}

interface ActionCommand {
  id: string;
  label: string;
  /** Extra terms the client-side filter matches against (beyond the label). */
  keywords: string[];
  run: () => void;
}

/** Debounce matches the sidebar search (300ms) so keystrokes don't each fetch. */
const SEARCH_DEBOUNCE_MS = 300;

export function CommandPalette({
  open,
  onOpenChange,
  onToggleLeftSidebar,
  onToggleRightSidebar,
}: CommandPaletteProps) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");

  // Reset the query when the palette closes so it reopens clean.
  useEffect(() => {
    if (!open) {
      setQuery("");
      setDebouncedQuery("");
    }
  }, [open]);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  const close = (): void => onOpenChange(false);

  const actions = useMemo<ActionCommand[]>(
    () => [
      {
        id: "new-chat",
        label: "New chat",
        keywords: ["compose", "start", "new session"],
        run: () => navigate("/"),
      },
      {
        id: "go-inbox",
        label: "Go to Inbox",
        keywords: ["notifications", "comments", "needs response"],
        run: () => navigate("/inbox"),
      },
      {
        id: "go-settings",
        label: "Go to Settings",
        keywords: ["preferences", "configuration", "account"],
        run: () => navigate("/settings"),
      },
      {
        id: "toggle-left-sidebar",
        label: "Toggle conversations sidebar",
        keywords: ["panel", "left", "sessions list"],
        run: onToggleLeftSidebar,
      },
      {
        id: "toggle-right-sidebar",
        label: "Toggle workspace sidebar",
        keywords: ["panel", "right", "files", "terminal"],
        run: onToggleRightSidebar,
      },
      {
        id: "keyboard-shortcuts",
        label: "Keyboard shortcuts",
        keywords: ["help", "keys", "hotkeys"],
        run: openKeyboardShortcuts,
      },
    ],
    [navigate, onToggleLeftSidebar, onToggleRightSidebar],
  );

  const filteredActions = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (q === "") return actions;
    return actions.filter(
      (a) =>
        a.label.toLowerCase().includes(q) || a.keywords.some((k) => k.toLowerCase().includes(q)),
    );
  }, [actions, query]);

  // Archived excluded (matches the sidebar default). With an empty query this
  // shares AppShell's existing `useConversations()` cache entry, so an idle
  // palette costs no extra fetch; a search keys its own entry.
  const { data, isFetching } = useConversations(debouncedQuery, false);

  const sessions = useMemo(() => {
    const seen = new Set<string>();
    const out: { id: string; label: string; agent: string }[] = [];
    for (const page of data?.pages ?? []) {
      for (const c of page.data) {
        if (seen.has(c.id)) continue;
        seen.add(c.id);
        out.push({
          id: c.id,
          label: conversationDisplayLabel(c),
          agent: getConversationAgentType(c),
        });
      }
    }
    return out;
  }, [data]);

  const runAction = (action: ActionCommand): void => {
    close();
    action.run();
  };

  const goToSession = (id: string): void => {
    close();
    navigate(`/c/${id}`);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby={undefined}
        className="top-1/4 translate-y-0 overflow-hidden p-0"
        showCloseButton={false}
      >
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        {/* shouldFilter=false: the server filters sessions and we filter actions
            (see file header). vimBindings=false: keep Ctrl+K/J from doubling as
            list-nav on Win/Linux, where Ctrl+K is also the opener. */}
        <Command shouldFilter={false} vimBindings={false} label="Command palette">
          <CommandInput
            value={query}
            onValueChange={setQuery}
            placeholder="Search commands and sessions…"
            data-testid="command-palette-input"
          />
          <CommandList>
            <CommandEmpty>
              {isFetching && debouncedQuery ? "Searching…" : "No results found"}
            </CommandEmpty>
            {filteredActions.length > 0 && (
              <CommandGroup heading="Actions">
                {filteredActions.map((a) => (
                  <CommandItem key={a.id} value={`action:${a.id}`} onSelect={() => runAction(a)}>
                    <span className="flex-1 truncate text-left">{a.label}</span>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
            {sessions.length > 0 && (
              <CommandGroup heading="Sessions">
                {sessions.map((s) => (
                  <CommandItem key={s.id} value={s.id} onSelect={() => goToSession(s.id)}>
                    <span className="flex-1 truncate text-left">{s.label}</span>
                    <span className="ml-2 shrink-0 text-xs text-muted-foreground">{s.agent}</span>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
