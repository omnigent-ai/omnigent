// A vertical minimap of the conversation: one tick per turn (a user message
// and a preview of the reply that followed). Hovering a tick reveals a preview
// box; clicking scrolls the transcript to that user message. The rail scrolls
// independently of the transcript, fades at its top edge to signal there's
// more above, and pages in older history when scrolled near that top.

import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { scrollToUserMessage } from "@/hooks/useUserMessageNav";
import { useChatStore } from "@/store/chatStore";

/** One rail tick: a user turn plus a preview of the reply it drew. */
export interface Turn {
  /** The user bubble's itemId — the transcript scroll anchor. */
  itemId: string;
  /** The user message text, for the hover preview's heading. */
  userText: string;
  /** Leading text of the assistant reply, for the hover preview's body. */
  responsePreview: string;
}

/** Scroll container for the rail; also drives its own scroll for fetch-on-top. */
type Scroller = { el: HTMLElement };

// Rail scrollTop below which we treat the user as "near the top" and page in
// older history — mirrors HistoryAutoLoader's transcript threshold.
const FETCH_TOP_PX = 40;

// How many ticks to show on first load. The transcript's initial history
// window is only "back to the previous user message" (a turn or two), so the
// rail eagerly pages older history up to this many turns — then the user
// scrolls the rail up for more. Matches the "≤20 ticks initially" spec.
const INITIAL_TURNS = 20;

// Width of the top/bottom fade ramps, in px. Single source of truth: fed to
// the CSS mask via the --turn-rail-fade variable AND used as the usable-edge
// inset in the thumb-tracking math below, so the mask and the math stay in
// lockstep.
const FADE = 32;

