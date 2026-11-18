import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { isChunkLoadError } from "@/lib/chunkLoadRecovery";

interface Props {
  children: ReactNode;
  name: string;
  resetKey?: string;
  /** Shown instead of the alert; `null` hides a part whose absence is fine. */
  fallback?: ReactNode;
  /** Told of a render failure it contained. */
  onError?: (error: Error) => void;
}

interface State {
  failed: boolean;
  resetKey?: string;
}

/** Contains render failures without remounting healthy content on navigation. */
export class RenderErrorBoundary extends Component<Props, State> {
  override state: State = { failed: false, resetKey: this.props.resetKey };

  static getDerivedStateFromProps(props: Props, state: State) {
    return props.resetKey !== state.resetKey ? { failed: false, resetKey: props.resetKey } : null;
  }

  static getDerivedStateFromError(error: unknown) {
    // Let the app's chunk boundary keep its guarded reload recovery.
    if (isChunkLoadError(error)) throw error;
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`Rendering ${this.props.name} failed`, error, info.componentStack);
    this.props.onError?.(error);
  }

  override render() {
    if (!this.state.failed) return this.props.children;
    if (this.props.fallback !== undefined) return this.props.fallback;
    return (
      <div
        role="alert"
        className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 p-6 text-center"
      >
        <p className="text-sm text-muted-foreground">Unable to display {this.props.name}.</p>
        <Button type="button" variant="outline" onClick={() => this.setState({ failed: false })}>
          Try again
        </Button>
      </div>
    );
  }
}
