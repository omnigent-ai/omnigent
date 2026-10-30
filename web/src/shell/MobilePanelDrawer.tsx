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
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { useInertRef } from "@/hooks/useInertRef";
import { useIOSNativeKeyboardInset } from "@/hooks/useIOSNativeKeyboardInset";
import { cn } from "@/lib/utils";

const MobilePanelHeaderContext = createContext<
  { actions: HTMLElement | null; title: HTMLElement | null } | undefined
>(undefined);

/** Header destinations; undefined outside a drawer, targets null before mounting. */
export function useMobilePanelHeaderTarget() {
  return useContext(MobilePanelHeaderContext);
}

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
   * (e.g. ``SubagentsPanel``) don't keep running behind a closed drawer —
   * unless ``keepMounted`` is set.
   */
  children: ReactNode;
  /**
   * Keep ``children`` mounted while the drawer is closed. Opt-in, for content
   * that owns live work the user expects to continue once the drawer is
   * dismissed — a side chat's in-flight turn and unsent composer text, which
   * the desktop rail also keeps alive when collapsed. Leave unset for panels
   * whose only job is display: unmounting stops their polling.
   */
  keepMounted?: boolean;
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
 * @param keepMounted - Keep the body mounted while closed (live content).
 */
export function MobilePanelDrawer({
  open,
  title,
  onClose,
  children,
  testId,
  keepMounted = false,
}: MobilePanelDrawerProps) {
  // The iOS shell-lock (useIOSViewportLock) resizes only flow content inside
  // .app-shell, so this fixed overlay must reserve the docked keyboard itself;
  // otherwise its bottom content (the side-chat composer) stays under it.
  const keyboardInset = useIOSNativeKeyboardInset(open);
  const [headerTarget, setHeaderTarget] = useState<HTMLDivElement | null>(null);
  const [titleTarget, setTitleTarget] = useState<HTMLDivElement | null>(null);
  const headerTargets = useMemo(
    () => ({ actions: headerTarget, title: titleTarget }),
    [headerTarget, titleTarget],
  );
  const inertRef = useInertRef<HTMLElement>(!open);
  return (
    <aside
      ref={inertRef}
      data-testid={testId}
      data-state={open ? "open" : "closed"}
      style={keyboardInset > 0 ? { paddingBottom: keyboardInset } : undefined}
      className={cn(
        // `mobile-panel-drawer` is the hook the native shells use to inset the
        // drawer past the status bar / home indicator (see index.css); without
        // it the header sits under the notch and Close is untappable.
        "mobile-panel-drawer flex flex-col overflow-hidden bg-card transition-[translate] duration-150 ease-out",
        "fixed inset-0 z-50 md:hidden",
        open ? "translate-x-0 shadow-lg" : "translate-x-full shadow-none",
      )}
      aria-hidden={!open}
      data-collapsed={!open || undefined}
    >
      <header className="flex shrink-0 items-center justify-between border-border border-b pl-4 pr-2 py-2">
        <div ref={setTitleTarget} className="min-w-0 [&:has(>[data-mobile-panel-title])>h2]:hidden">
          <h2 className="font-medium text-ui">{title}</h2>
        </div>
        <div className="ml-auto flex items-center gap-1">
          <div ref={setHeaderTarget} className="flex items-center gap-1" />
          <Button type="button" variant="ghost" size="icon-sm" aria-label="Close" onClick={onClose}>
            <XIcon className="size-4" />
          </Button>
        </div>
      </header>
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <MobilePanelHeaderContext.Provider value={headerTargets}>
          {(open || keepMounted) && children}
        </MobilePanelHeaderContext.Provider>
      </div>
    </aside>
  );
}
