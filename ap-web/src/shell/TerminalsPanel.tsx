// Right-side panel that hosts the xterm.js views for the
// conversation's open terminals. Triggered from `SessionRail` (or the
// mobile FAB).
//
// Layout contract (matches the left `Sidebar`'s responsive shape):
//
//   - **Mobile (`< md`)**: fixed full-screen overlay. Slides in from
//     the right via `translate-x`. Covers the chat edge-to-edge.
//   - **Desktop (`md+`)**: static flex sibling. Width is set instantly
//     via the `useResizablePanel` hook (drag-to-resize); no CSS width
//     transition. The panel pushes the chat surface left.
//
// Navigation model: a persistent horizontal split (VS Code-style).
//   - **Left**: scrollable list of all terminals — session (primary),
//     name (muted), status badge. Clicking a row selects it; clicking
//     the active row deselects (returns to list-only).
//   - **Right**: xterm for the selected terminal, mount-gated by
//     `expanded` so fit() reads settled dimensions. Hidden (and the
//     divider removed) when no terminal is selected.
//
// Mount gating: the `TerminalView` xterm + WebSocket are only
// mounted after the panel's open animation completes. On mobile the
// slide-in transition (translate-x) needs ~200ms to settle; on
// desktop width is set instantly but the timeout is harmless.

