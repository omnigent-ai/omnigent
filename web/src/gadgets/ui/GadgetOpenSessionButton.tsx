// Full screen gadget controls reuse the session composer's portals: the Open session button,
// with a menu holding the composer's own host, permission and harness controls and, before the
// session exists, its Create session button.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { MessagesSquareIcon } from "lucide-react";

import { SessionStateBadge } from "@/components/SessionStateBadge";
import { Button } from "@/components/ui/button";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
import { useOpenSessionRow } from "@/hooks/useOpenSessionRow";
import { useSessionIndicatorState } from "@/hooks/useSessionIndicatorState";
import type { Conversation } from "@/hooks/useConversations";
import { getEmbedRoot } from "@/lib/host";
import {
  clearGadgetCreateSessionReview,
  forgetGadgetCreateSessionReviewShown,
  noteGadgetCreateSessionReviewShown,
  takeGadgetCreateSessionMenuRequest,
  useGadgetRailRequest,
} from "@/lib/gadgetRailTabs";
import { cn } from "@/lib/utils";
import {
  useSessionControlSlotRef,
  type SessionControlSlot,
} from "@/shell/MobileHeaderControlsSlot";

// Hover intent: a pointer passing over the button doesn't open the menu, and one moving from
// the button to the menu doesn't close it.
const OPEN_DELAY_MS = 350;
const CLOSE_DELAY_MS = 250;

const LABEL = "Open session";
// A control's own menu or list opens outside this menu, in a portal; interacting with it is
// interacting with this menu.
const NESTED_LAYER =
  "[data-radix-popper-content-wrapper], [role='menu'], [role='listbox'], [role='dialog']";
const NESTED_OPEN = "[aria-expanded='true'], [data-state='open']";
const FOCUSABLE = "button:not([disabled]), [href], [tabindex]:not([tabindex='-1'])";

type RowSlot = Exclude<SessionControlSlot, "icon" | "create">;

// The controls name themselves, so they carry no labels, side by side in one row.
const CONTROLS: RowSlot[] = ["host", "permission", "harness"];

function ControlSlot({ sessionId, slot }: { sessionId: string | null; slot: RowSlot }) {
  const ref = useSessionControlSlotRef(sessionId, slot);
  // A control the composer doesn't offer (an empty slot) takes no room.
  return (
    <div
      ref={ref}
      data-control-slot
      data-testid={`gadget-open-session-${slot}`}
      className="flex min-w-0 shrink items-center empty:hidden"
    />
  );
}

/** The session's status as its sidebar row shows it; the viewer isn't reading the session. */
function SessionRowStatus({ row }: { row: Conversation }) {
  const { sessionState } = useSessionIndicatorState(row, false);
  return sessionState === null ? null : <SessionStateBadge state={sessionState} />;
}

function SessionStatus({ sessionId }: { sessionId: string }) {
  const row = useOpenSessionRow(sessionId);
  return row === null ? null : <SessionRowStatus row={row} />;
}

function CreateSlot({ onFilled }: { onFilled: (element: HTMLElement | null) => void }) {
  const ref = useSessionControlSlotRef(null, "create");
  const setRef = useCallback(
    (element: HTMLDivElement | null) => {
      ref(element);
      onFilled(element);
    },
    [onFilled, ref],
  );
  return (
    <div
      ref={setRef}
      data-create-slot
      data-testid="gadget-open-session-create"
      className="mt-3 empty:hidden"
    />
  );
}

/** The message gadget code wrote for the session, shown before Create session sends it. */
function PromptReview({ gadgetName, prompt }: { gadgetName: string; prompt: string }) {
  return (
    <div
      data-testid="gadget-open-session-review"
      className="mb-2 max-w-[min(36rem,calc(100vw-2rem))] rounded-md border bg-muted/50 p-2 text-sm"
    >
      <p className="mb-1 text-xs text-muted-foreground">
        Written by the gadget “{gadgetName}”. Create session sends it to your agent.
      </p>
      <p className="max-h-48 overflow-y-auto break-words whitespace-pre-wrap">{prompt}</p>
    </div>
  );
}

