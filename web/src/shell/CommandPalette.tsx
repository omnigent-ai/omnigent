// The keyboard-driven overlay, in two modes on two separate keys — the split VS
// Code makes between its Command Palette and its Search view:
//
//   • "commands" (⌘K) — static app commands (new chat, navigate, toggle
//     panels), filtered client-side. No session lookup happens in this mode.
//   • "sessions" (⌘⇧F) — fuzzy session switching from the SAME server-search
//     source the sidebar's magnifier targets (`useConversations(query)` →
//     `GET /v1/sessions?search_query=`), debounced. Not a static first page: a
//     user with hundreds of sessions must find any of them, which client-side
//     filtering over one page cannot do. It matches chat content, not just
//     titles, and renders the matching line — closer to VS Code's ⌘⇧F than to
//     its file quick-open.
//
// Keeping the two apart means neither task pushes the other below the fold, and
// the sidebar's magnifier lands on a search surface rather than a command list.
//
// Both modes share this dialog, the debounce, and the cmdk config. Only the
// sessions mode mounts `SessionResults`, so ⌘K never issues a session request.
//
// cmdk's own filtering is disabled (`shouldFilter={false}`): the server filters
// sessions, and we filter the (tiny, static) action list ourselves.

import type React from "react";
import { useEffect, useMemo, useState } from "react";
import {
  CalendarClockIcon,
  InboxIcon,
  type LucideIcon,
  PanelLeftIcon,
  PanelRightIcon,
  SettingsIcon,
  SquarePenIcon,
  XIcon,
} from "lucide-react";
import { useNavigate } from "@/lib/routing";
import { useConversations } from "@/hooks/useConversations";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
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

/** Which task the overlay is open for. Set by whichever hotkey opened it. */
export type CommandPaletteMode = "commands" | "sessions";

export interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Commands (⌘K) or session search (⌘⇧F). */
  mode: CommandPaletteMode;
  /** Flip the left (Conversations) sidebar — owned by AppShell. */
  onToggleLeftSidebar: () => void;
  /** Flip the right (Workspace) sidebar — owned by AppShell. */
  onToggleRightSidebar: () => void;
}

interface ActionCommand {
  id: string;
  label: string;
  /** Mirrors the icon on the equivalent button elsewhere in the UI. */
  icon: LucideIcon;
  /** Extra terms the client-side filter matches against (beyond the label). */
  keywords: string[];
  run: () => void;
}

/** Debounce matches the sidebar search (300ms) so keystrokes don't each fetch. */
const SEARCH_DEBOUNCE_MS = 300;

/** Split `text` on case-insensitive occurrences of `query`, bolding the matches
    so a search hit is visible in the title / content snippet. Returns the raw
    text unchanged when the query is empty or doesn't occur (e.g. the snippet
    matched a stemmed form). */
function HighlightedText({ text, query }: { text: string; query: string }): React.ReactNode {
  const q = query.trim();
  if (!q) return text;
  // Escape regex metacharacters so a query like "a.b" matches literally.
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = text.split(new RegExp(`(${escaped})`, "gi"));
  const lower = q.toLowerCase();
  // Non-matches stay raw strings (React needs no key for those); each match is
  // keyed by its running offset so repeated terms get stable, unique keys
  // without leaning on the bare array index.
  let offset = 0;
  return parts.map((part) => {
    const at = offset;
    offset += part.length;
    return part.toLowerCase() === lower ? (
      <mark key={at} className="bg-transparent font-semibold text-foreground">
        {part}
      </mark>
    ) : (
      part
    );
  });
}

/** The sessions half. Mounted only in that mode, which is what keeps the ⌘K
    palette from issuing a session request. */
function SessionResults({
  query,
  onSelect,
}: {
  /** Debounced — this is what drives the server request. */
  query: string;
  onSelect: (id: string) => void;
}) {
  // includeArchived=true shares the sidebar's cache key; archived rows are
  // filtered out below so search only lists active sessions.
  const { data, isFetching } = useConversations(query, true);

  const sessions = useMemo(() => {
    const seen = new Set<string>();
    const out: { id: string; label: string; agent: string; snippet: string | null }[] = [];
    for (const page of data?.pages ?? []) {
      for (const c of page.data) {
        if (c.archived) continue;
        if (seen.has(c.id)) continue;
        seen.add(c.id);
        out.push({
          id: c.id,
          label: conversationDisplayLabel(c),
          agent: getConversationAgentType(c),
          // Present only when the match was in chat content (not the title);
          // the server omits it otherwise. Shown as a dimmed second line.
          snippet: c.search_snippet ?? null,
        });
      }
    }
    return out;
  }, [data]);

  return (
    <>
      <CommandEmpty>{isFetching && query ? "Searching…" : "No sessions found"}</CommandEmpty>
      {sessions.length > 0 && (
        <CommandGroup heading="Sessions">
          {sessions.map((s) => (
            <CommandItem
              key={s.id}
              value={s.id}
              onSelect={() => onSelect(s.id)}
              className="items-start"
            >
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="truncate text-left">
                  <HighlightedText text={s.label} query={query} />
                </span>
                {s.snippet && (
                  // Where the match was found in the chat body — the session is
                  // often unidentifiable from the title alone.
                  <span className="truncate text-left text-muted-foreground text-sm">
                    <HighlightedText text={s.snippet} query={query} />
                  </span>
                )}
              </div>
              <span className="ml-2 shrink-0 text-sm text-muted-foreground">{s.agent}</span>
            </CommandItem>
          ))}
        </CommandGroup>
      )}
    </>
  );
}