import { TerminalIcon, XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { TerminalView } from "@/components/blocks/TerminalView";
import { Button } from "@/components/ui/button";
import { useResizablePanel } from "@/hooks/useResizablePanel";
import { terminalTabKey } from "@/hooks/useTerminals";
import { cn } from "@/lib/utils";
import { NewTerminalButton } from "./NewTerminalButton";
import { TerminalStatusBadge } from "./terminalStatus";
import { useTerminalSplit } from "./useTerminalSplit";

interface TerminalsPanelProps {
  open: boolean;
  conversationId: string;
  /**
   * Tab key for the terminal to activate on open, e.g.
   * ``"terminal:terminal_bash_s1"``. Always a specific terminal —
   * the panel is only opened from a row click, never from a
   * panel-wide trigger. `null` when ``open`` is false.
   */
  initialTerminalKey: string | null;
  onClose: () => void;
  /**
   * Fluid mode: when true, the panel takes all remaining flex space
   * instead of a fixed resize-controlled width. Used by AppShell's TUI
   * mode where the panel sits between the (hidden) main column and the
   * file browser, and there's no neighbor to resize against. No resize
   * handle is rendered. Default: false (legacy push-panel behavior).
   */
  fluid?: boolean;
  /**
   * When true, attach terminals read-only — the viewer can watch but
   * not type. Set for non-owners: a shared PTY's keystrokes carry no
   * per-user identity, so only the owner may drive it (the server
   * enforces this and refuses a non-owner write attach). Default false
   * (owner / single-user).
   */
  readOnly?: boolean;
}

export function TerminalsPanel({
  open,
  conversationId,
  initialTerminalKey,
  onClose,
  fluid = false,
  readOnly = false,
}: TerminalsPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const ref = useRef<HTMLElement>(null);
  const {
    terminals,
    activeKey,
    setActiveKey,
    activeTerminal,
    getStatus,
    setTerminalConnectionState,
    markTerminalActive,
    listWidth,
    splitRef,
    columnHandleProps,
  } = useTerminalSplit(conversationId);
  const { panelWidth, handleProps, isDesktop } = useResizablePanel(open);
  const [prevOpen, setPrevOpen] = useState(open);
  if (open !== prevOpen) {
    setPrevOpen(open);
    if (!open) setExpanded(false);
  }

  useEffect(() => {
    if (!open) return;
    const t = window.setTimeout(() => setExpanded(true), 180);
    return () => window.clearTimeout(t);
  }, [open]);

  // When the panel opens, jump to the requested terminal. When it
  // closes, reset so the next open picks up the latest prop.
  useEffect(() => {
    if (!open) {
      setActiveKey(null);
      return;
    }
    if (initialTerminalKey) {
      setActiveKey(initialTerminalKey);
    }
  }, [open, initialTerminalKey, setActiveKey]);

  // Esc dismisses — common panel convention.
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Auto-close when there's nothing left to view.
  useEffect(() => {
    if (open && terminals.length === 0) onClose();
  }, [open, terminals.length, onClose]);

  useEffect(() => {
    if (ref.current) {
      if (open) {
        ref.current.removeAttribute("inert");
      } else {
        ref.current.setAttribute("inert", "");
      }
    }
  }, [open]);

  return (
    <aside
      ref={ref}
      data-testid="terminals-panel"
      data-state={open ? "open" : "closed"}
      data-expanded={expanded}
      data-fluid={fluid}
      style={fluid ? undefined : { width: panelWidth }}
      className={cn(
        "flex flex-col overflow-hidden bg-card transition-[translate,border-color,border-width] duration-150 ease-out",
        "fixed inset-0 z-50 shadow-lg",
        open ? "translate-x-0" : "translate-x-full",
        "md:relative md:inset-auto md:z-auto md:shadow-none md:translate-x-0",
        fluid ? "md:flex-1" : "md:shrink-0",
        open ? "md:border-border md:border-l" : "md:w-0 md:border-l-0",
      )}
      aria-hidden={!open}
      data-collapsed={!open || undefined}
    >
      {/* Resize handle — desktop only. Suppressed in fluid mode. */}
      {isDesktop && !fluid && (
        <div
          {...handleProps}
          className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
        />
      )}

      <header className="flex shrink-0 items-center justify-between border-border border-b px-4 py-2">
        <h2 className="font-medium text-sm">Shells</h2>
        <div className="flex items-center gap-1">
          {/* Renders only when the agent's spec declares terminals. */}
          <NewTerminalButton
            conversationId={conversationId}
            onCreated={(key) => setActiveKey(key)}
          />
          <Button type="button" variant="ghost" size="icon-sm" aria-label="Close" onClick={onClose}>
            <XIcon className="size-4" />
          </Button>
        </div>
      </header>

      {/* Split content.
          Mobile: flex-col — list on top, xterm below.
          Desktop: flex-row — list on left, xterm on right. */}
      <div ref={splitRef as React.RefObject<HTMLDivElement>} className="flex min-h-0 flex-1 flex-col md:flex-row overflow-hidden">
        {/* List panel */}
        <div
          className={cn(
            "relative flex shrink-0 flex-col overflow-y-auto py-1",
            activeTerminal
              ? "border-b border-border md:border-b-0 md:border-r"
              : "flex-1",
          )}
          // Width only meaningful on desktop (horizontal split).
          style={activeTerminal && isDesktop ? { width: listWidth } : undefined}
        >
          {/* Column resize handle — desktop only */}
          {activeTerminal && isDesktop && (
            <div
              {...columnHandleProps}
              className="absolute inset-y-0 right-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
            />
          )}
          {terminals.map((t) => {
            const key = terminalTabKey(t);
            const isActive = key === activeKey;
            return (
              <button
                key={key}
                type="button"
                className={cn(
                  "flex w-full items-center gap-2 px-3 py-1.5 text-left",
                  isActive ? "bg-accent" : "hover:bg-accent/60",
                )}
                onClick={() => setActiveKey(isActive ? null : key)}
              >
                <TerminalIcon className="size-3.5 shrink-0 text-muted-foreground" />
                {t.session && (
                  <span className="shrink-0 text-xs font-medium">{t.session}</span>
                )}
                <span className="truncate text-xs text-muted-foreground/70">{t.name}</span>
                <span className="flex-1" />
                <TerminalStatusBadge status={getStatus(t)} />
              </button>
            );
          })}
        </div>

        {/* xterm — only rendered when a terminal is selected */}
        <div className={cn("min-h-0 min-w-0 flex-1", !activeTerminal && "hidden")}>
          {expanded && activeTerminal ? (
            <div key={activeTerminal.id} className="flex h-full flex-col">
              <TerminalView
                sessionId={conversationId}
                terminalId={activeTerminal.id}
                readOnly={readOnly}
                onStateChange={(state) => {
                  setTerminalConnectionState(activeTerminal.id, state);
                }}
                onActivity={() => markTerminalActive(activeTerminal.id)}
              />
            </div>
          ) : (
            <div className="flex-1" />
          )}
        </div>
      </div>
    </aside>
  );
}
