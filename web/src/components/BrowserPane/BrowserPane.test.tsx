import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type * as NativeBridge from "@/lib/nativeBridge";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserPane } from "./BrowserPane";

const getSessionSlim = vi.fn();
vi.mock("@/lib/sessionsApi", () => ({
  getSessionSlim: (...args: unknown[]) => getSessionSlim(...args),
}));

// supportsBrowser gates the whole pane. Force it true so the pane renders; the
// unsupported-shell (returns null) path is covered by reading the early return.
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isElectronShell: () => true,
  supportsBrowser: () => true,
  // DialogContent sizes itself for the iOS keyboard and asks for the shell.
  isIOSShell: () => false,
  // Delegate to the per-test bridge stub so a test can assert the call.
  getDesktopFeatures: async () => {
    const api = (window as unknown as { omnigentDesktop?: Record<string, unknown> })
      .omnigentDesktop;
    const read = api?.getDesktopFeatures as (() => Promise<unknown>) | undefined;
    return read ? await read() : null;
  },
}));

// The cookie-import flows report outcomes through toasts; capture them instead
// of rendering a Toaster.
const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), info: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

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
    // Overrides add methods the base stub doesn't declare (older shells lack
    // them), so the recorded mocks are reached through a loose index type.
  } as unknown as Record<string, ReturnType<typeof vi.fn>>;
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = bridge;
  return bridge;
}

