import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";

type HeaderControlsTarget = { element: HTMLDivElement; sessionId: string | null } | null;

const MobileHeaderControlsContext = createContext<{
  target: HeaderControlsTarget;
  setTarget: (target: HeaderControlsTarget) => void;
} | null>(null);

/**
 * Slots, keyed by session (`null` for New session), where the composer's own controls render
 * outside the composer, so a choice made there is the composer's.
 */
export type SessionControlSlot = "icon" | "host" | "permission" | "harness" | "create";

type SessionControlSlots = {
  sessionId: string | null;
  slots: Partial<Record<SessionControlSlot, HTMLElement>>;
} | null;

const SessionControlSlotsContext = createContext<{
  target: SessionControlSlots;
  setSlot: (
    sessionId: string | null,
    slot: SessionControlSlot,
    element: HTMLElement | null,
  ) => void;
} | null>(null);

export function MobileHeaderControlsProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<HeaderControlsTarget>(null);
  const value = useMemo(() => ({ target, setTarget }), [target]);
  const [slotTarget, setSlotTarget] = useState<SessionControlSlots>(null);
  const setSlot = useCallback(
    (sessionId: string | null, slot: SessionControlSlot, element: HTMLElement | null) =>
      setSlotTarget((current) => {
        const previous = current?.sessionId === sessionId ? current.slots : {};
        if ((previous[slot] ?? null) === element) return current;
        const slots = Object.fromEntries(
          Object.entries({ ...previous, [slot]: element ?? undefined }).filter(
            ([, slotElement]) => slotElement !== undefined,
          ),
        ) as Partial<Record<SessionControlSlot, HTMLElement>>;
        return Object.keys(slots).length > 0 ? { sessionId, slots } : null;
      }),
    [],
  );
  const slotValue = useMemo(() => ({ target: slotTarget, setSlot }), [slotTarget, setSlot]);
  return (
    <MobileHeaderControlsContext.Provider value={value}>
      <SessionControlSlotsContext.Provider value={slotValue}>
        {children}
      </SessionControlSlotsContext.Provider>
    </MobileHeaderControlsContext.Provider>
  );
}

export function MobileHeaderControlsSlot({ sessionId }: { sessionId: string | null }) {
  const context = useContext(MobileHeaderControlsContext);
  const setTarget = context?.setTarget;
  const registerTarget = useCallback(
    (element: HTMLDivElement | null) => setTarget?.(element ? { element, sessionId } : null),
    [setTarget, sessionId],
  );
  return (
    <div
      ref={registerTarget}
      data-session-id={sessionId}
      data-testid="mobile-header-controls-slot"
      className="no-drag pointer-events-auto flex items-center empty:hidden max-md:gap-2"
    />
  );
}

/** A ref that offers `element` as the session's `slot` while it is mounted. */
export function useSessionControlSlotRef(
  sessionId: string | null,
  slot: SessionControlSlot,
): (element: HTMLElement | null) => void {
  const setSlot = useContext(SessionControlSlotsContext)?.setSlot;
  return useCallback(
    (element: HTMLElement | null) => setSlot?.(sessionId, slot, element),
    [setSlot, sessionId, slot],
  );
}

function useSessionControlSlot(
  sessionId: string | null,
  slot: SessionControlSlot | undefined,
): HTMLElement | null {
  const target = useContext(SessionControlSlotsContext)?.target;
  if (slot === undefined || !target || target.sessionId !== sessionId) return null;
  return target.slots[slot] ?? null;
}

/**
 * Renders a composer control where it is offered: the session's `popoverSlot` while the full
 * screen gadget's popover is open, the mobile header on mobile, else in place.
 */
export function MobileHeaderControlPortal({
  sessionId,
  popoverSlot,
  children,
}: {
  sessionId: string | null;
  popoverSlot?: SessionControlSlot;
  children: ReactNode;
}) {
  const context = useContext(MobileHeaderControlsContext);
  const isMobile = useIsMobileViewport();
  const slot = useSessionControlSlot(sessionId, popoverSlot);
  if (slot) return createPortal(children, slot);
  const target = context?.target;
  if (!isMobile || !target || target.sessionId !== sessionId) return children;
  return createPortal(children, target.element);
}

/**
 * Renders a composer control in the session's `slot` while it is offered. Otherwise it stays in
 * place, or renders nothing when it exists only for the slot (`inPlace` false).
 */
export function SessionControlPortal({
  sessionId,
  slot,
  inPlace = true,
  offered = true,
  children,
}: {
  sessionId: string | null;
  slot: SessionControlSlot;
  inPlace?: boolean;
  /** False keeps the control out of slots, e.g. for a session still being created. */
  offered?: boolean;
  children: ReactNode;
}) {
  const element = useSessionControlSlot(sessionId, offered ? slot : undefined);
  if (element) return createPortal(children, element);
  return inPlace ? children : null;
}
