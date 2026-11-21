// The full screen thingy's Open session button. It shows the session's harness, in grey while
// the session is still New session, and leaves full screen when clicked. Hovering or focusing
// it opens a menu with the session's own composer controls (host, permission mode, harness and
// model), which the composer renders there through its session control slots, so a choice made
// in the menu is the composer's.
//
// Before the session exists, the menu's main button is New session's own Create session button:
// it starts the session with New session's prompt and settings while the thingy stays full
// screen. A request from the thingy that needs the session opens the menu by itself, over a dimmed
// screen, so the viewer sees what the request waits for.
//
// Beside the button, the session's status shows as in its sidebar row.
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
import { takeThingyCreateSessionMenuRequest, useThingyRailRequest } from "@/lib/thingyRailTabs";
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
      data-testid={`thingy-open-session-${slot}`}
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
      data-testid="thingy-open-session-create"
      className="mt-3 empty:hidden"
    />
  );
}

export function ThingyOpenSessionButton({
  sessionId,
  starting = false,
  onOpenSession,
  className,
}: {
  /** The session the thingy is shown beside; null for New session. */
  sessionId: string | null;
  /** Whether the session New session created is starting, before it shows the thingy. */
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
      // Without Create session (no host yet, say), the menu itself: a control would show its
      // tooltip, which takes the first Escape.
      const create = createSlotRef.current?.querySelector<HTMLElement>(FOCUSABLE);
      (create ?? contentRef.current)?.focus({ preventScroll: true });
    });
  }, []);

  // A request from the thingy before its session exists opens the menu, with focus on Create
  // session, and keeps it open until the viewer dismisses it.
  const requested = useThingyRailRequest((state) => state.createSessionMenuRequested);
  useEffect(() => {
    if (sessionId !== null || !requested || !takeThingyCreateSessionMenuRequest()) return;
    changeOpen(true);
    pinned.current = true;
    setModal(true);
    focusInMenu(true);
  }, [changeOpen, focusInMenu, requested, sessionId]);

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
      element?.closest<HTMLDivElement>("[data-testid='thingy-open-session-popover']") ?? element;
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

  // A click in the thingy is a click elsewhere, but its frame keeps the event to itself: the
  // page only loses focus to the frame.
  useEffect(() => {
    if (!open) return undefined;
    const onBlur = () => {
      if (document.activeElement instanceof HTMLIFrameElement) changeOpen(false);
    };
    window.addEventListener("blur", onBlur);
    return () => window.removeEventListener("blur", onBlur);
  }, [changeOpen, open]);

  const creating = sessionId === null;
  return (
    <Popover open={open} onOpenChange={changeOpen}>
      {starting ? (
        <SessionStateBadge state={{ kind: "starting" }} />
      ) : (
        sessionId !== null && <SessionStatus sessionId={sessionId} />
      )}
      {/* Below the menu (z-50), above the thingy and everything else. A press on it is a press
      outside the menu, which closes it. */}
      {open &&
        modal &&
        createPortal(
          <div
            aria-hidden
            data-testid="thingy-open-session-scrim"
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
          data-testid="thingy-open-session"
          className={cn("flex size-6", className)}
          onClick={(event) => {
            // Only a click on the button itself leaves full screen, not one React routes here
            // from content rendered elsewhere.
            if (event.currentTarget.contains(event.target as Node)) openSession();
          }}
          // pointerover, not pointerenter: a pointer arriving from the thingy's frame brings no
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
            data-testid="thingy-open-session-icon"
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
        data-testid="thingy-open-session-popover"
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
        <div
          ref={setContentRef}
          data-testid="thingy-open-session-controls"
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
          data-testid="thingy-open-session-button"
          onClick={openSession}
        >
          <MessagesSquareIcon className="size-4" aria-hidden />
          {LABEL}
        </Button>
      </PopoverContent>
    </Popover>
  );
}
