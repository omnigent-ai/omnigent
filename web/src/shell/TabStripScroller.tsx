import {
  type ComponentProps,
  type ReactNode,
  type RefObject,
  useEffect,
  useLayoutEffect,
  useRef,
} from "react";

import { cn } from "@/lib/utils";
import { scrollViewportToShow } from "@/shell/WorkspaceTabsScroller";

/**
 * Scrolls the tab strip just far enough to show `tab`, or the larger entry (with its ×)
 * it sits in. Unlike `scrollIntoView`, it moves no other scroller: the rail slides in
 * from past the window's right edge, so revealing a tab there would shift the page.
 */
export function scrollTabIntoView(tab: HTMLElement | null): void {
  const entry = tab?.closest<HTMLElement>("[data-tab-strip-entry]") ?? tab;
  const viewport = entry?.closest<HTMLElement>("[data-tab-strip-viewport]");
  if (!entry || !viewport) return;
  // The visible width, without borders or scrollbar; the box's own width before layout.
  const width = viewport.clientWidth || viewport.getBoundingClientRect().width;
  scrollViewportToShow(viewport, entry.getBoundingClientRect(), width);
}

/** What ends a press, or shows the next focus isn't the press's own. */
const FORGET_PRESS_EVENTS = ["pointerup", "pointercancel", "click", "keydown"] as const;

/** How far the fade at a cut-off edge reaches into the strip. */
const FADE = "1.5rem";

/**
 * Before a held close removes a tab from `row`: on a scrolled strip, pin the row's width so
 * the scroll can't clamp and slide the tabs under the pointer. An unscrolled row isn't
 * pinned, so it shrinks and "+" follows its last tab at once.
 */
export function holdTabStripScroll(row: HTMLElement): void {
  const viewport = row.parentElement;
  if (!viewport || viewport.scrollLeft <= 0 || row.style.minWidth) return;
  row.style.minWidth = `${row.scrollWidth}px`;
}

/**
 * Scroll a row of tabs horizontally with a hidden scrollbar, so a narrow pane
 * keeps every tab reachable, and keep the selected tab in view as it resizes.
 */
