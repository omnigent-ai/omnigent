import { act, renderHook, render, screen, waitFor } from "@testing-library/react";
import { Component, createRef, type ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

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

/** The tools' chunk can't load (a deploy replaced it, the network dropped). */
function chunkFails() {
  vi.doMock("./DesktopDesignTools", () => ({
    get DesktopDesignTools() {
      throw new TypeError(
        "Failed to fetch dynamically imported module: /assets/DesktopDesignTools.js",
      );
    },
  }));
}

// Each test loads the module afresh, so none sees another's failed load.
beforeEach(() => {
  vi.resetModules();
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    browserDesignTools: vi.fn(),
    onBrowserDesignToolsEvent: vi.fn(() => () => {}),
  };
});

afterEach(() => {
  vi.doUnmock("./DesktopDesignTools");
  vi.restoreAllMocks();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

it("leaves the app as it is when the tools' chunk can't load", async () => {
  chunkFails();
  const { DesktopDesignTools } = await import("./LazyDesktopDesignTools");
  render(
    <AppBoundary>
      <p>Chat</p>
      <DesktopDesignTools viewId="conv_1" hasView url="" container={createRef()} />
    </AppBoundary>,
  );
  await new Promise((resolve) => {
    setTimeout(resolve, 20);
  });
  expect(screen.getByText("Chat")).toBeTruthy();
  expect(screen.queryByText("The app failed")).toBeNull();
});

it("says the tools are missing once their chunk can't load, so the pane offers design mode", async () => {
  chunkFails();
  const { DesktopDesignTools, hasDesktopDesignTools, useHasDesktopDesignTools } =
    await import("./LazyDesktopDesignTools");
  const has = renderHook(() => useHasDesktopDesignTools());
  expect(has.result.current).toBe(true);
  render(<DesktopDesignTools viewId="conv_1" hasView url="" container={createRef()} />);
  await waitFor(() => expect(has.result.current).toBe(false));
  expect(hasDesktopDesignTools()).toBe(false);
});

it("offers the tools once a later try loads their chunk", async () => {
  vi.useFakeTimers();
  try {
    const chunk = { broken: true };
    vi.doMock("./DesktopDesignTools", () => ({
      get DesktopDesignTools() {
        if (!chunk.broken) return () => <p>The tools</p>;
        throw new TypeError("Failed to fetch dynamically imported module");
      },
    }));
    const { DesktopDesignTools, useHasDesktopDesignTools } =
      await import("./LazyDesktopDesignTools");
    const { CHUNK_RETRY_MS } = await import("./retryingChunk");
    const has = renderHook(() => useHasDesktopDesignTools());
    render(<DesktopDesignTools viewId="conv_1" hasView url="" container={createRef()} />);
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(has.result.current).toBe(false);
    // The chunk loads again: the pane has the tools, not design mode.
    chunk.broken = false;
    await act(() => vi.advanceTimersByTimeAsync(CHUNK_RETRY_MS));
    expect(has.result.current).toBe(true);
    expect(screen.getByText("The tools")).toBeTruthy();
  } finally {
    vi.useRealTimers();
  }
});

it("says the tools are missing for good once they fail after loading", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.doMock("./DesktopDesignTools", () => ({
    DesktopDesignTools: () => {
      throw new Error("The tools broke");
    },
  }));
  const { DesktopDesignTools, useHasDesktopDesignTools } = await import("./LazyDesktopDesignTools");
  const has = renderHook(() => useHasDesktopDesignTools());
  expect(has.result.current).toBe(true);
  render(
    <AppBoundary>
      <p>Chat</p>
      <DesktopDesignTools viewId="conv_1" hasView url="" container={createRef()} />
    </AppBoundary>,
  );
  await waitFor(() => expect(has.result.current).toBe(false));
  expect(screen.queryByText("The app failed")).toBeNull();
  // A failure while running isn't a chunk to try again: no later try brings them back.
  vi.useFakeTimers();
  try {
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(has.result.current).toBe(false);
  } finally {
    vi.useRealTimers();
  }
});
