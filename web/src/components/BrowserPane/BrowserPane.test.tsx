import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { EventEmitter } from "node:events";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// @ts-expect-error The Electron registry is an untyped CommonJS module.
import { createBrowserViewRegistry } from "../../../electron/src/browserViewRegistry.js";
import { BrowserPane } from "./BrowserPane";

// supportsBrowser gates the whole pane. Force it true so the pane renders; the
// unsupported-shell (returns null) path is covered by reading the early return.
vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: () => true,
  supportsBrowser: () => true,
}));

/**
 * Minimal `window.omnigentDesktop` stub. The empty-state tests only need the
 * subscription methods to exist (they return no-op unsubscribes) and
 * `browserHasView` to resolve "no view", so `viewExists` stays false and the
 * pane renders its cold-start (no-page-open) state — exactly the state the
 * regression made unreachable.
 */
function installBridge(overrides: Record<string, unknown> = {}) {
  const noopUnsub = () => {};
  const bridge = {
    browserHasView: vi.fn().mockResolvedValue({ exists: false }),
    onBrowserViewCreated: vi.fn().mockReturnValue(noopUnsub),
    onBrowserHostActiveChanged: vi.fn().mockReturnValue(noopUnsub),
    onBrowserViewClosed: vi.fn().mockReturnValue(noopUnsub),
    onBrowserUrlChanged: vi.fn().mockReturnValue(noopUnsub),
    onBrowserNavState: vi.fn().mockReturnValue(noopUnsub),
    browserSetActive: vi.fn().mockResolvedValue({ ok: true }),
    browserResize: vi.fn().mockResolvedValue({ ok: true }),
    browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: true, created: true }),
    browserGoBack: vi.fn().mockResolvedValue({ ok: true }),
    browserGoForward: vi.fn().mockResolvedValue({ ok: true }),
    browserReload: vi.fn().mockResolvedValue({ ok: true }),
    openBrowserDevTools: vi.fn().mockResolvedValue({ ok: true }),
    browserEnableDesignMode: vi.fn().mockResolvedValue({ ok: true }),
    browserDisableDesignMode: vi.fn().mockResolvedValue({ ok: true }),
    ...overrides,
  };
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = bridge;
  return bridge;
}

beforeEach(() => {
  // jsdom has no ResizeObserver; the measuring-container effect (viewExists path)
  // constructs one. Stub it so mounting the container doesn't throw.
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  installBridge();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = undefined;
});