beforeEach(() => {
  getSessionSlim.mockReset().mockImplementation(async (id: string) => ({
    id,
    hostId: null,
    parentSessionId: null,
  }));
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

  it("reattaches the retained page without navigating when its rail is reopened", async () => {
    let fireActive: ((p: { conversationId: string | null }) => void) | undefined;
    const bridge = installBridge({
      onBrowserHostActiveChanged: vi.fn((cb) => {
        fireActive = cb;
        return () => {};
      }),
      browserSetActive: vi.fn(async (conversationId: string | null) => {
        // The real shell broadcasts null when detaching, without destroying the page.
        fireActive?.({ conversationId });
        return { ok: true };
      }),
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

    rerender(<BrowserPane conversationId="conv_hidden" active />);
    await waitFor(() => expect(bridge.browserSetActive).toHaveBeenLastCalledWith("conv_hidden"));
    expect(screen.getByRole("textbox", { name: "Address bar" })).toHaveValue("https://example.com");
    expect(bridge.browserHasView).toHaveBeenCalledTimes(1);
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
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

describe("BrowserPane retained-view lifecycle", () => {
  it("does not reattach a view closed while the pane is hidden", async () => {
    let fireClosed: ((p: { conversationId: string; reason: string | null }) => void) | undefined;
    const bridge = installBridge({
      browserHasView: vi.fn().mockResolvedValue({ exists: true, url: "http://localhost:5173" }),
      onBrowserViewClosed: vi.fn((cb) => {
        fireClosed = cb;
        return () => {};
      }),
    });
    const { rerender } = render(<BrowserPane conversationId="conv_closed" />);
    await waitFor(() => expect(bridge.browserSetActive).toHaveBeenCalledWith("conv_closed"));
    rerender(<BrowserPane conversationId="conv_closed" active={false} />);
    act(() => fireClosed?.({ conversationId: "conv_closed", reason: "user" }));
    bridge.browserSetActive.mockClear();

    rerender(<BrowserPane conversationId="conv_closed" />);
    expect(screen.getByRole("button", { name: "Reload" })).toBeDisabled();
    expect(bridge.browserSetActive).not.toHaveBeenCalled();
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
  });

  it("ignores an existence probe that arrives after the view was closed", async () => {
    let resolveProbe: ((r: { exists: boolean; url: string }) => void) | undefined;
    let fireClosed: ((p: { conversationId: string; reason: string | null }) => void) | undefined;
    const bridge = installBridge({
      browserHasView: vi.fn(
        () =>
          new Promise((resolve) => {
            resolveProbe = resolve;
          }),
      ),
      onBrowserViewClosed: vi.fn((cb) => {
        fireClosed = cb;
        return () => {};
      }),
    });
    render(<BrowserPane conversationId="conv_closed" />);
    act(() => fireClosed?.({ conversationId: "conv_closed", reason: "user" }));
    await act(async () => resolveProbe?.({ exists: true, url: "http://localhost:5173" }));

    expect(screen.getByRole("button", { name: "Reload" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Address bar" })).toHaveValue("");
    expect(bridge.browserSetActive).not.toHaveBeenCalled();
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
  it("sends the owning session host and protocol for a manually typed localhost preview", async () => {
    getSessionSlim.mockResolvedValue({
      id: "conv_preview",
      hostId: "local-host",
      parentSessionId: null,
    });
    const bridge = installBridge();
    render(<BrowserPane conversationId="browser-tab:conv_preview:two" agentBrowser={false} />);
    const bar = screen.getByRole("textbox", { name: "Address bar" });
    fireEvent.change(bar, { target: { value: "localhost:5273/preview" } });
    fireEvent.keyDown(bar, { key: "Enter" });

    await waitFor(() =>
      expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
        "browser-tab:conv_preview:two",
        "http://localhost:5273/preview",
        undefined,
        { force: true, sourceHostId: "local-host", sessionHostProtocol: 1 },
      ),
    );
    expect(getSessionSlim).toHaveBeenCalledWith("conv_preview");
    expect(bar).toHaveValue("http://localhost:5273/preview");
  });

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

  it("resolves the current host on address-bar navigation after the session moves", async () => {
    getSessionSlim
      .mockResolvedValueOnce({ id: "conv_preview", hostId: "local-host", parentSessionId: null })
      .mockResolvedValue({ id: "conv_preview", hostId: "remote-host", parentSessionId: null });
    const bridge = installBridge();
    render(<BrowserPane conversationId="browser-tab:conv_preview:two" agentBrowser={false} />);
    const bar = screen.getByRole("textbox", { name: "Address bar" });
    fireEvent.change(bar, { target: { value: "http://127.0.0.1:5173/before" } });
    fireEvent.keyDown(bar, { key: "Enter" });
    await waitFor(() => expect(bridge.browserOpenOrNavigate).toHaveBeenCalledTimes(1));
    fireEvent.change(bar, { target: { value: "http://127.0.0.1:5173/after" } });
    fireEvent.keyDown(bar, { key: "Enter" });
    await waitFor(() =>
      expect(bridge.browserOpenOrNavigate).toHaveBeenLastCalledWith(
        "browser-tab:conv_preview:two",
        "http://127.0.0.1:5173/after",
        undefined,
        { force: true, sourceHostId: "remote-host", sessionHostProtocol: 1 },
      ),
    );
    expect(getSessionSlim).toHaveBeenCalledTimes(2);
  });

  it.each(["host lookup", "navigation result"])(
    "keeps the latest URL submission when the earlier %s finishes last",
    async (pendingStage) => {
      let resolveFirstHost!: (host: unknown) => void;
      const firstHost = new Promise((resolve) => {
        resolveFirstHost = resolve;
      });
      let resolveFirstNavigation!: (result: { ok: boolean; error?: string }) => void;
      const firstNavigation = new Promise<{ ok: boolean; error?: string }>((resolve) => {
        resolveFirstNavigation = resolve;
      });
      getSessionSlim
        .mockReturnValueOnce(
          pendingStage === "host lookup"
            ? firstHost
            : Promise.resolve({ id: "conv_preview", hostId: "local-host" }),
        )
        .mockResolvedValue({ id: "conv_preview", hostId: "remote-host" });
      const bridge = installBridge({
        browserOpenOrNavigate: vi
          .fn()
          .mockImplementationOnce(() =>
            pendingStage === "navigation result" ? firstNavigation : Promise.resolve({ ok: true }),
          )
          .mockResolvedValue({ ok: true }),
      });
      render(<BrowserPane conversationId="browser-tab:conv_preview:two" agentBrowser={false} />);
      const bar = screen.getByRole("textbox", { name: "Address bar" });
      fireEvent.change(bar, { target: { value: "https://first.example" } });
      fireEvent.keyDown(bar, { key: "Enter" });
      if (pendingStage === "navigation result") {
        await waitFor(() => expect(bridge.browserOpenOrNavigate).toHaveBeenCalledTimes(1));
      }
      fireEvent.change(bar, { target: { value: "https://latest.example" } });
      fireEvent.keyDown(bar, { key: "Enter" });
      await waitFor(() => expect(bar).toHaveValue("https://latest.example"));
      await waitFor(() =>
        expect(bridge.browserOpenOrNavigate).toHaveBeenLastCalledWith(
          "browser-tab:conv_preview:two",
          "https://latest.example",
          undefined,
          { force: true, sourceHostId: "remote-host", sessionHostProtocol: 1 },
        ),
      );
      await act(async () => {
        resolveFirstHost({ id: "conv_preview", hostId: "local-host" });
        resolveFirstNavigation({ ok: false, error: "stale failure" });
      });
      expect(bridge.browserOpenOrNavigate).toHaveBeenCalledTimes(
        pendingStage === "host lookup" ? 1 : 2,
      );
      expect(bar).toHaveValue("https://latest.example");
      expect(screen.queryByText("stale failure")).not.toBeInTheDocument();
    },
  );

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

describe("BrowserPane Chrome cookie import", () => {
  const HOST = "mail.google.com";
  const PARENT = "google.com";
  const PREVIEW = {
    ok: true as const,
    host: HOST,
    scopes: [HOST, PARENT],
    profiles: [
      {
        id: "Default",
        name: "Work",
        cookieCount: 4,
        scopes: [
          { host: HOST, cookieCount: 4 },
          { host: PARENT, cookieCount: 6 },
        ],
      },
    ],
  };

  /** Bridge whose cookie methods exist; `features` is what the shell reports. */
  function installCookieBridge({
    features = { chromeCookieImport: true },
    preview = PREVIEW,
    importResult = {
      ok: true as const,
      host: HOST,
      profileId: "Default",
      imported: 4,
      skipped: 0,
      failed: 0,
    },
  }: {
    features?: { chromeCookieImport?: boolean } | null;
    preview?: unknown;
    importResult?: unknown;
  } = {}) {
    return installBridge({
      browserHasView: vi.fn().mockResolvedValue({ exists: true, url: `https://${HOST}/mail/u/0` }),
      getDesktopFeatures: vi.fn().mockResolvedValue(features),
      browserChromeCookiePreview: vi.fn().mockResolvedValue(preview),
      browserChromeCookieImport: vi.fn().mockResolvedValue(importResult),
    });
  }

  const importButton = () => screen.queryByRole("button", { name: "Import cookies from Chrome" });

  it("hides the button on a shell that doesn't expose the bridge methods", async () => {
    // An older shell: no cookie methods at all, so the pane must not offer a
    // button that would reject. This is the compat boundary, not an error path.
    installBridge({
      browserHasView: vi.fn().mockResolvedValue({ exists: true, url: `https://${HOST}/` }),
    });
    render(<BrowserPane conversationId="conv_old_shell" />);
    await screen.findByRole("textbox", { name: "Address bar" });
    expect(importButton()).toBeNull();
  });

  it("hides the button when the shell reports the feature off", async () => {
    const bridge = installCookieBridge({ features: { chromeCookieImport: false } });
    render(<BrowserPane conversationId="conv_flag_off" />);
    await waitFor(() => expect(bridge.getDesktopFeatures).toHaveBeenCalled());
    // Wait for the probe to resolve, so this can't pass before the flag lands.
    await act(async () => {
      await bridge.getDesktopFeatures.mock.results[0].value;
    });
    expect(importButton()).toBeNull();
  });

  it("previews the current site and imports the picked profile into this tab", async () => {
    const bridge = installCookieBridge();
    render(<BrowserPane conversationId="conv_cookie" />);

    fireEvent.click(await screen.findByRole("button", { name: "Import cookies from Chrome" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getAllByText(HOST).length).toBeGreaterThan(0);
    expect(within(dialog).getByText("Work")).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        `4 cookies for ${HOST} and its subdomains. Other sites' cookies stay out of Omnigent entirely.`,
      ),
    ).toBeInTheDocument();
    expect(bridge.browserChromeCookiePreview).toHaveBeenCalledWith("conv_cookie");
    // The preview alone must never write cookies.
    expect(bridge.browserChromeCookieImport).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Import cookies" }));

    await waitFor(() =>
      expect(bridge.browserChromeCookieImport).toHaveBeenCalledWith("conv_cookie", {
        profileId: "Default",
        expectedHost: HOST,
        scope: HOST,
      }),
    );
    // A reload makes the site pick up the new session cookies.
    await waitFor(() => expect(bridge.browserReload).toHaveBeenCalledWith("conv_cookie"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(toast.success).toHaveBeenCalledWith("Imported 4 cookies from Chrome.");
  });

  it("offers the page's host and its parent domain, and imports the picked one", async () => {
    const bridge = installCookieBridge();
    render(<BrowserPane conversationId="conv_scope" />);

    fireEvent.click(await screen.findByRole("button", { name: "Import cookies from Chrome" }));
    const dialog = await screen.findByRole("dialog");

    // Counts follow the scope: the narrow scope is a subset of the wider one's
    // tree, not a copy of it.
    const scopePicker = within(dialog).getByRole("combobox", { name: "Site scope" });
    fireEvent.click(scopePicker);
    fireEvent.click(await screen.findByRole("option", { name: `${PARENT} (6)` }));

    expect(dialog).toHaveTextContent(`6 cookies for ${PARENT} and its subdomains`);
    expect(
      within(dialog).getByText(/Cookies from other subdomains stay bound to their own host/),
    ).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Import cookies" }));

    await waitFor(() =>
      expect(bridge.browserChromeCookieImport).toHaveBeenCalledWith("conv_scope", {
        profileId: "Default",
        expectedHost: HOST,
        scope: PARENT,
      }),
    );
  });

  it("shows the site as plain text when the host has no parent domain", async () => {
    installCookieBridge({
      preview: {
        ok: true,
        host: "example.com",
        scopes: ["example.com"],
        profiles: [{ id: "Default", name: "Work", cookieCount: 2 }],
      },
    });
    render(<BrowserPane conversationId="conv_noparent" />);

    fireEvent.click(await screen.findByRole("button", { name: "Import cookies from Chrome" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("combobox", { name: "Site scope" })).toBeNull();
    expect(dialog).toHaveTextContent("2 cookies for example.com");
  });

  it("says so instead of opening a dialog when Chrome has nothing for the site", async () => {
    const bridge = installCookieBridge({ preview: { ok: true, host: HOST, profiles: [] } });
    render(<BrowserPane conversationId="conv_nocookie" />);

    fireEvent.click(await screen.findByRole("button", { name: "Import cookies from Chrome" }));

    await waitFor(() => expect(bridge.browserChromeCookiePreview).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(toast.info).toHaveBeenCalledWith(`No Chrome cookies found for ${HOST}.`);
    // The button returns to its normal (clickable) state.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Import cookies from Chrome" })).toBeEnabled(),
    );
  });

  it("shows the shell's preview error rather than a dialog", async () => {
    installCookieBridge({
      preview: { ok: false, error: "Could not read Chrome's cookie database." },
    });
    render(<BrowserPane conversationId="conv_unreadable" />);

    fireEvent.click(await screen.findByRole("button", { name: "Import cookies from Chrome" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Could not read Chrome's cookie database."),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("always warns that the conversation's tabs share the jar", async () => {
    // Even a user-controlled tab shares the conversation's partition with any
    // tab the agent opens, so the warning is not conditional on agentBrowser.
    installCookieBridge();
    render(<BrowserPane conversationId="conv_agent" agentBrowser={false} />);

    fireEvent.click(await screen.findByRole("button", { name: "Import cookies from Chrome" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(/tabs share one cookie jar, including any tab the agent controls/),
    ).toBeInTheDocument();
  });

  it("stays silent when the user declines the shell's confirmation", async () => {
    const bridge = installCookieBridge({ importResult: { ok: false, cancelled: true } });
    render(<BrowserPane conversationId="conv_cancel" />);

    fireEvent.click(await screen.findByRole("button", { name: "Import cookies from Chrome" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Import cookies" }),
    );

    await waitFor(() => expect(bridge.browserChromeCookieImport).toHaveBeenCalled());
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.info).not.toHaveBeenCalled();
    expect(bridge.browserReload).not.toHaveBeenCalled();
    // Still open so the user can retry without reopening the pane's dialog.
    expect(screen.queryByRole("dialog")).not.toBeNull();
  });

  it("narrows the scope when the picked profile can't cover the wider one", async () => {
    const bridge = installCookieBridge({
      preview: {
        ok: true,
        host: HOST,
        scopes: [HOST, PARENT],
        profiles: [
          {
            id: "Default",
            name: "Work",
            cookieCount: 4,
            scopes: [
              { host: HOST, cookieCount: 4 },
              { host: PARENT, cookieCount: 6 },
            ],
          },
          // Profile 2 has nothing on the parent domain, so picking it must not
          // leave the wider scope selected.
          {
            id: "Profile 2",
            name: "Personal",
            cookieCount: 1,
            scopes: [{ host: HOST, cookieCount: 1 }],
          },
        ],
      },
    });
    render(<BrowserPane conversationId="conv_reset" />);

    fireEvent.click(await screen.findByRole("button", { name: "Import cookies from Chrome" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Site scope" }));
    fireEvent.click(await screen.findByRole("option", { name: `${PARENT} (6)` }));
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Chrome profile" }));
    fireEvent.click(await screen.findByRole("option", { name: "Personal (1)" }));

    // The wider scope is gone from the picker, so the import targets the host.
    expect(within(dialog).queryByRole("combobox", { name: "Site scope" })).toBeNull();
    expect(dialog).toHaveTextContent(`1 cookie for ${HOST}`);
    fireEvent.click(within(dialog).getByRole("button", { name: "Import cookies" }));
    await waitFor(() =>
      expect(bridge.browserChromeCookieImport).toHaveBeenCalledWith("conv_reset", {
        profileId: "Profile 2",
        expectedHost: HOST,
        scope: HOST,
      }),
    );
  });

  it("explains a zero-cookie import that the session rejected", async () => {
    installCookieBridge({
      importResult: {
        ok: true,
        host: HOST,
        profileId: "Default",
        imported: 0,
        skipped: 0,
        failed: 2,
      },
    });
    render(<BrowserPane conversationId="conv_rejected" />);

    fireEvent.click(await screen.findByRole("button", { name: "Import cookies from Chrome" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Import cookies" }),
    );

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        `Chrome's cookies for ${HOST} couldn't be saved in this browser session.`,
      ),
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("keeps the dialog open and explains a zero-cookie import", async () => {
    // Chrome had rows, but none could be decrypted/replayed for this host.
    installCookieBridge({
      importResult: {
        ok: true,
        host: HOST,
        profileId: "Default",
        imported: 0,
        skipped: 3,
        failed: 0,
      },
    });
    render(<BrowserPane conversationId="conv_zero" />);

    fireEvent.click(await screen.findByRole("button", { name: "Import cookies from Chrome" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Import cookies" }),
    );

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(`Chrome had no usable cookies for ${HOST}.`),
    );
    expect(screen.queryByRole("dialog")).not.toBeNull();
  });
});
