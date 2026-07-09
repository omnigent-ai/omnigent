import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
