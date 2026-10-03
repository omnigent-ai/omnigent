import { Component, type ErrorInfo, type ReactNode } from "react";

/** Keep rejected PDF imports and rendering errors inside the file preview. */
export class PdfPreviewBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("PDF preview failed", error, info.componentStack);
  }

  override render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" className="p-8 text-ui">
        <p className="text-destructive">Could not preview this PDF.</p>
        <p className="mt-2 text-muted-foreground">Download the file to open it in another app.</p>
      </div>
    );
  }
}
