import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { clearChunkReloadGuard, LazyChunkLoadError } from "@/lib/lazyChunkRecovery";

interface RouteChunkErrorBoundaryProps {
  children: ReactNode;
  /** Test seam; defaults to a real page reload. */
  reloadPage?: () => void;
}

interface RouteChunkErrorBoundaryState {
  failed: boolean;
  error: unknown;
}

/**
 * Catches a route chunk that could not be loaded and offers an explicit
 * reload instead of a blank page.
 *
 * `reloadOnMissingChunk` already reloads the tab once when a redeploy deletes
 * a lazy route's chunk; it throws `LazyChunkLoadError` only when that reload
 * didn't help (broken asset graph, offline tab, storage unavailable). Any
 * other error is rethrown untouched so real rendering bugs keep their normal
 * failure path. No retry-in-place: `React.lazy` caches a rejection forever,
 * so a fresh document is the only way the chunk can load again.
 */
export class RouteChunkErrorBoundary extends Component<
  RouteChunkErrorBoundaryProps,
  RouteChunkErrorBoundaryState
> {
  override state: RouteChunkErrorBoundaryState = { failed: false, error: null };

  static getDerivedStateFromError(error: unknown): RouteChunkErrorBoundaryState {
    return { failed: true, error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    if (error instanceof LazyChunkLoadError) {
      console.error("Route chunk failed to load; offering a manual reload", error, {
        componentStack: info.componentStack,
      });
    }
  }

  private reload = () => {
    // A user-invoked reload always goes through: re-arm the auto-recovery
    // guard so the freshly booted page may reload itself once more if needed.
    clearChunkReloadGuard();
    (this.props.reloadPage ?? (() => window.location.reload()))();
  };

  override render() {
    if (!this.state.failed) return this.props.children;
    // Not a chunk-load failure — let it propagate to the default handling.
    if (!(this.state.error instanceof LazyChunkLoadError)) throw this.state.error;
    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-6">
        <div className="flex max-w-sm flex-col items-center gap-3 text-center">
          <h1 className="font-medium text-foreground text-lg">This page failed to load</h1>
          <p className="text-muted-foreground text-ui">
            The app may have been updated while this tab was open. Reload to pick up the latest
            version.
          </p>
          <Button variant="outline" onClick={this.reload}>
            Reload
          </Button>
        </div>
      </div>
    );
  }
}
