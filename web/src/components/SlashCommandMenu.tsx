import { useEffect, useRef } from "react";
import { CommandIcon, WandSparklesIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Built-in slash commands the web UI recognises directly. Each entry
 * maps a command name (lower-case, with leading slash) to a
 * human-readable description shown in the suggestions menu and
 * ``/help`` listing. The full set surfaced in the menu also
 * includes ``/skill-name`` entries derived from the session's
 * available skills (see ``buildSlashCommandMap`` in ChatPage). Lives
 * here (not ChatPage) so the menu can section rows into Commands vs
 * Skills without importing ChatPage — which imports NewChatDialog,
 * which imports this menu.
 */
export const BUILTIN_SLASH_COMMANDS: Record<string, string> = {
  "/compact": "Compact conversation context to free up space",
  "/context": "Show context window usage for this session",
  "/effort": "Set reasoning effort: /effort low | medium | high | default",
  "/model": "Switch the model for this session: /model <name> | default",
  "/help": "Show available slash commands",
};

// First token must read as a command name (`/cross-review`,
// `/dev-productivity:simplify`) — letters/digits then word chars, `:`, `-`.
// The leading `/` is the only slash allowed IN THE NAME, so file paths like
// `/etc/hosts` don't match — but anything may follow the first whitespace,
// so args carrying paths or URLs (`/review-pr https://github.com/...`) do.
const SLASH_COMMAND_RE = /^\/[A-Za-z0-9][\w:-]*(\s|$)/;

/**
 * True when a user message is a slash-command invocation typed in the
 * composer. The single command-shape definition shared by the in-session
 * composer's submit routing + highlight overlay and the landing
 * composer's skill matching — one guard, so the surfaces can't diverge
 * on what "reads as a command".
 */
export function isSlashCommandText(text: string): boolean {
  return SLASH_COMMAND_RE.test(text.trim());
}

interface SlashCommandMenuProps {
  /** The text typed after the leading ``/``, used to filter suggestions. */
  query: string;
  /** Index of the currently highlighted suggestion (-1 = none). */
  activeIndex: number;
  /** Called when the user selects a command (click or keyboard). */
  onSelect: (cmd: string) => void;
  /**
   * Full command map to filter against — built-ins merged with the
   * session's available skills. Order is preserved by insertion and
   * drives the caller's keyboard navigation, so built-ins must come
   * first (skills after) for the section split below to stay aligned
   * with the flat match order.
   */
  commands: Record<string, string>;
}

/** One filtered menu row, carrying its index in the flat match order. */
interface MenuRow {
  /** Slash-prefixed command name, e.g. ``"/review-pr"``. */
  name: string;
  /** One-line description shown in the detail card when active. */
  description: string;
  /** Index into the flat ``matches`` list — the caller's keyboard index. */
  flatIndex: number;
}

/** A row inside a section list ("Commands" or "Skills"). */
function MenuRowButton({
  row,
  active,
  onSelect,
}: {
  /** The row to render. */
  row: MenuRow;
  /** Whether this row is the keyboard-highlighted one. */
  active: boolean;
  /** Selection callback, called with the slash-prefixed name. */
  onSelect: (cmd: string) => void;
}) {
  const isBuiltin = row.name in BUILTIN_SLASH_COMMANDS;
  // Wand in pink for skills (per design feedback — distinct from the
  // info-blue slash-command tint, and from plain Sparkles which marks
  // thinking/reasoning blocks), the ⌘ glyph in slate for built-ins.
  const Icon = isBuiltin ? CommandIcon : WandSparklesIcon;
  return (
    <button
      type="button"
      data-testid={`slash-menu-item-${row.name.slice(1)}`}
      data-active={active ? "true" : undefined}
      className={cn(
        "flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[13px] text-foreground hover:bg-accent",
        active && "bg-accent",
      )}
      // preventDefault keeps the textarea focused while the user clicks.
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => onSelect(row.name)}
    >
      <Icon
        className={cn(
          "size-3.5 shrink-0",
          isBuiltin ? "text-slate-500 dark:text-slate-400" : "text-pink-500 dark:text-pink-400",
        )}
      />
      <span className="truncate">{row.name}</span>
    </button>
  );
}

/**
 * Floating suggestions menu rendered above the composer when the user
 * starts a slash command, shared by the in-session composer (ChatPage)
 * and the new-chat landing composer (NewChatDialog). Cursor-style
 * layout: a narrow panel with "Commands" / "Skills" section headers and
 * icon + name rows, plus a detail card beside the panel showing the
 * highlighted entry's full description (hidden on small screens).
 * Only commands whose name starts with the current query are shown.
 * Positioned via ``absolute bottom-full`` relative to the rounded
 * composer container. Exported for direct unit testing.
 */
export function SlashCommandMenu({
  query,
  activeIndex,
  onSelect,
  commands,
}: SlashCommandMenuProps) {
  const lower = query.toLowerCase();
  const matches = Object.entries(commands).filter(([name]) => name.slice(1).startsWith(lower));
  const listRef = useRef<HTMLDivElement>(null);
  // Keep the keyboard-highlighted row visible as the user arrows past the
  // visible window of this capped-height, scrollable list. Without this the
  // selection silently moves off-screen. Mirrors the WorkspacePathField
  // dropdown pattern (``data-active`` + ``scrollIntoView({ block: "nearest" })``).
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    listRef.current.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);
  if (matches.length === 0) return null;

  // Split into sections WITHOUT reordering: the flat match order drives the
  // caller's keyboard index, and built-ins are inserted before skills (the
  // landing menu passes skills only), so the partition is contiguous and
  // rendering Commands above Skills preserves the visual = keyboard order.
  const rows: MenuRow[] = matches.map(([name, description], flatIndex) => ({
    name,
    description,
    flatIndex,
  }));
  const builtinRows = rows.filter((r) => r.name in BUILTIN_SLASH_COMMANDS);
  const skillRows = rows.filter((r) => !(r.name in BUILTIN_SLASH_COMMANDS));
  const active = activeIndex >= 0 ? rows[activeIndex] : undefined;

  const sectionHeader = (label: string) => (
    <div className="px-2 pb-0.5 pt-1.5 text-[11px] font-medium text-muted-foreground">{label}</div>
  );

  return (
    <div className="absolute bottom-full left-0 z-10 mb-2 flex items-end gap-2">
      <div className="w-64 shrink-0 overflow-hidden rounded-xl border border-border bg-popover shadow-lg">
        <div ref={listRef} className="max-h-80 overflow-y-auto p-1">
          {builtinRows.length > 0 && sectionHeader("Commands")}
          {builtinRows.map((row) => (
            <MenuRowButton
              key={row.name}
              row={row}
              active={row.flatIndex === activeIndex}
              onSelect={onSelect}
            />
          ))}
          {skillRows.length > 0 && sectionHeader("Skills")}
          {skillRows.map((row) => (
            <MenuRowButton
              key={row.name}
              row={row}
              active={row.flatIndex === activeIndex}
              onSelect={onSelect}
            />
          ))}
        </div>
      </div>
      {/* Detail card for the highlighted entry — descriptions live here
          (not inline) so long skill blurbs get room to breathe. Hidden on
          small screens where there's no room beside the panel. */}
      {active && (
        <div
          data-testid="slash-menu-detail"
          className="hidden max-h-80 w-80 shrink-0 overflow-y-auto rounded-xl border border-border bg-popover p-3 shadow-lg md:block"
        >
          <p className="font-mono text-xs font-medium text-foreground">{active.name}</p>
          <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
            {active.description}
          </p>
        </div>
      )}
    </div>
  );
}
