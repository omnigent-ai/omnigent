import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserPane } from "./BrowserPane";

// isElectronShell gates the whole pane. Force it true so the pane renders; the
// plain-browser (returns null) path is covered by reading the early return.
vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: () => true,
}));

/**
 * Minimal `window.omnigentDesktop` stub. The empty-state tests only need the
 * subscription methods to exist (they return no-op unsubscribes) and
 * `browserHasView` to resolve "no view", so `viewActive` stays false and the
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
  // jsdom has no ResizeObserver; the measuring-container effect (viewActive path)
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
  it("renders the URL bar in the empty state so the first page is reachable", async () => {
    render(<BrowserPane conversationId="conv_a" />);

    // The address bar must be present with no view attached — this is the whole
    // point of the fix: gating it on viewActive made it unreachable from a cold
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
    // drive viewActive → true, proving the toolbar stays and the hint is
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

  it("calls enable then disable IPC as it toggles, once a view is active", async () => {
    let fireCreated: ((p: { conversationId: string }) => void) | undefined;
    const bridge = installBridge({
      onBrowserViewCreated: vi.fn((cb: (p: { conversationId: string }) => void) => {
        fireCreated = cb;
        return () => {};
      }),
    });

    render(<BrowserPane conversationId="conv_dm3" />);
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
