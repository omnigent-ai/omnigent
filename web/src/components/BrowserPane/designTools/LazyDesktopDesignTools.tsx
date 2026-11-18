import { lazy, Suspense, useSyncExternalStore, type ComponentType } from "react";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { desktopDesignTools } from "./desktopBridge";
import type { DesktopDesignToolsProps } from "./DesktopDesignTools";

// The tools' chunk couldn't load, or they failed (this document's lifetime): the pane
// offers design mode in their place, as an older shell does.
let loadFailed = false;
const loadListeners = new Set<() => void>();

function markFailed(): void {
  loadFailed = true;
  for (const listener of [...loadListeners]) listener();
}

/** Whether this desktop shell has the design tools and they can load (else, design mode). */
export function hasDesktopDesignTools(): boolean {
  return desktopDesignTools() !== null && !loadFailed;
}

/** hasDesktopDesignTools(), kept up to date (a failed load changes it). */
export function useHasDesktopDesignTools(): boolean {
  return useSyncExternalStore((listener) => {
    loadListeners.add(listener);
    return () => loadListeners.delete(listener);
  }, hasDesktopDesignTools);
}

// The tools, their overlay and outbox load in their own chunk, off the app's first paint.
// A chunk that can't load leaves the toolbar with design mode instead, as they're optional.
const Tools = lazy<ComponentType<DesktopDesignToolsProps>>(() =>
  import("./DesktopDesignTools")
    .then((module) => ({ default: module.DesktopDesignTools }))
    .catch(() => {
      markFailed();
      return { default: () => null };
    }),
);

/** The Browser pane's design tools; nothing in the web app or an older shell, or on a failure. */
export function DesktopDesignTools(props: DesktopDesignToolsProps) {
  if (!hasDesktopDesignTools()) return null;
  return (
    <RenderErrorBoundary name="the design tools" fallback={null} onError={markFailed}>
      <Suspense fallback={null}>
        <Tools {...props} />
      </Suspense>
    </RenderErrorBoundary>
  );
}
