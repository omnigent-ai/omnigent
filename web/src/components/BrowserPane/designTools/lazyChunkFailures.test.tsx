import { act, render, screen } from "@testing-library/react";
import { Component, type ReactNode } from "react";
import { expect, it, vi } from "vitest";

// The strip's chunk can't load until the network comes back.
const strip = vi.hoisted(() => ({ broken: true, failsFor: "" }));
vi.mock("./DesignOutboxStrip", () => ({
  get DesignOutboxStrip() {
    if (!strip.broken) {
      return ({ conversationId }: { conversationId: string }) => {
        if (conversationId === strip.failsFor) throw new Error("The strip broke");
        return <p>Design requests</p>;
      };
    }
    throw new TypeError(
      "Failed to fetch dynamically imported module: /assets/DesignOutboxStrip.js",
    );
  },
}));
import { DesignOutboxStrip } from "./LazyDesignOutboxStrip";
import { CHUNK_RETRY_MS } from "./retryingChunk";
import { setDesignRequestSessions } from "./core/designOutboxPresence";

/** The app's boundary above: what reaches it would replace the app. */
class AppBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  override render() {
    return this.state.failed ? <p>The app failed</p> : this.props.children;
  }
}

it("leaves the composer as it was when the outbox strip can't load, and shows it once it does", async () => {
  vi.useFakeTimers();
  try {
    setDesignRequestSessions(["conv_1"]);
    const view = render(
      <AppBoundary>
        <p>Composer</p>
        <DesignOutboxStrip conversationId="conv_1" />
      </AppBoundary>,
    );
    await act(() => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText("Composer")).toBeTruthy();
    expect(screen.queryByText("The app failed")).toBeNull();
    // The chunk loads again, and the mounted strip shows it.
    strip.broken = false;
    await act(() => vi.advanceTimersByTimeAsync(CHUNK_RETRY_MS));
    expect(screen.getByText("Design requests")).toBeTruthy();
    // A render failure hides it, until the conversation changes.
    vi.spyOn(console, "error").mockImplementation(() => {});
    setDesignRequestSessions(["conv_1", "conv_2", "conv_3"]);
    strip.failsFor = "conv_2";
    view.rerender(
      <AppBoundary>
        <p>Composer</p>
        <DesignOutboxStrip conversationId="conv_2" />
      </AppBoundary>,
    );
    expect(screen.queryByText("Design requests")).toBeNull();
    expect(screen.getByText("Composer")).toBeTruthy();
    view.rerender(
      <AppBoundary>
        <p>Composer</p>
        <DesignOutboxStrip conversationId="conv_3" />
      </AppBoundary>,
    );
    expect(screen.getByText("Design requests")).toBeTruthy();
  } finally {
    setDesignRequestSessions([]);
    vi.restoreAllMocks();
    vi.useRealTimers();
  }
});
