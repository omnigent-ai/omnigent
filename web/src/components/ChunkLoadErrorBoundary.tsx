import { Component, type ReactNode } from "react";
import { isChunkLoadError, reloadAfterChunkError } from "@/lib/chunkLoadRecovery";
import { Button } from "@/components/ui/button";

/** Suspense handles pending imports; rejected lazy imports need a fresh document. */
export class ChunkLoadErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  override state = { failed: false };

  static getDerivedStateFromError(error: unknown) {
    if (!isChunkLoadError(error)) throw error;
    return { failed: true };
  }

  override componentDidCatch() {
    reloadAfterChunkError();
  }

  override render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" className="flex flex-1 flex-col items-center justify-center gap-4 p-8">
        <h1 className="text-lg font-medium">Unable to load this page</h1>
        <p className="max-w-md text-center text-sm text-muted-foreground">
          A required part of the app could not be loaded. This can happen after an update or a
          connection problem. Check your connection and refresh to try again.
        </p>
        <Button onClick={() => location.reload()}>Refresh page</Button>
      </div>
    );
  }
}
