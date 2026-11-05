import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";

type HeaderControlsTarget = { element: HTMLDivElement; sessionId: string } | null;

const MobileHeaderControlsContext = createContext<{
  target: HeaderControlsTarget;
  setTarget: (target: HeaderControlsTarget) => void;
} | null>(null);

export function MobileHeaderControlsProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<HeaderControlsTarget>(null);
  const value = useMemo(() => ({ target, setTarget }), [target]);
  return (
    <MobileHeaderControlsContext.Provider value={value}>
      {children}
    </MobileHeaderControlsContext.Provider>
  );
}

export function MobileHeaderControlsSlot({ sessionId }: { sessionId: string }) {
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
      className="flex items-center empty:hidden"
    />
  );
}

export function MobileHeaderControlPortal({
  sessionId,
  children,
}: {
  sessionId: string | null;
  children: ReactNode;
}) {
  const context = useContext(MobileHeaderControlsContext);
  const isMobile = useIsMobileViewport();
  const target = context?.target;
  if (!isMobile || !target || target.sessionId !== sessionId) return children;
  return createPortal(children, target.element);
}