export function TabStripScroller({
  children,
  className,
  contentClassName,
  fadeEdges = false,
  holdExtent = false,
  contentRef: givenContentRef,
  ...viewportProps
}: ComponentProps<"div"> & {
  children: ReactNode;
  contentClassName?: string;
  /** Fade the edge that has more tabs past it, so a cut-off row reads as scrollable. */
  fadeEdges?: boolean;
  /**
   * Keep the scroll where it is and don't reveal the selected tab, so tabs closed one after
   * another leave the next one under the pointer; a scrolled row also keeps its width.
   */
  holdExtent?: boolean;
  /** The row of tabs, for an owner that pins its width before a close lands. */
  contentRef?: RefObject<HTMLDivElement | null>;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const ownContentRef = useRef<HTMLDivElement>(null);
  const contentRef = givenContentRef ?? ownContentRef;
  const holding = useRef(holdExtent);
  holding.current = holdExtent;
  const revealSelectedRef = useRef<() => void>(() => {});

  // A held close keeps a scrolled row's width, so its scroll stays put.
  const wasHolding = useRef(false);
  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!content) return;
    if (holdExtent) {
      wasHolding.current = true;
      // An owner pins the width before the close; this only covers holds it didn't pin.
      holdTabStripScroll(content);
      return;
    }
    if (!wasHolding.current) return;
    wasHolding.current = false;
    content.style.minWidth = "";
    // The hold kept the selection where it was, pinned or not; show it now, even when
    // nothing resizes as the hold ends.
    revealSelectedRef.current();
  }, [holdExtent, contentRef]);

  useEffect(() => {
    const viewport = viewportRef.current;
    const content = contentRef.current;
    if (!viewport || !content) return;
    const wheel = (event: WheelEvent) => {
      if (viewport.scrollWidth <= viewport.clientWidth) return;
      // Sideways gestures scroll natively; Ctrl-wheel and pinches stay the page's zoom.
      if (event.ctrlKey || event.deltaX) return;
      const delta = event.deltaY;
      if (!delta) return;
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientWidth : 1;
      viewport.scrollLeft += delta * unit;
    };
    const reveal = scrollTabIntoView;
    const revealSelected = () => {
      if (holding.current) return;
      const selected = content.querySelector<HTMLElement>(
        '[aria-current="true"], [aria-current="page"], [aria-selected="true"]',
      );
      if (selected) reveal(selected);
    };
    revealSelectedRef.current = revealSelected;
    // A press focuses what's already under the pointer: revealing it would slide it away
    // before the release, and a close waits for the click. Only that press's own focus is
    // left alone, and any release, key or leaving the window forgets the press. A tap
    // focuses only after its release, so it lasts until the click instead.
    let pressed: Node | null = null;
    let pressedByTouch = false;
    const notePress = (event: PointerEvent) => {
      pressed = event.target instanceof Node ? event.target : null;
      pressedByTouch = event.pointerType === "touch" || event.pointerType === "pen";
    };
    const forgetPress = (event: Event) => {
      if (event.type === "pointerup" && pressedByTouch) return;
      pressed = null;
    };
    // Keyboard focus can rest on a tab that isn't selected; keep all of it in view too.
    const revealFocused = (event: FocusEvent) => {
      const target = event.target;
      const fromPress = pressed !== null && target instanceof Node && target.contains(pressed);
      pressed = null;
      if (!fromPress && target instanceof HTMLElement) reveal(target);
    };
    const updateFades = () => {
      if (!fadeEdges) return;
      const before = viewport.scrollLeft > 1;
      const after = viewport.scrollLeft + viewport.clientWidth < viewport.scrollWidth - 1;
      viewport.toggleAttribute("data-fade-start", before);
      viewport.toggleAttribute("data-fade-end", after);
      if (before || after) {
        const start = before ? "transparent" : "#000";
        const end = after ? "transparent" : "#000";
        viewport.style.setProperty(
          "--tab-strip-mask",
          `linear-gradient(to right, ${start}, #000 ${FADE}, #000 calc(100% - ${FADE}), ${end})`,
        );
      } else {
        viewport.style.removeProperty("--tab-strip-mask");
      }
    };
    // Keep the selected tab in view as the pane or tab list resizes, and when the selection moves.
    const observer = new ResizeObserver(() => {
      revealSelected();
      updateFades();
    });
    const selection = new MutationObserver(revealSelected);
    observer.observe(viewport);
    observer.observe(content);
    selection.observe(content, {
      subtree: true,
      attributeFilter: ["aria-current", "aria-selected"],
    });
    viewport.addEventListener("wheel", wheel, { passive: false });
    content.addEventListener("focusin", revealFocused);
    content.addEventListener("pointerdown", notePress, true);
    // Capture, so a key clears the press before a tab's own handler moves focus.
    for (const type of FORGET_PRESS_EVENTS) window.addEventListener(type, forgetPress, true);
    window.addEventListener("blur", forgetPress);
    viewport.addEventListener("scroll", updateFades, { passive: true });
    updateFades();
    return () => {
      observer.disconnect();
      selection.disconnect();
      viewport.removeEventListener("wheel", wheel);
      content.removeEventListener("focusin", revealFocused);
      content.removeEventListener("pointerdown", notePress, true);
      for (const type of FORGET_PRESS_EVENTS) window.removeEventListener(type, forgetPress, true);
      window.removeEventListener("blur", forgetPress);
      viewport.removeEventListener("scroll", updateFades);
    };
  }, [fadeEdges, contentRef]);

  return (
    <div
      ref={viewportRef}
      data-tab-strip-viewport=""
      {...viewportProps}
      className={cn(
        "min-w-0 overflow-x-auto overflow-y-hidden [scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
        fadeEdges && "[mask-image:var(--tab-strip-mask,none)]",
        className,
      )}
    >
      <div ref={contentRef} className={cn("flex w-max items-center", contentClassName)}>
        {children}
      </div>
    </div>
  );
}