export function CommandPalette({
  open,
  onOpenChange,
  mode,
  onToggleLeftSidebar,
  onToggleRightSidebar,
}: CommandPaletteProps) {
  const navigate = useNavigate();
  const isMobile = useIsMobileViewport();
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");

  // Reset the query when the overlay closes so it reopens clean — including
  // when it reopens in the other mode, where the old query would be nonsense.
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
        icon: SquarePenIcon,
        keywords: ["compose", "start", "new session"],
        run: () => navigate("/"),
      },
      {
        id: "go-inbox",
        label: "Go to Inbox",
        icon: InboxIcon,
        keywords: ["notifications", "comments", "needs response"],
        run: () => navigate("/inbox"),
      },
      {
        id: "go-tasks",
        label: "Go to Automations",
        icon: CalendarClockIcon,
        keywords: ["scheduled", "recurring", "cron", "automation", "schedule"],
        run: () => navigate("/tasks"),
      },
      {
        id: "go-settings",
        label: "Go to Settings",
        icon: SettingsIcon,
        keywords: ["preferences", "configuration", "account"],
        run: () => navigate("/settings"),
      },
      {
        id: "toggle-left-sidebar",
        label: "Toggle conversations sidebar",
        icon: PanelLeftIcon,
        keywords: ["panel", "left", "sessions list"],
        run: onToggleLeftSidebar,
      },
      {
        id: "toggle-right-sidebar",
        label: "Toggle workspace sidebar",
        icon: PanelRightIcon,
        keywords: ["panel", "right", "files", "terminal"],
        run: onToggleRightSidebar,
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

  const runAction = (action: ActionCommand): void => {
    close();
    action.run();
  };

  const goToSession = (id: string): void => {
    close();
    navigate(`/c/${id}`);
  };

  const sessionsMode = mode === "sessions";
  const title = sessionsMode ? "Search sessions" : "Command palette";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby={undefined}
        // Mobile: a top-anchored full-screen sheet sized to the keyboard-aware
        // visible viewport (--omnigent-viewport-height), so the input and results
        // sit above the soft keyboard instead of a centered card whose lower half
        // hides behind it. Desktop keeps the centered command palette.
        className={cn(
          "overflow-hidden p-0",
          isMobile
            ? "inset-x-0 top-0 h-full max-h-full w-full max-w-full translate-x-0 translate-y-0 gap-0 rounded-none border-0 shadow-none"
            : "top-1/4 translate-y-0 sm:max-w-2xl",
        )}
        style={
          isMobile
            ? {
                top: 0,
                height: "var(--omnigent-viewport-height, 100dvh)",
                maxHeight: "var(--omnigent-viewport-height, 100dvh)",
                // Pad both insets: safe-top clears the notch, safe-bottom keeps
                // the last row above the home indicator when the keyboard is
                // closed (the visible-viewport height then spans the home bar).
                paddingTop: "var(--omnigent-safe-top, 0px)",
                paddingBottom: "var(--omnigent-safe-bottom, 0px)",
              }
            : undefined
        }
        showCloseButton={false}
      >
        <DialogTitle className="sr-only">{title}</DialogTitle>
        {/* shouldFilter=false: the server filters sessions and we filter actions
            (see file header). vimBindings=false: keep Ctrl+K/J from doubling as
            list-nav on Win/Linux, where Ctrl+K is also the opener. */}
        {/* Command's base class is `size-full`, so it already fills the sheet. */}
        <Command shouldFilter={false} vimBindings={false} label={title}>
          {isMobile ? (
            // Search field and an explicit close button share a top row; the
            // full-screen sheet has no ⌘K/Esc affordance the way the desktop
            // dialog does, so the X is how touch users dismiss it.
            <div className="flex items-center gap-1 p-1">
              <div className="min-w-0 flex-1">
                <CommandInput
                  value={query}
                  onValueChange={setQuery}
                  placeholder={sessionsMode ? "Search sessions" : "Run a command"}
                  data-testid="command-palette-input"
                />
              </div>
              <Button
                variant="ghost"
                size="icon-lg"
                className="shrink-0 rounded-full"
                onClick={close}
                aria-label="Close search"
              >
                <XIcon className="size-5 text-muted-foreground" />
              </Button>
            </div>
          ) : (
            <CommandInput
              value={query}
              onValueChange={setQuery}
              placeholder={sessionsMode ? "Search sessions" : "Run a command"}
              data-testid="command-palette-input"
            />
          )}
          <CommandList className={isMobile ? "max-h-none flex-1" : undefined}>
            {sessionsMode ? (
              <SessionResults query={debouncedQuery} onSelect={goToSession} />
            ) : (
              <>
                <CommandEmpty>No commands found</CommandEmpty>
                {filteredActions.length > 0 && (
                  <CommandGroup heading="Actions">
                    {filteredActions.map((a) => {
                      const Icon = a.icon;
                      return (
                        <CommandItem
                          key={a.id}
                          value={`action:${a.id}`}
                          onSelect={() => runAction(a)}
                        >
                          <Icon />
                          <span className="flex-1 truncate text-left">{a.label}</span>
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                )}
              </>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
