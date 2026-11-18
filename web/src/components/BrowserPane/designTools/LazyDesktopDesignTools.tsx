import { lazy, Suspense, useSyncExternalStore, type ComponentType } from "react";
import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { desktopDesignTools } from "./desktopBridge";
import type { DesktopDesignToolsProps } from "./DesktopDesignTools";

/** After the tools' chunk first fails to load, how long before trying it again. */
export const TOOLS_RETRY_MS = 5_000;
/** The longest wait between tries: each failed one doubles it, and a load resets it. */
export const TOOLS_RETRY_MAX_MS = 5 * 60_000;
let retryMs = TOOLS_RETRY_MS;

// While the tools' chunk can't load (tried again after a while), or for good once they
// failed while running, the pane offers design mode in their place, as an older shell does.
let chunkMissing = false;
let broken = false;
const loadListeners = new Set<() => void>();

function changed(): void {
  for (const listener of [...loadListeners]) listener();
}

function markBroken(): void {
  broken = true;
  changed();
}

/** Whether this desktop shell has the design tools and they can load (else, design mode). */
export function hasDesktopDesignTools(): boolean {
  return desktopDesignTools() !== null && !chunkMissing && !broken;
}

function subscribeLoad(listener: () => void): () => void {
  loadListeners.add(listener);
  return () => {
    loadListeners.delete(listener);
  };
}

/** hasDesktopDesignTools(), kept up to date (a failed load changes it). */
export function useHasDesktopDesignTools(): boolean {
  return useSyncExternalStore(subscribeLoad, hasDesktopDesignTools);
}

const importTools = () =>
  import("./DesktopDesignTools").then((module) => ({ default: module.DesktopDesignTools }));

// The tools, their overlay and outbox load in their own chunk, off the app's first paint.
// A chunk that can't load leaves the toolbar with design mode instead, as they're optional.
function loadTools() {
  return lazy<ComponentType<DesktopDesignToolsProps>>(() =>
    importTools().catch(() => {
      chunkMissing = true;
      changed();
      retryLater();
      return { default: () => null };
    }),
  );
}
let Tools = loadTools();

/** Try the chunk again after a while, less often each time; once it loads, offer the tools. */
function retryLater(): void {
  const waitMs = retryMs;
  retryMs = Math.min(retryMs * 2, TOOLS_RETRY_MAX_MS);
  setTimeout(() => {
    importTools().then(() => {
      Tools = loadTools();
      chunkMissing = false;
      retryMs = TOOLS_RETRY_MS;
      changed();
    }, retryLater);
  }, waitMs);
}

/** The Browser pane's design tools; nothing in the web app or an older shell, or on a failure. */
export function DesktopDesignTools(props: DesktopDesignToolsProps) {
  const available = useHasDesktopDesignTools();
  if (!available) return null;
  return (
    <RenderErrorBoundary name="the design tools" fallback={null} onError={markBroken}>
      <Suspense fallback={null}>
        <Tools {...props} />
      </Suspense>
    </RenderErrorBoundary>
  );
}
