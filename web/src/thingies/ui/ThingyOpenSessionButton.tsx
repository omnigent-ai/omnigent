// The full screen thingy's Open session button. It shows the session's harness, in grey while
// the session is still New session, and leaves full screen when clicked. Hovering or focusing
// it opens a menu with the session's own composer controls (host, permission mode, harness and
// model), which the composer renders there through its session control slots, so a choice made
// in the menu is the composer's.
//
// Before the session exists, the menu's main button is New session's own Create session button:
// it starts the session with New session's prompt and settings while the thingy stays full
// screen. A request from the thingy that needs the session opens the menu by itself.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { MessagesSquareIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
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
const FOCUSABLE = "button:not([disabled]), [href], [tabindex]:not([tabindex='-1'])";

type RowSlot = Exclude<SessionControlSlot, "icon" | "create">;

const ROWS: { slot: RowSlot; label: string }[] = [
  { slot: "host", label: "Host" },
  { slot: "permission", label: "Permissions" },
  { slot: "harness", label: "Harness" },
];

function ControlRow({
  sessionId,
  slot,
  label,
}: {
  sessionId: string | null;
  slot: RowSlot;
  label: string;
}) {
  const ref = useSessionControlSlotRef(sessionId, slot);
  // A row whose control the composer doesn't offer (an empty slot) hides itself.
  return (
    <div className="flex min-w-0 items-center gap-3 has-[>[data-control-slot]:empty]:hidden">
      <span className="w-20 shrink-0 text-xs text-muted-foreground">{label}</span>
      <div
        ref={ref}
        data-control-slot
        data-testid={`thingy-open-session-${slot}`}
        className="flex min-w-0 flex-1 items-center"
      />
    </div>
  );
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
  onOpenSession,
  className,
}: {
  /** The session the thingy is shown beside; null for New session. */
  sessionId: string | null;
  onOpenSession: () => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
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
      if (!next) pinned.current = false;
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
      const create = preferCreate
        ? createSlotRef.current?.querySelector<HTMLElement>(FOCUSABLE)
        : null;
      (create ?? contentRef.current?.querySelector<HTMLElement>(FOCUSABLE))?.focus();
    });
  }, []);

  // A request from the thingy before its session exists opens the menu, with focus on Create
  // session, and keeps it open until the viewer dismisses it.
  const requested = useThingyRailRequest((state) => state.createSessionMenuRequested);
  useEffect(() => {
    if (sessionId !== null || !requested || !takeThingyCreateSessionMenuRequest()) return;
    changeOpen(true);
    pinned.current = true;
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

  const creating = sessionId === null;
  return (
    <Popover open={open} onOpenChange={changeOpen}>
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
          onClick={openSession}
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
        ref={contentRef}
        side="bottom"
        align="end"
        className="group w-72 p-3"
        data-testid="thingy-open-session-popover"
        aria-label={creating ? "Create session" : LABEL}
        // Focus stays on the button; ArrowDown, or a request that opens the menu, moves it in.
        onOpenAutoFocus={(event) => event.preventDefault()}
        onCloseAutoFocus={(event) => event.preventDefault()}
        onPointerOver={clearTimer}
        onPointerLeave={closeSoon}
        onPointerDownCapture={() => {
          pinned.current = true;
        }}
        onKeyDownCapture={() => {
          pinned.current = true;
        }}
      >
        <div className="flex flex-col gap-2">
          {ROWS.map((row) => (
            <ControlRow key={row.slot} sessionId={sessionId} slot={row.slot} label={row.label} />
          ))}
        </div>
        {creating && <CreateSlot onFilled={setCreateSlot} />}
        {/* New session's Create session button takes this one's place while it is offered. */}
        <Button
          type="button"
          size="sm"
          variant={creating ? "outline" : "default"}
          className={cn(
            "mt-3 w-full",
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