export function TurnRail({
  turns,
  scroller,
  hasMoreHistory,
  loadingMoreHistory,
}: {
  turns: readonly Turn[];
  scroller: Scroller | null;
  hasMoreHistory: boolean;
  loadingMoreHistory: boolean;
}) {
  const flashUserMessage = useChatStore((s) => s.flashUserMessage);
  const railRef = useRef<HTMLDivElement | null>(null);
  const tickRefs = useRef(new Map<string, HTMLButtonElement>());
  // True while the pointer is over the rail, i.e. the user is browsing ticks.
  // Suppresses the thumb-tracking auto-scroll so a history fetch (or any other
  // `turns` change) can't yank the rail back to the transcript's visible run
  // while the user is scrolling it.
  const interactingRef = useRef(false);
  // Cursor position at which we last accepted a hover. Scrolling drags ticks
  // under a stationary cursor, firing onMouseEnter on each — but the cursor
  // itself hasn't moved. Comparing against this lets us tell a real hover
  // (cursor moved) from a scroll-induced one (same position), independent of
  // whether the browser fires mouseenter before or after the scroll event.
  const hoverPointRef = useRef({ x: -1, y: -1 });
  // Last pointer position over the rail, used to pick the settle tick. Starts
  // off-screen so a settle before any pointermove resolves to no element.
  const pointerRef = useRef({ x: -1, y: -1 });
  // itemIds of the turns whose messages are currently on screen. Their ticks
  // read as active (black) when the user isn't hovering the rail.
  const [visibleIds, setVisibleIds] = useState<ReadonlySet<string>>(() => new Set());
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  // Vertical center of the hovered tick within the rail's own coordinate
  // space, so the (rail-relative) preview box tracks it as the rail scrolls.
  const [previewTop, setPreviewTop] = useState(0);
  // Gate the first paint until the eager back-fill settles, so the rail fades
  // in once at its full run of ticks instead of flashing the initial 2-turn
  // window before older history lands. Latches true and stays true — later
  // scroll-up loads must not re-hide it.
  const [revealed, setRevealed] = useState(false);

  const scrollEl = scroller?.el ?? null;

  // Track which turns' messages are on screen, so their ticks read as active.
  // A turn spans from its own user-message anchor down to the next turn's
  // anchor (its reply); a turn is visible when that span overlaps the
  // viewport. rAF-throttled — scroll fires far faster than we need to
  // recompute, and reading anchor rects forces layout.
  useEffect(() => {
    if (!scrollEl) return;
    let frame = 0;
    const recompute = () => {
      frame = 0;
      const view = scrollEl.getBoundingClientRect();
      // Anchor top for each turn (skip turns whose message isn't in the DOM).
      const tops = turns.map((turn) => {
        const anchor = document.querySelector(
          `[data-user-message-id="${CSS.escape(turn.itemId)}"]`,
        );
        return anchor ? anchor.getBoundingClientRect().top : null;
      });
      const next = new Set<string>();
      for (let i = 0; i < turns.length; i++) {
        const start = tops[i];
        if (start == null) continue;
        // The turn's region ends where the next visible turn begins (or the
        // viewport bottom for the last turn).
        let end = view.bottom;
        for (let j = i + 1; j < tops.length; j++) {
          if (tops[j] != null) {
            end = tops[j] as number;
            break;
          }
        }
        // Overlaps the viewport?
        if (start < view.bottom && end > view.top) next.add(turns[i].itemId);
      }
      setVisibleIds((prev) => {
        if (prev.size === next.size && [...next].every((id) => prev.has(id))) return prev;
        return next;
      });
    };
    const schedule = () => {
      if (frame === 0) frame = requestAnimationFrame(recompute);
    };
    // Schedule the initial recompute through the same rAF gate rather than
    // running it synchronously: `turns` is a fresh array on every stream token,
    // so a synchronous read here would force a layout pass per token. Deferring
    // to rAF (and cancelling the pending frame on cleanup) coalesces a burst of
    // token-level re-renders into at most one layout read per frame.
    schedule();
    scrollEl.addEventListener("scroll", schedule, { passive: true });
    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      scrollEl.removeEventListener("scroll", schedule);
    };
  }, [scrollEl, turns]);

  // Keep the visible run of ticks reachable in the rail's own viewport as the
  // transcript scrolls, so the highlight tracks your position like a scrollbar
  // thumb. Only scrolls when that run has drifted out of (or past) the rail
  // viewport, and only far enough to bring it back to the edge — never
  // re-centering. This is what lets a tick-click leave the rail alone: after
  // you scroll the rail to a tick and click it, that tick is already in view,
  // so there's nothing to correct and the rail stays parked. The fade masks
  // (32px top/bottom) are treated as the usable edges so a tracked tick never
  // hides under them.
  useEffect(() => {
    const rail = railRef.current;
    if (!rail || visibleIds.size === 0) return;
    // Don't fight the user: while they're browsing the rail (pointer over it),
    // a `turns` change from eager/scroll-up history loading must not snap the
    // rail back to the transcript's visible run.
    if (interactingRef.current) return;
    let top = Infinity;
    let bottom = -Infinity;
    for (const id of visibleIds) {
      const tick = tickRefs.current.get(id);
      if (!tick) continue;
      top = Math.min(top, tick.offsetTop);
      bottom = Math.max(bottom, tick.offsetTop + tick.offsetHeight);
    }
    if (!Number.isFinite(top)) return;
    const viewTop = rail.scrollTop + FADE;
    const viewBottom = rail.scrollTop + rail.clientHeight - FADE;
    const max = rail.scrollHeight - rail.clientHeight;
    let next: number;
    if (top < viewTop) {
      // Run sits above the usable viewport — bring its top to the top edge.
      next = top - FADE;
    } else if (bottom > viewBottom) {
      // Run sits below — bring its bottom to the bottom edge.
      next = bottom - rail.clientHeight + FADE;
    } else {
      // Already fully in view: leave the rail exactly where it is.
      return;
    }
    const clamped = Math.max(0, Math.min(next, max));
    if (Math.abs(clamped - rail.scrollTop) < 1) return;
    rail.scrollTo({ top: clamped, behavior: "smooth" });
    // Re-run on `turns` too, not just `visibleIds`: eager-loading older history
    // prepends ticks without changing which transcript turns are on screen, so
    // `visibleIds` stays put. Without this, a fresh load (pinned to the bottom)
    // leaves the rail stuck at the top with the active run stranded off-screen
    // below the fade — the reported "should start at the bottom" bug.
  }, [visibleIds, turns]);

  // Eagerly page older history until the rail has its initial run of ticks.
  // The first history window holds only a turn or two (20 items ≈ a handful of
  // user turns), so without this the rail is a short, unscrollable stub. This
  // pages back-to-back and commits once (loadHistoryUntilUserMessages) so the
  // rail lands at ~INITIAL_TURNS ticks.
  //
  // Depends on loadingMoreHistory + hasMoreHistory, not just turns.length: the
  // first call can land while the initial bind still holds the history lock and
  // no-op. Re-running when the lock releases (and while more history exists)
  // resumes paging instead of wedging at the partial set.
  useEffect(() => {
    if (turns.length >= INITIAL_TURNS || !hasMoreHistory || loadingMoreHistory) return;
    void useChatStore.getState().loadHistoryUntilUserMessages(INITIAL_TURNS);
  }, [turns.length, hasMoreHistory, loadingMoreHistory]);

  // Reveal the rail once the eager back-fill has settled: either it reached the
  // initial run of ticks, or history ran out first (a genuinely short session).
  // Gating on !loadingMoreHistory avoids revealing mid-fetch at a partial count.
  // Latches once — never reset — so scroll-up loads later don't re-hide it.
  useEffect(() => {
    if (revealed || loadingMoreHistory) return;
    if (turns.length >= INITIAL_TURNS || !hasMoreHistory) setRevealed(true);
  }, [revealed, turns.length, hasMoreHistory, loadingMoreHistory]);

  // Page in older history when the rail nears its own top. Two triggers:
  //  - scroll: fires when the ticks overflow the box and the user scrolls up.
  //  - wheel: an upward wheel gesture near the top ALSO fetches, even when the
  //    ticks don't overflow (a box that fits its content emits no scroll event,
  //    so without this the rail would be a dead zone — the reported bug). New
  //    ticks prepend under the fade.
  useEffect(() => {
    const rail = railRef.current;
    if (!rail) return;
    const fetchOlder = () => {
      if (rail.scrollTop < FETCH_TOP_PX && hasMoreHistory && !loadingMoreHistory) {
        void useChatStore.getState().loadMoreHistory();
      }
    };
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) fetchOlder();
    };
    rail.addEventListener("scroll", fetchOlder, { passive: true });
    rail.addEventListener("wheel", onWheel, { passive: true });
    return () => {
      rail.removeEventListener("scroll", fetchOlder);
      rail.removeEventListener("wheel", onWheel);
    };
  }, [hasMoreHistory, loadingMoreHistory]);

  // Stable ref callback so React doesn't detach/re-attach every tick on every
  // render (an inline arrow changes identity each render, thrashing the Map).
  // Keyed off the element's own data-turn-tick. Entries for removed turns are
  // left stale — never iterated for absent turns, overwritten if the id returns
  // — so we skip the null (unmount) call rather than delete.
  const setTickRef = useCallback((el: HTMLButtonElement | null) => {
    if (!el) return;
    const id = el.dataset.turnTick;
    if (id) tickRefs.current.set(id, el);
  }, []);

  // Drop ref entries for turns no longer rendered. setTickRef never deletes on
  // unmount (to avoid churn), so on a session switch — where every itemId
  // changes — the old entries would otherwise leak references to detached
  // buttons for the component's lifetime. Prune to the live turn id-set here.
  useEffect(() => {
    const live = new Set(turns.map((t) => t.itemId));
    for (const id of tickRefs.current.keys()) {
      if (!live.has(id)) tickRefs.current.delete(id);
    }
  }, [turns]);

  const handleHover = useCallback((itemId: string) => {
    const rail = railRef.current;
    const tick = tickRefs.current.get(itemId);
    if (rail && tick) {
      // Rail-relative center: tick offset within the scrolled content minus
      // the rail's own scroll, so the box stays glued to the tick.
      setPreviewTop(tick.offsetTop - rail.scrollTop + tick.offsetHeight / 2);
    }
    setHoveredId(itemId);
  }, []);

  // onMouseEnter handler for a tick. Ignores the enter events a scroll causes
  // by dragging ticks under a stationary cursor: the cursor position hasn't
  // moved since the last accepted hover, so only accept enters where it has.
  // The scroll-end handler settles onto the tick actually under the cursor.
  const handleTickEnter = useCallback(
    (itemId: string, x: number, y: number) => {
      if (x === hoverPointRef.current.x && y === hoverPointRef.current.y) return;
      hoverPointRef.current = { x, y };
      handleHover(itemId);
    },
    [handleHover],
  );

  // Keep previewTop glued to the hovered tick while the rail scrolls under a
  // stationary pointer — thumb-tracking can smooth-scroll the rail without a
  // mouseenter, which would otherwise leave the preview box detached until the
  // next hover.
  useEffect(() => {
    const rail = railRef.current;
    if (!rail || !hoveredId) return;
    const reposition = () => {
      const tick = tickRefs.current.get(hoveredId);
      if (tick) setPreviewTop(tick.offsetTop - rail.scrollTop + tick.offsetHeight / 2);
    };
    rail.addEventListener("scroll", reposition, { passive: true });
    return () => rail.removeEventListener("scroll", reposition);
  }, [hoveredId]);

  // Once the rail comes to rest after a scroll, adopt the tick now under the
  // cursor as the preview. The mouseenter storm during the scroll is ignored
  // (cursor stationary), so without this settle the preview would stay stuck on
  // the pre-scroll tick. Debounced: each scroll event pushes the settle out, so
  // it fires once the rail stops.
  useEffect(() => {
    const rail = railRef.current;
    if (!rail) return;
    let settle = 0;
    const onScroll = () => {
      window.clearTimeout(settle);
      settle = window.setTimeout(() => {
        // Don't re-hover after the pointer has left the rail: mouseLeave clears
        // hoveredId, and a late settle would flicker a preview back on.
        if (!interactingRef.current) return;
        // Pick the tick now under the last pointer position and preview it.
        const el = document.elementFromPoint(pointerRef.current.x, pointerRef.current.y);
        const button = el?.closest<HTMLButtonElement>("[data-turn-tick]");
        const itemId = button?.dataset.turnTick;
        if (itemId && tickRefs.current.has(itemId)) handleHover(itemId);
      }, 120);
    };
    const onPointerMove = (e: PointerEvent) => {
      pointerRef.current = { x: e.clientX, y: e.clientY };
    };
    rail.addEventListener("scroll", onScroll, { passive: true });
    rail.addEventListener("pointermove", onPointerMove, { passive: true });
    return () => {
      window.clearTimeout(settle);
      rail.removeEventListener("scroll", onScroll);
      rail.removeEventListener("pointermove", onPointerMove);
    };
  }, [handleHover]);

  const hovered = hoveredId ? turns.find((t) => t.itemId === hoveredId) : undefined;

  // A single-turn (or empty) conversation has nothing to navigate.
  if (turns.length < 2) return null;

  return (
    <div
      // Vertically centered on the left edge (not full-height) so a short run
      // of ticks sits mid-page rather than clustering at the top. The row is
      // wide enough for the ticks; the preview box overflows to the right.
      // Fades in once the eager back-fill settles (see `revealed`) so the rail
      // doesn't flash the initial 2-turn window before older history lands.
      // Hidden on mobile (max-md:hidden): the rail is a hover minimap and
      // touch has no hover, so mobile keeps the ↑↓ nav buttons instead.
      className={cn(
        "pointer-events-none absolute left-0 top-1/2 z-40 flex w-6 -translate-y-1/2 items-center transition-opacity duration-200 max-md:hidden",
        revealed ? "opacity-100" : "opacity-0",
      )}
      onMouseLeave={() => {
        interactingRef.current = false;
        setHoveredId(null);
      }}
    >
      <div
        ref={railRef}
        onMouseEnter={() => {
          interactingRef.current = true;
        }}
        // Feed FADE to the CSS mask so the ramp width and the thumb-tracking
        // math share one constant.
        style={{ "--turn-rail-fade": `${FADE}px` } as CSSProperties}
        // max-h-72 (not a fixed height): the box shrinks to its ticks when a
        // session is short — so no confusing empty scroll track — and caps at
        // 288px once the ticks (~16px pitch) exceed ~18, at which point it
        // overflows and scrolls. Top+bottom fades (mask) show the ticks scroll
        // past both ends. items-start (not center) so a hover-widened tick
        // extends rightward from a fixed left edge instead of re-centering the
        // column; pl-2 insets the dashes from the screen edge; scrollbar hidden
        // — this is chrome. pointer-events only once revealed, so the invisible
        // (opacity-0) rail isn't a silent click target before it fades in.
        className={cn(
          "turn-rail-fade flex max-h-72 flex-col items-start overflow-y-auto py-6 pl-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
          revealed ? "pointer-events-auto" : "pointer-events-none",
        )}
      >
        {turns.map((turn) => {
          const isHovered = turn.itemId === hoveredId;
          // Black when it's the tick you're hovering, or — with no hover — when
          // its message is on screen. Hovering isolates black to that one tick.
          const black = hoveredId ? isHovered : visibleIds.has(turn.itemId);
          return (
            <button
              key={turn.itemId}
              type="button"
              data-turn-tick={turn.itemId}
              ref={setTickRef}
              onMouseEnter={(e) => handleTickEnter(turn.itemId, e.clientX, e.clientY)}
              onFocus={() => handleHover(turn.itemId)}
              // Keyboard focus shows the preview via onFocus; clear it on blur
              // so tabbing away doesn't leave the preview stranded on-screen.
              onBlur={() => setHoveredId((cur) => (cur === turn.itemId ? null : cur))}
              onClick={() => scrollToUserMessage(turn.itemId, flashUserMessage)}
              aria-label={`Jump to: ${turn.userText.slice(0, 80) || "message"}`}
              // Full-pitch hit area (h-4, no gap between ticks) so clicking
              // anywhere in a tick's band — not just the 2px dash — registers.
              // Matches the hover zone, so any spot that shows the preview also
              // navigates on click. Dash anchored left (justify-start) so the
              // hover-widen grows rightward without nudging the button box.
              className="group flex h-4 w-4 shrink-0 items-center justify-start"
            >
              {/* Dash: subtle by default; black for on-screen turns (or the
                  hovered one); wider only on hover. Transitions keep the color
                  shift and the hover widen smooth. */}
              <span
                className={cn(
                  "h-0.5 rounded-full transition-all duration-150",
                  isHovered ? "w-4" : "w-2.5",
                  black
                    ? "bg-foreground"
                    : "bg-muted-foreground/40 group-hover:bg-muted-foreground/70",
                )}
              />
            </button>
          );
        })}
      </div>

      {/* One persistent preview box: only its content and vertical position
          change between ticks, so hovering across ticks reads as a single box
          gliding + swapping text rather than many boxes popping in and out. */}
      <div
        aria-hidden={!hovered}
        style={{ top: previewTop }}
        className={cn(
          // Explicit width (not just max-w): the box is absolutely positioned
          // inside the narrow w-6 rail column, so without a set width it
          // shrink-wraps to a few words per line. w-80 lets it fill out and
          // preview more content; max-w caps it on small viewports.
          "pointer-events-none absolute left-7 w-80 max-w-[calc(100vw-4rem)] -translate-y-1/2 rounded-xl border border-border/60 bg-background px-3 py-2 shadow-md transition-[opacity,top] duration-150",
          hovered ? "opacity-100" : "opacity-0",
        )}
      >
        {hovered && (
          <>
            <p className="line-clamp-2 text-[0.8125rem] font-medium text-foreground">
              {hovered.userText || "(no text)"}
            </p>
            {hovered.responsePreview && (
              <p className="mt-1 line-clamp-3 text-xs text-muted-foreground">
                {hovered.responsePreview}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