describe("BrowserPane cold-start (no view yet)", () => {
  it("does not claim that the agent navigates user-created tabs", () => {
    render(<BrowserPane conversationId="browser-tab:conv_a:two" agentBrowser={false} />);
    expect(screen.getByText("Enter a URL above to get started.")).toBeInTheDocument();
    expect(screen.queryByText(/the agent will open pages here too/)).toBeNull();
  });

  it("restores a tab's URL and navigation state from its existing native view", async () => {
    const bridge = installBridge({
      browserHasView: vi.fn().mockResolvedValue({
        exists: true,
        url: "https://example.com/tab-two",
        canGoBack: true,
        canGoForward: false,
      }),
    });
    render(<BrowserPane conversationId="browser-tab:conv_a:two" />);
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Address bar" })).toHaveValue(
        "https://example.com/tab-two",
      ),
    );
    expect(screen.getByRole("button", { name: "Go back" })).toBeEnabled();
    await waitFor(() =>
      expect(bridge.browserSetActive).toHaveBeenCalledWith("browser-tab:conv_a:two"),
    );
    cleanup();
    expect(bridge.browserSetActive).toHaveBeenLastCalledWith(null);
  });

  it("detaches the native view when its always-mounted rail becomes inactive", async () => {
    const bridge = installBridge({
      browserHasView: vi.fn().mockResolvedValue({
        exists: true,
        url: "https://example.com",
        canGoBack: false,
        canGoForward: false,
      }),
    });
    const { rerender } = render(<BrowserPane conversationId="conv_hidden" active />);
    await waitFor(() => expect(bridge.browserSetActive).toHaveBeenCalledWith("conv_hidden"));

    rerender(<BrowserPane conversationId="conv_hidden" active={false} />);

    await waitFor(() => expect(bridge.browserSetActive).toHaveBeenLastCalledWith(null));
    const resizeCount = bridge.browserResize.mock.calls.length;
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });
    expect(bridge.browserResize).toHaveBeenCalledTimes(resizeCount);
  });

  it("reports native view-cap failures instead of silently leaving a blank tab", async () => {
    installBridge({
      browserOpenOrNavigate: vi
        .fn()
        .mockResolvedValue({ ok: false, error: "browser view cap reached — close one" }),
    });
    render(<BrowserPane conversationId="browser-tab:conv_a:two" />);
    const address = screen.getByRole("textbox", { name: "Address bar" });
    fireEvent.change(address, { target: { value: "example.com" } });
    fireEvent.keyDown(address, { key: "Enter" });
    expect(await screen.findByRole("alert")).toHaveTextContent("browser view cap reached");
    expect(address).toHaveValue("example.com");
  });

  it("renders the URL bar in the empty state so the first page is reachable", async () => {
    render(<BrowserPane conversationId="conv_a" />);

    // The address bar must be present with no view attached — this is the whole
    // point of the fix: gating it on view existence made it unreachable from a cold
    // start (no page → no bar → no way to open the first page).
    const urlBar = await screen.findByRole("textbox", { name: /address bar/i });
    expect(urlBar).toBeInTheDocument();
    expect(urlBar).not.toBeDisabled();

    // The cold-start hint is shown instead of the measuring container.
    expect(screen.getByText(/enter a url above to get started/i)).toBeInTheDocument();
  });

  it("disables reload and devtools while no view is attached", async () => {
    render(<BrowserPane conversationId="conv_b" />);

    // Nothing to reload / no devtools target with no view — both disabled.
    await screen.findByRole("textbox", { name: /address bar/i });
    expect(screen.getByRole("button", { name: /reload/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /toggle devtools/i })).toBeDisabled();
  });

  it("disables back and forward while no view is attached", async () => {
    render(<BrowserPane conversationId="conv_c" />);

    // canGoBack/canGoForward start false with no view, so the arrows are off.
    await screen.findByRole("textbox", { name: /address bar/i });
    expect(screen.getByRole("button", { name: /go back/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /go forward/i })).toBeDisabled();
  });

  it("shows the measuring container (not the hint) once a view is created", async () => {
    // Capture the browser-view-created callback so the test can fire it and
    // drive viewExists → true, proving the toolbar stays and the hint is
    // replaced by the measuring region.
    let fireCreated: ((p: { conversationId: string }) => void) | undefined;
    installBridge({
      onBrowserViewCreated: vi.fn((cb: (p: { conversationId: string }) => void) => {
        fireCreated = cb;
        return () => {};
      }),
    });

    render(<BrowserPane conversationId="conv_d" />);
    await screen.findByRole("textbox", { name: /address bar/i });
    expect(screen.getByText(/enter a url above to get started/i)).toBeInTheDocument();

    fireCreated?.({ conversationId: "conv_d" });

    // The hint disappears (measuring container takes over) but the URL bar — the
    // always-present toolbar — is still there.
    await waitFor(() => {
      expect(screen.queryByText(/enter a url above to get started/i)).toBeNull();
    });
    expect(screen.getByRole("textbox", { name: /address bar/i })).toBeInTheDocument();
  });
});

