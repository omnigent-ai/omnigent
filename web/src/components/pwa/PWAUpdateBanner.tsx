import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { useServiceWorkerUpdate } from "./useServiceWorkerUpdate";

/**
 * Service-worker update banner.
 *
 * Mounted once at the standalone app root (NOT in the embed island). The
 * service worker never swaps under a live session — we surface this banner and
 * let the user reload into the new build. Omnigent has no offline mode (cloud
 * app), so there is deliberately no "ready to work offline" state.
 */
export function PWAUpdateBanner() {
  const { needRefresh, reload, dismiss } = useServiceWorkerUpdate();

  if (!needRefresh) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "fixed inset-x-0 bottom-0 z-[100] flex items-center justify-center gap-3 px-4 py-3",
        "border-t border-border bg-background/95 backdrop-blur",
        "supports-[backdrop-filter]:bg-background/80",
      )}
    >
      <span className="text-sm text-foreground">A new version of Omnigent is available.</span>
      <Button size="sm" onClick={reload}>
        Reload
      </Button>
      <Button size="sm" variant="ghost" onClick={dismiss}>
        Dismiss
      </Button>
    </div>
  );
}
