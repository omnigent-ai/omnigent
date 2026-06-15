// Mobile-only full-screen drawer for right-rail tab content that has no
// desktop push-panel of its own (Agents, Tasks). On desktop these live as
// tabs inside the workspace rail; a phone has no room for the rail, so the
// top-right session menu (the FAB dropdown) opens each tab's content here as
// a full-screen overlay — the same affordance the Files drawer uses.
//
// Layout contract matches `FilesPanelDrawer` / `TerminalsPanel`'s mobile
// shape: a fixed full-screen overlay that slides in from the right via
// `translate-x`, with a header (title + close) above the content.
//
// `md:hidden`: the drawer is never shown on desktop — the rail owns that
// content there — so even if `open` is left true across a viewport resize it
// can't collide with the rail.

import { XIcon } from "lucide-react";
import { type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface MobilePanelDrawerProps {
  /** Whether the drawer is slid in (visible) or off-screen. */
  open: boolean;
  /** Header title, e.g. ``"Agents"`` or ``"Tasks"``. */
  title: string;
  /**
   * Dismiss handler — fired by the header close button. (No Escape-key
   * listener: this drawer is `md:hidden`, so it only renders on mobile
   * viewports, where there's no Escape key to press.)
   */
  onClose: () => void;
  /**
   * Panel content. Mounted only while ``open`` so panels that poll
   * (e.g. ``SubagentsPanel``) don't keep running behind a closed drawer.
   */
  children: ReactNode;
  /** Optional test id applied to the root ``aside`` for assertions. */
  testId?: string;
}

/**
 * Full-screen, mobile-only push panel hosting a single rail tab's content.
 *
 * @param open - Whether the drawer is visible.
 * @param title - Header title text.
 * @param onClose - Called to dismiss the drawer.
 * @param children - The panel body, mounted only while open.
 * @param testId - Optional ``data-testid`` for the root element.
 */
export function MobilePanelDrawer({
  open,
  title,
  onClose,
  children,
  testId,
}: MobilePanelDrawerProps) {
  return (
    <aside
      data-testid={testId}
      data-state={open ? "open" : "closed"}
      className={cn(
        "flex flex-col overflow-hidden bg-card transition-[translate] duration-150 ease-out",
        "fixed inset-0 z-50 shadow-lg md:hidden",
        open ? "translate-x-0" : "translate-x-full",
      )}
      aria-hidden={!open}
      data-collapsed={!open || undefined}
      inert={!open}
    >
      <header className="flex shrink-0 items-center justify-between border-border border-b px-4 py-2">
        <h2 className="font-medium text-sm">{title}</h2>
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Close" onClick={onClose}>
          <XIcon className="size-4" />
        </Button>
      </header>
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">{open && children}</div>
    </aside>
  );
}