export function GadgetOpenSessionButton({
  sessionId,
  gadgetId = null,
  starting = false,
  onOpenSession,
  className,
}: {
  /** The session the gadget is shown beside; null for New session. */
  sessionId: string | null;
  /** The gadget beside New session, whose own messages the menu shows before sending. */
  gadgetId?: string | null;
  /** Whether the session New session created is starting, before it shows the gadget. */
  starting?: boolean;
  onOpenSession: () => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  // Opened for a request: the rest of the screen dims until the viewer answers or dismisses it.
  const [modal, setModal] = useState(false);
  // Once the viewer uses a control in the menu, or the menu opened for a request, only Escape or
  // a click outside closes it: a control's own menu opens outside this one, where the pointer
  // would otherwise close it.
  const pinned = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const createSlotRef = useRef<HTMLElement | null>(null);
  const pressed = useRef(false);
  const iconRef = useSessionControlSlotRef(sessionId, "icon");
  const setCreateSlot = useCallback((element: HTMLElement | null) => {
    createSlotRef.current = element;
  }, []);

  const clearTimer = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, []);
  useEffect(() => clearTimer, [clearTimer]);

  const changeOpen = useCallback(
    (next: boolean) => {
      clearTimer();
      if (!next) {
        pinned.current = false;
        setModal(false);
      }
      setOpen(next);
    },
    [clearTimer],
  );
  const openSoon = useCallback(() => {
    clearTimer();
    timer.current = setTimeout(() => changeOpen(true), OPEN_DELAY_MS);
  }, [changeOpen, clearTimer]);
  const closeSoon = useCallback(() => {
    clearTimer();
    if (pinned.current) return;
    timer.current = setTimeout(() => changeOpen(false), CLOSE_DELAY_MS);
  }, [changeOpen, clearTimer]);

  const focusInMenu = useCallback((preferCreate: boolean) => {
    // The menu mounts on the next frame, and the composer fills its slots after that.
    requestAnimationFrame(() => {
      if (!preferCreate) {
        contentRef.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
        return;
      }
      // Focus Create session, or the menu when creation is unavailable or text needs review.
      const slot = createSlotRef.current;
      const create = slot?.querySelector("[data-gadget-draft-review]")
        ? null
        : slot?.querySelector<HTMLElement>(FOCUSABLE);
      (create ?? contentRef.current)?.focus({ preventScroll: true });
    });
  }, []);

  // A request from the gadget before its session exists opens the menu, with focus on Create
  // session, and keeps it open until the viewer dismisses it.
  const requested = useGadgetRailRequest((state) => state.createSessionMenuRequested);
  // A message the gadget itself wrote is shown in the menu, and focus goes to the menu, not
  // Create session: confirming the session must not confirm text the viewer hasn't seen.
  const storedReview = useGadgetRailRequest((state) => state.createSessionReview);
  const review =
    sessionId === null && storedReview !== null && storedReview.gadgetId === gadgetId
      ? storedReview
      : null;
  const reviewing = review !== null;
  // New session drops its own copy of the draft only while this menu shows the gadget's.
  const shownFor = review?.gadgetId ?? null;
  useEffect(() => {
    if (shownFor === null) return;
    noteGadgetCreateSessionReviewShown(shownFor);
    return () => forgetGadgetCreateSessionReviewShown(shownFor);
  }, [shownFor]);
  useEffect(() => {
    if (sessionId !== null || !requested || !takeGadgetCreateSessionMenuRequest()) return;
    changeOpen(true);
    pinned.current = true;
    setModal(true);
    if (reviewing) {
      requestAnimationFrame(() => contentRef.current?.focus({ preventScroll: true }));
    } else {
      focusInMenu(true);
    }
  }, [changeOpen, focusInMenu, requested, reviewing, sessionId]);
  // The session exists: what the gadget wrote has been sent.
  useEffect(() => {
    if (sessionId !== null) clearGadgetCreateSessionReview();
  }, [sessionId]);

  const openSession = useCallback(() => {
    changeOpen(false);
    onOpenSession();
  }, [changeOpen, onOpenSession]);

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "Escape" && open) {
      event.preventDefault();
      changeOpen(false);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      changeOpen(true);
      focusInMenu(false);
    }
  };

  // Something inside the menu (or a menu one of its controls opened) has the viewer's attention:
  // a control's open list, or the target of the event in question.
  const nestedOpen = useCallback(() => contentRef.current?.querySelector(NESTED_OPEN) != null, []);
  const inNestedLayer = useCallback((target: EventTarget | null) => {
    return target instanceof Element && target.closest(NESTED_LAYER) !== null;
  }, []);
  const keepOpenFor = useCallback(
    (event: Event) => {
      if (inNestedLayer(event.target) || nestedOpen()) event.preventDefault();
    },
    [inNestedLayer, nestedOpen],
  );

  // The composer renders the controls here through portals, so React sends their events to the
  // composer's tree, not this menu's: pin the menu from the DOM instead.
  const [content, setContent] = useState<HTMLDivElement | null>(null);
  // PopoverContent doesn't forward a ref, so an inner element finds the content it sits in.
  const setContentRef = useCallback((element: HTMLDivElement | null) => {
    const surface =
      element?.closest<HTMLDivElement>("[data-testid='gadget-open-session-popover']") ?? element;
    contentRef.current = surface;
    setContent(surface);
  }, []);
  useEffect(() => {
    if (content === null) return undefined;
    const pin = () => {
      pinned.current = true;
      clearTimer();
    };
    // Hover by DOM, not React: moving onto a control is staying in the menu.
    const enter = () => clearTimer();
    content.addEventListener("pointerdown", pin, true);
    content.addEventListener("focusin", pin);
    content.addEventListener("keydown", pin, true);
    content.addEventListener("pointerenter", enter);
    content.addEventListener("pointerleave", closeSoon);
    return () => {
      content.removeEventListener("pointerdown", pin, true);
      content.removeEventListener("focusin", pin);
      content.removeEventListener("keydown", pin, true);
      content.removeEventListener("pointerenter", enter);
      content.removeEventListener("pointerleave", closeSoon);
    };
  }, [clearTimer, closeSoon, content]);

  // A click in the gadget is a click elsewhere, but its frame keeps the event to itself: the
  // page only loses focus to the frame.
  useEffect(() => {
    if (!open) return undefined;
    const onBlur = () => {
      if (document.activeElement instanceof HTMLIFrameElement) changeOpen(false);
    };
    window.addEventListener("blur", onBlur);
    return () => window.removeEventListener("blur", onBlur);
  }, [changeOpen, open]);

  const dimming = open && modal;
  useEffect(() => {
    if (!dimming) return;
    // index.css drops the sidebar under the dimming while this is set.
    const root = document.documentElement;
    root.setAttribute("data-gadget-session-scrim", "");
    return () => root.removeAttribute("data-gadget-session-scrim");
  }, [dimming]);

  const creating = sessionId === null;
  return (
    <Popover open={open} onOpenChange={changeOpen}>
      {starting ? (
        <SessionStateBadge state={{ kind: "starting" }} />
      ) : (
        sessionId !== null && <SessionStatus sessionId={sessionId} />
      )}
      {/* Below the menu (z-50), above the gadget and everything else, the sidebar included. A
      press on it is a press outside the menu, which closes it. */}
      {dimming &&
        createPortal(
          <div
            aria-hidden
            data-testid="gadget-open-session-scrim"
            className="fixed inset-0 z-[49] bg-black/40 duration-150 animate-in fade-in-0"
          />,
          getEmbedRoot() ?? document.body,
        )}
      <PopoverAnchor asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={LABEL}
          aria-haspopup="dialog"
          aria-expanded={open}
          data-testid="gadget-open-session"
          className={cn("flex size-6", className)}
          onClick={(event) => {
            // Only a click on the button itself leaves full screen, not one React routes here
            // from content rendered elsewhere.
            if (event.currentTarget.contains(event.target as Node)) openSession();
          }}
          // pointerover, not pointerenter: a pointer arriving from the gadget's frame brings no
          // pointerout to this document, so React may never report it entering the button.
          onPointerOver={() => {
            if (!open && timer.current === null) openSoon();
          }}
          onPointerLeave={closeSoon}
          onPointerDown={() => {
            pressed.current = true;
          }}
          onFocus={() => {
            // Keyboard focus opens the menu; a click's focus leaves full screen instead.
            if (!pressed.current) changeOpen(true);
            pressed.current = false;
          }}
          onKeyDown={onKeyDown}
        >
          {/* The icon comes from the composer through a portal, so React would send events on
          it to the composer, not this button; with pointer-events off, the button gets them. */}
          <span
            ref={iconRef}
            data-testid="gadget-open-session-icon"
            data-pending-session={creating ? "true" : undefined}
            className={cn(
              "peer pointer-events-none flex size-4 items-center justify-center empty:hidden [&_img]:size-4 [&_svg]:size-4",
              creating && "opacity-70 grayscale",
            )}
          />
          <MessagesSquareIcon
            aria-hidden
            className="hidden size-4 text-muted-foreground peer-empty:block"
          />
        </Button>
      </PopoverAnchor>
      <PopoverContent
        side="bottom"
        align="end"
        className="group w-max min-w-44 max-w-[min(42rem,calc(100vw-1rem))] p-2"
        data-testid="gadget-open-session-popover"
        tabIndex={-1}
        data-modal={modal || undefined}
        aria-label={creating ? "Create session" : LABEL}
        aria-modal={modal || undefined}
        // Focus stays on the button; ArrowDown, or a request that opens the menu, moves it in.
        onOpenAutoFocus={(event) => event.preventDefault()}
        onCloseAutoFocus={(event) => event.preventDefault()}
        // A control's open menu is part of this one: neither focus nor a press there closes it,
        // and Escape closes that control's menu first.
        onInteractOutside={keepOpenFor}
        onEscapeKeyDown={(event) => {
          if (nestedOpen()) event.preventDefault();
        }}
      >
        {review !== null && <PromptReview gadgetName={review.gadgetName} prompt={review.prompt} />}
        <div
          ref={setContentRef}
          data-testid="gadget-open-session-controls"
          className="flex min-w-0 flex-row flex-wrap items-center gap-1"
        >
          {CONTROLS.map((slot) => (
            <ControlSlot key={slot} sessionId={sessionId} slot={slot} />
          ))}
        </div>
        {creating && <CreateSlot onFilled={setCreateSlot} />}
        {/* New session's Create session button takes this one's place while it is offered. */}
        <Button
          type="button"
          size="sm"
          variant={creating ? "outline" : "default"}
          className={cn(
            "mt-2 w-full",
            creating && "group-has-[[data-create-slot]:not(:empty)]:hidden",
          )}
          data-testid="gadget-open-session-button"
          onClick={openSession}
        >
          <MessagesSquareIcon className="size-4" aria-hidden />
          {LABEL}
        </Button>
      </PopoverContent>
    </Popover>
  );
}
