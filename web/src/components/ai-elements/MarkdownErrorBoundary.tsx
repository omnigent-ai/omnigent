import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Contains a throw from the markdown pipeline to the one block that threw.
 *
 * Streamdown renders mermaid diagrams behind `React.lazy`, and a rejected lazy
 * import is re-thrown on every subsequent render (Suspense catches pending
 * imports, not failed ones). With no boundary above it React unmounts the whole
 * tree, so a single un-renderable diagram — a stale tab whose hashed mermaid
 * chunk no longer exists, say — blanks the entire app.
 *
 * Wrap every `Streamdown` seam so a block that can't render degrades to its own
 * markdown source and the rest of the page keeps working. Deliberately does not
 * reset when `children` change: React.lazy caches a rejection forever, so a
 * retry would only throw again.
 */
export class MarkdownErrorBoundary extends Component<
  { children: ReactNode; source: ReactNode },
  { failed: boolean }
> {
  override state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Markdown rendering failed; falling back to source", error, info.componentStack);
  }

  override render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div>
        <p className="mb-1 text-muted-foreground text-xs">Could not render this markdown.</p>
        <div className="whitespace-pre-wrap wrap-anywhere font-mono text-sm">
          {this.props.source}
        </div>
      </div>
    );
  }
}
