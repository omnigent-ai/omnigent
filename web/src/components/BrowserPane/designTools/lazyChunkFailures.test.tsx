import { act, render, screen } from "@testing-library/react";
import { Component, type ReactNode } from "react";
import { expect, it, vi } from "vitest";

// The card's chunk can't load (a deploy replaced it, the network dropped).
vi.mock("./DesignRequestMessage", () => ({
  get DesignRequestBodyView() {
    throw new TypeError(
      "Failed to fetch dynamically imported module: /assets/DesignRequestMessage.js",
    );
  },
}));
// The strip's chunk can't load until the network comes back.
const strip = vi.hoisted(() => ({ broken: true, loads: 0 }));
vi.mock("./DesignOutboxStrip", () => ({
  get DesignOutboxStrip() {
    strip.loads += 1;
    if (!strip.broken) return () => <p>Design requests</p>;
    throw new TypeError(
      "Failed to fetch dynamically imported module: /assets/DesignOutboxStrip.js",
    );
  },
}));
import { DesignRequestBody } from "./LazyDesignRequestMessage";
import { DesignOutboxStrip, STRIP_RETRY_MAX_MS, STRIP_RETRY_MS } from "./LazyDesignOutboxStrip";
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

it("shows a design message's own words when its card can't load", async () => {
  render(
    <AppBoundary>
      <DesignRequestBody
        request={{ kind: "element", request: "Make it red", element: "p#blurb", elementText: "Hi" }}
      />
    </AppBoundary>,
  );
  await new Promise((resolve) => {
    setTimeout(resolve, 20);
  });
  expect(screen.getByText("Make it red")).toBeTruthy();
  // And what it was about.
  expect(screen.getByText('p#blurb "Hi"')).toBeTruthy();
  expect(screen.queryByText("The app failed")).toBeNull();
});

it("leaves the composer as it was when the outbox strip can't load, and tries again less often", async () => {
  vi.useFakeTimers();
  try {
    setDesignRequestSessions(["conv_1"]);
    render(
      <AppBoundary>
        <p>Composer</p>
        <DesignOutboxStrip conversationId="conv_1" />
      </AppBoundary>,
    );
    await act(() => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText("Composer")).toBeTruthy();
    expect(screen.queryByText("The app failed")).toBeNull();
    // Each try that fails waits twice as long for the next, up to a cap.
    const loads = () => strip.loads;
    strip.loads = 0;
    let waitMs = STRIP_RETRY_MS;
    for (let tries = 1; tries <= 8; tries += 1) {
      // oxlint-disable-next-line no-await-in-loop -- one wait at a time
      await act(() => vi.advanceTimersByTimeAsync(waitMs - 1));
      expect(loads()).toBe(tries - 1);
      // oxlint-disable-next-line no-await-in-loop -- one wait at a time
      await act(() => vi.advanceTimersByTimeAsync(1));
      expect(loads()).toBe(tries);
      waitMs = Math.min(waitMs * 2, STRIP_RETRY_MAX_MS);
    }
    expect(waitMs).toBe(STRIP_RETRY_MAX_MS);
    // The chunk loads again, and the mounted strip tries it again on its own.
    strip.broken = false;
    await act(() => vi.advanceTimersByTimeAsync(STRIP_RETRY_MAX_MS));
    await act(() => vi.advanceTimersByTimeAsync(20));
    expect(screen.getByText("Design requests")).toBeTruthy();
  } finally {
    setDesignRequestSessions([]);
    vi.useRealTimers();
  }
});