describe("BrowserPane localhost preview lifecycle", () => {
  function installLifecycleBridge() {
    let fireCreated: ((p: { conversationId: string }) => void) | undefined;
    let fireClosed: ((p: { conversationId: string; reason: string | null }) => void) | undefined;
    installBridge({
      browserHasView: vi.fn().mockResolvedValue({
        exists: true,
        url: "http://localhost:5173/stale",
        canGoBack: true,
        canGoForward: true,
      }),
      onBrowserViewCreated: vi.fn((cb: (p: { conversationId: string }) => void) => {
        fireCreated = cb;
        return () => {};
      }),
      onBrowserViewClosed: vi.fn(
        (cb: (p: { conversationId: string; reason: string | null }) => void) => {
          fireClosed = cb;
          return () => {};
        },
      ),
    });
    return { fireCreated: () => fireCreated, fireClosed: () => fireClosed };
  }

  it("marks an expired preview unavailable without affecting another conversation", async () => {
    const events = installLifecycleBridge();
    render(<BrowserPane conversationId="conv_preview" />);
    const address = await screen.findByRole("textbox", { name: "Address bar" });
    await waitFor(() => expect(address).toHaveValue("http://localhost:5173/stale"));

    act(() => {
      events.fireClosed()?.({ conversationId: "conv_other", reason: "preview-expired" });
    });
    expect(address).toHaveValue("http://localhost:5173/stale");
    expect(screen.queryByRole("alert")).toBeNull();

    act(() => {
      events.fireClosed()?.({ conversationId: "conv_preview", reason: "preview-expired" });
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "This localhost preview expired. Ask the agent to open it again.",
    );
    expect(address).toHaveValue("");
    expect(screen.getByRole("button", { name: "Go back" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Go forward" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reload" })).toBeDisabled();

    act(() => {
      events.fireCreated()?.({ conversationId: "conv_preview" });
    });
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("explains when the preview connection exits", async () => {
    const events = installLifecycleBridge();
    render(<BrowserPane conversationId="conv_preview" />);
    await screen.findByRole("textbox", { name: "Address bar" });
    act(() => {
      events.fireClosed()?.({ conversationId: "conv_preview", reason: "preview-exited" });
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "This localhost preview is unavailable because its secure connection closed.",
    );
  });
});

describe("BrowserPane retained native view lifecycle", () => {
  interface ViewEventPayload {
    conversationId: string | null;
    reason?: string | null;
  }

  function installRegistryBridge() {
    const listeners = new Map<string, Set<(payload: ViewEventPayload) => void>>();
    const attachedViews: unknown[] = [];
    const detachedViews: unknown[] = [];
    const loadURL = vi.fn().mockResolvedValue(undefined);
    const page = {
      webContents: Object.assign(new EventEmitter(), {
        loadURL,
        close: vi.fn(),
        setWindowOpenHandler: vi.fn(),
      }),
      setVisible: vi.fn(),
      setBounds: vi.fn(),
    };
    let releaseCount = 0;
    const deliveredEvents: { channel: string; payload: ViewEventPayload }[] = [];
    const registry = createBrowserViewRegistry({
      WebContentsViewCtor: () => page,
      createBoundsController: () => ({
        setRendererBounds: vi.fn(),
        clear: vi.fn(),
        resync: vi.fn(),
      }),
      attachToHost: (view: unknown) => attachedViews.push(view),
      detachFromHost: (view: unknown) => detachedViews.push(view),
      sendToRenderer: (channel: string, payload: ViewEventPayload) => {
        queueMicrotask(() => {
          deliveredEvents.push({ channel, payload });
          listeners.get(channel)?.forEach((callback) => callback(payload));
        });
      },
      partitionScope: "browser-pane-test",
    });
    const opened = registry.openOrNavigate("conv_preview", "http://localhost:5173/app", undefined, {
      agent: true,
      ownedOrigin: "http://localhost:5173",
      releaseOwnedOrigin: () => {
        releaseCount += 1;
      },
    });
    expect(opened.ok).toBe(true);

    const subscribe = (channel: string, callback: (payload: ViewEventPayload) => void) => {
      const channelListeners = listeners.get(channel) ?? new Set();
      channelListeners.add(callback);
      listeners.set(channel, channelListeners);
      return () => channelListeners.delete(callback);
    };
    const bridge = installBridge({
      browserHasView: vi.fn(async (conversationId: string) => ({
        exists: registry.has(conversationId),
        url: "http://localhost:5173/app",
        canGoBack: false,
        canGoForward: false,
      })),
      browserSetActive: vi.fn(async (conversationId: string | null) =>
        registry.setActive(conversationId),
      ),
      onBrowserHostActiveChanged: vi.fn((callback: (payload: ViewEventPayload) => void) =>
        subscribe("browser-host-active-changed", callback),
      ),
      onBrowserViewCreated: vi.fn((callback: (payload: ViewEventPayload) => void) =>
        subscribe("browser-view-created", callback),
      ),
      onBrowserViewClosed: vi.fn((callback: (payload: ViewEventPayload) => void) =>
        subscribe("browser-view-closed", callback),
      ),
    });
    return {
      attachedViews,
      bridge,
      deliveredEvents,
      detachedViews,
      loadURL,
      page,
      registry,
      releaseCount: () => releaseCount,
    };
  }

  it("reattaches the same retained page after an asynchronous rail detach", async () => {
    const fixture = installRegistryBridge();
    const { rerender } = render(<BrowserPane conversationId="conv_preview" active />);
    await waitFor(() => expect(fixture.registry.activeConversationId()).toBe("conv_preview"));
    expect(fixture.attachedViews).toEqual([fixture.page]);

    rerender(<BrowserPane conversationId="conv_preview" active={false} />);
    await waitFor(() => expect(fixture.registry.activeConversationId()).toBeNull());
    await waitFor(() =>
      expect(fixture.deliveredEvents).toContainEqual({
        channel: "browser-host-active-changed",
        payload: { conversationId: null },
      }),
    );

    rerender(<BrowserPane conversationId="conv_preview" active />);
    await waitFor(() => expect(fixture.registry.activeConversationId()).toBe("conv_preview"));
    expect(fixture.attachedViews).toEqual([fixture.page, fixture.page]);
    expect(fixture.detachedViews).toEqual([fixture.page]);
    expect(fixture.loadURL).toHaveBeenCalledTimes(1);
    expect(fixture.bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect(fixture.releaseCount()).toBe(0);
  });

  it("does not resurrect a preview that closes while the rail is hidden", async () => {
    const fixture = installRegistryBridge();
    const { rerender } = render(<BrowserPane conversationId="conv_preview" active />);
    await waitFor(() => expect(fixture.registry.activeConversationId()).toBe("conv_preview"));

    rerender(<BrowserPane conversationId="conv_preview" active={false} />);
    await waitFor(() => expect(fixture.registry.activeConversationId()).toBeNull());
    act(() => {
      fixture.registry.close("conv_preview", "preview-exited");
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This localhost preview is unavailable because its secure connection closed.",
    );
    expect(screen.getByRole("textbox", { name: "Address bar" })).toHaveValue("");

    rerender(<BrowserPane conversationId="conv_preview" active />);
    await act(async () => Promise.resolve());
    expect(fixture.registry.activeConversationId()).toBeNull();
    expect(fixture.attachedViews).toEqual([fixture.page]);
    expect(fixture.releaseCount()).toBe(1);
    expect(fixture.bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
  });

  it("ignores a stale existence probe after the retained preview closes", async () => {
    const fixture = installRegistryBridge();
    let resolveProbe: ((result: { exists: boolean; url: string }) => void) | undefined;
    fixture.bridge.browserHasView.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveProbe = resolve;
        }),
    );
    const { rerender } = render(<BrowserPane conversationId="conv_preview" active />);
    await waitFor(() => expect(resolveProbe).toBeDefined());
    await waitFor(() => expect(fixture.registry.activeConversationId()).toBe("conv_preview"));
    rerender(<BrowserPane conversationId="conv_preview" active={false} />);
    await waitFor(() => expect(fixture.registry.activeConversationId()).toBeNull());

    act(() => {
      fixture.registry.close("conv_preview", "preview-expired");
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This localhost preview expired. Ask the agent to open it again.",
    );
    await act(async () => {
      resolveProbe?.({ exists: true, url: "http://localhost:5173/app" });
      await Promise.resolve();
    });
    rerender(<BrowserPane conversationId="conv_preview" active />);
    await act(async () => Promise.resolve());

    expect(fixture.registry.activeConversationId()).toBeNull();
    expect(fixture.attachedViews).toEqual([fixture.page]);
    expect(screen.getByRole("textbox", { name: "Address bar" })).toHaveValue("");
    expect(fixture.releaseCount()).toBe(1);
  });

  it("reattaches the retained page after a true unmount and remount", async () => {
    const fixture = installRegistryBridge();
    const first = render(<BrowserPane conversationId="conv_preview" active />);
    await waitFor(() => expect(fixture.registry.activeConversationId()).toBe("conv_preview"));
    first.unmount();
    await waitFor(() => expect(fixture.registry.activeConversationId()).toBeNull());

    render(<BrowserPane conversationId="conv_preview" active />);
    await waitFor(() => expect(fixture.registry.activeConversationId()).toBe("conv_preview"));
    expect(fixture.attachedViews).toEqual([fixture.page, fixture.page]);
    expect(fixture.loadURL).toHaveBeenCalledTimes(1);
    expect(fixture.bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect(fixture.releaseCount()).toBe(0);
  });
});

describe("BrowserPane design-mode toggle", () => {
  it("renders the design-mode toggle in the toolbar", async () => {
    render(<BrowserPane conversationId="conv_dm1" />);
    await screen.findByRole("textbox", { name: /address bar/i });
    expect(screen.getByRole("button", { name: /enter design mode/i })).toBeInTheDocument();
  });

  it("disables the design-mode toggle while no view is attached", async () => {
    render(<BrowserPane conversationId="conv_dm2" />);
    await screen.findByRole("textbox", { name: /address bar/i });
    // No injected picker target without a view — the button is disabled, same
    // as reload / devtools.
    expect(screen.getByRole("button", { name: /enter design mode/i })).toBeDisabled();
  });

  it("calls enable then disable IPC as it toggles, and disables on unmount", async () => {
    let fireCreated: ((p: { conversationId: string }) => void) | undefined;
    const bridge = installBridge({
      onBrowserViewCreated: vi.fn((cb: (p: { conversationId: string }) => void) => {
        fireCreated = cb;
        return () => {};
      }),
    });

    const { unmount } = render(<BrowserPane conversationId="conv_dm3" />);
    await screen.findByRole("textbox", { name: /address bar/i });

    // Activate a view so the toggle is enabled.
    fireCreated?.({ conversationId: "conv_dm3" });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /enter design mode/i })).not.toBeDisabled();
    });

    // First click enables design mode (button flips to aria-pressed + "exit").
    screen.getByRole("button", { name: /enter design mode/i }).click();
    await waitFor(() => {
      expect(bridge.browserEnableDesignMode).toHaveBeenCalledWith("conv_dm3");
    });
    const pressed = screen.getByRole("button", { name: /exit design mode/i });
    expect(pressed).toHaveAttribute("aria-pressed", "true");

    // Second click disables it again.
    pressed.click();
    await waitFor(() => {
      expect(bridge.browserDisableDesignMode).toHaveBeenCalledWith("conv_dm3");
    });
    expect(screen.getByRole("button", { name: /enter design mode/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    screen.getByRole("button", { name: /enter design mode/i }).click();
    await waitFor(() => {
      expect(bridge.browserEnableDesignMode).toHaveBeenCalledTimes(2);
    });
    unmount();
    expect(bridge.browserDisableDesignMode).toHaveBeenCalledTimes(2);
  });
});

describe("BrowserPane toolbar navigation + URL bar", () => {
  /** Render the pane, activate a view (so toolbar buttons enable), and return
   *  the bridge + handles to the captured event callbacks. */
  async function renderActive(conversationId: string, overrides: Record<string, unknown> = {}) {
    let fireCreated: ((p: { conversationId: string }) => void) | undefined;
    let fireUrl: ((p: { conversationId: string; url: string }) => void) | undefined;
    let fireNav:
      | ((p: { conversationId: string; canGoBack: boolean; canGoForward: boolean }) => void)
      | undefined;
    const bridge = installBridge({
      onBrowserViewCreated: vi.fn((cb: (p: { conversationId: string }) => void) => {
        fireCreated = cb;
        return () => {};
      }),
      onBrowserUrlChanged: vi.fn((cb: (p: { conversationId: string; url: string }) => void) => {
        fireUrl = cb;
        return () => {};
      }),
      onBrowserNavState: vi.fn(
        (
          cb: (p: { conversationId: string; canGoBack: boolean; canGoForward: boolean }) => void,
        ) => {
          fireNav = cb;
          return () => {};
        },
      ),
      ...overrides,
    });
    render(<BrowserPane conversationId={conversationId} />);
    await screen.findByRole("textbox", { name: /address bar/i });
    fireCreated?.({ conversationId });
    await waitFor(() => expect(screen.getByRole("button", { name: /reload/i })).not.toBeDisabled());
    return { bridge, fireUrl: () => fireUrl, fireNav: () => fireNav };
  }

  it("reload button calls the reload IPC once a view is active", async () => {
    const { bridge } = await renderActive("conv_reload");
    screen.getByRole("button", { name: /reload/i }).click();
    await waitFor(() => expect(bridge.browserReload).toHaveBeenCalledWith("conv_reload"));
  });

  it("devtools button calls the open-devtools IPC", async () => {
    const { bridge } = await renderActive("conv_dt");
    screen.getByRole("button", { name: /toggle devtools/i }).click();
    await waitFor(() => expect(bridge.openBrowserDevTools).toHaveBeenCalledWith("conv_dt"));
  });

  it("back/forward buttons enable when nav-state reports history available", async () => {
    const { fireNav } = await renderActive("conv_hist");

    // Both arrows start disabled (canGoBack/Forward false).
    expect(screen.getByRole("button", { name: /go back/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /go forward/i })).toBeDisabled();

    // A nav-state event enabling history flips both buttons on — the
    // browser-nav-state SSE → setCanGoBack/Forward → disabled-prop chain.
    act(() => {
      fireNav()?.({ conversationId: "conv_hist", canGoBack: true, canGoForward: true });
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /go back/i })).not.toBeDisabled(),
    );
    expect(screen.getByRole("button", { name: /go forward/i })).not.toBeDisabled();
  });

  it("the URL bar reflects the real url pushed by browser-url-changed", async () => {
    const { fireUrl } = await renderActive("conv_url");
    act(() => {
      fireUrl()?.({ conversationId: "conv_url", url: "https://myhost/landed" });
    });
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: /address bar/i })).toHaveValue(
        "https://myhost/landed",
      ),
    );
  });

  it("submitting a dotless address normalizes it to http:// and navigates", async () => {
    const { bridge } = await renderActive("conv_nav");
    const bar = screen.getByRole("textbox", { name: /address bar/i }) as HTMLInputElement;

    bar.focus();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
    setter?.call(bar, "myhost");
    bar.dispatchEvent(new Event("input", { bubbles: true }));
    bar.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    await waitFor(() =>
      expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
        "conv_nav",
        "http://myhost",
        undefined,
        { force: true },
      ),
    );
  });
});
