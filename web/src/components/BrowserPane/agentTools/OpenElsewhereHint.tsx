// An empty browser pane says when its tab's page is open in
// another Omnigent desktop window (its native view lives there), instead of
// offering an empty page.
import { type ReactNode, useEffect, useState } from "react";

interface ElsewhereBridge {
  browserHasView?: (viewId: string) => Promise<{ exists: boolean; elsewhere?: boolean }>;
}

// How often an empty pane re-asks, as the other window opens or closes it.
const RECHECK_MS = 1500;
// The first ask waits for the pane's own probe, which finds a page here.
const FIRST_CHECK_MS = 250;

export const OPEN_ELSEWHERE_HINT =
  "This tab's page is open in another Omnigent window. Switch to that window to see it.";

/** A counter that ticks every `intervalMs` while the window is visible, and once on return. */
function useVisibleTick(intervalMs: number): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let timer: number | undefined;
    const bump = () => setTick((value) => value + 1);
    const sync = () => {
      window.clearInterval(timer);
      timer = undefined;
      if (document.visibilityState === "hidden") return;
      timer = window.setInterval(bump, intervalMs);
    };
    const onVisibilityChange = () => {
      if (document.visibilityState !== "hidden") bump();
      sync();
    };
    sync();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [intervalMs]);
  return tick;
}

/** The empty pane's hint: `children`, or that the page is in another window.
 *  Mounted only while the pane has no page here, so it polls only then. */
export function OpenElsewhereHint({ viewId, children }: { viewId: string; children: ReactNode }) {
  const [elsewhere, setElsewhere] = useState(false);
  const tick = useVisibleTick(RECHECK_MS);
  useEffect(() => {
    const bridge = (window as unknown as { omnigentDesktop?: ElsewhereBridge }).omnigentDesktop;
    if (!bridge?.browserHasView) return;
    let cancelled = false;
    const timer = setTimeout(
      () => {
        void bridge
          .browserHasView?.(viewId)
          .then((r) => {
            if (!cancelled) setElsewhere(!r?.exists && r?.elsewhere === true);
          })
          .catch(() => {});
      },
      tick === 0 ? FIRST_CHECK_MS : 0,
    );
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [viewId, tick]);
  return elsewhere ? OPEN_ELSEWHERE_HINT : children;
}
