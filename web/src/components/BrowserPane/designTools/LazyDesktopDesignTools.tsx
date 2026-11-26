import { Suspense, useSyncExternalStore } from "react";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { desktopDesignTools } from "./desktopBridge";
import type { DesktopDesignToolsProps } from "./DesktopDesignTools";
import { retryingChunk } from "./retryingChunk";

// The tools, their overlay and outbox load in their own chunk. While it can't load, or for
// good once the tools failed while running, the pane offers design mode, as an older shell does.
const tools = retryingChunk<DesktopDesignToolsProps>(() =>
  import("./DesktopDesignTools").then((module) => module.DesktopDesignTools),
);
let broken = false;
const brokenListeners = new Set<() => void>();

function markBroken(): void {
  broken = true;
  for (const listener of [...brokenListeners]) listener();
}

/** Whether this desktop shell has the design tools and they can load (else, design mode). */
export function hasDesktopDesignTools(): boolean {
  return desktopDesignTools() !== null && !tools.missing() && !broken;
}

function subscribe(listener: () => void): () => void {
  brokenListeners.add(listener);
  const unsubscribe = tools.subscribe(listener);
  return () => {
    brokenListeners.delete(listener);
    unsubscribe();
  };
}

/** hasDesktopDesignTools(), kept up to date (a failed load changes it). */
export function useHasDesktopDesignTools(): boolean {
  return useSyncExternalStore(subscribe, hasDesktopDesignTools);
}

/** The Browser pane's design tools; nothing in the web app or an older shell, or on a failure. */
export function DesktopDesignTools(props: DesktopDesignToolsProps) {
  const available = useHasDesktopDesignTools();
  const Tools = tools.useComponent();
  if (!available) return null;
  return (
    <RenderErrorBoundary name="the design tools" fallback={null} onError={markBroken}>
      <Suspense fallback={null}>
        <Tools {...props} />
      </Suspense>
    </RenderErrorBoundary>
  );
}
