import { renderHook, render, screen, waitFor } from "@testing-library/react";
import { Component, createRef, type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

// The tools' chunk can't load (a deploy replaced it, the network dropped).
vi.mock("./DesktopDesignTools", () => ({
  get DesktopDesignTools() {
    throw new TypeError(
      "Failed to fetch dynamically imported module: /assets/DesktopDesignTools.js",
    );
  },
}));
import {
  DesktopDesignTools,
  hasDesktopDesignTools,
  useHasDesktopDesignTools,
} from "./LazyDesktopDesignTools";

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

afterEach(() => {
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

it("leaves the app as it is when the tools' chunk can't load", async () => {
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    browserDesignTools: vi.fn(),
    onBrowserDesignToolsEvent: vi.fn(() => () => {}),
  };
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
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    browserDesignTools: vi.fn(),
    onBrowserDesignToolsEvent: vi.fn(() => () => {}),
  };
  // Loaded already (the test above), or loading now.
  const has = renderHook(() => useHasDesktopDesignTools());
  render(<DesktopDesignTools viewId="conv_1" hasView url="" container={createRef()} />);
  await waitFor(() => expect(has.result.current).toBe(false));
  expect(hasDesktopDesignTools()).toBe(false);
});
