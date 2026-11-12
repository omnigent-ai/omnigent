// The browser pane says when its tab's page is open in
// another desktop window, instead of offering an empty page.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BrowserPane } from "@/components/BrowserPane/BrowserPane";
import { OPEN_ELSEWHERE_HINT } from "./OpenElsewhereHint";

vi.mock(import("@/lib/nativeBridge"), async (importOriginal) => ({
  ...(await importOriginal()),
  isElectronShell: () => true,
  supportsBrowser: () => true,
  isIOSShell: () => false,
  getDesktopFeatures: async () => null,
}));

function installBridge(browserHasView: ReturnType<typeof vi.fn>) {
  const unsub = () => {};
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    browserHasView,
    onBrowserViewCreated: vi.fn().mockReturnValue(unsub),
    onBrowserHostActiveChanged: vi.fn().mockReturnValue(unsub),
    onBrowserViewClosed: vi.fn().mockReturnValue(unsub),
    onBrowserUrlChanged: vi.fn().mockReturnValue(unsub),
    onBrowserNavState: vi.fn().mockReturnValue(unsub),
    browserSetActive: vi.fn().mockResolvedValue({ ok: true }),
    browserResize: vi.fn().mockResolvedValue({ ok: true }),
  };
}

afterEach(() => {
  cleanup();
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = undefined;
});

describe("BrowserPane for a tab whose page is in another window", () => {
  it("says so instead of offering an empty page", async () => {
    globalThis.ResizeObserver ??= class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    installBridge(vi.fn().mockResolvedValue({ exists: false, elsewhere: true }));
    render(<BrowserPane conversationId="browser-tab:conv_a:two" agentBrowser />);
    await waitFor(() => expect(screen.getByText(OPEN_ELSEWHERE_HINT)).toBeInTheDocument());
    expect(screen.queryByText(/Enter a URL above/)).toBeNull();
  });

  it("keeps the usual hint when no window has it", async () => {
    const hasView = vi.fn().mockResolvedValue({ exists: false, elsewhere: false });
    installBridge(hasView);
    render(<BrowserPane conversationId="browser-tab:conv_a:two" agentBrowser />);
    await waitFor(() => expect(hasView).toHaveBeenCalledTimes(2), { timeout: 2000 });
    expect(screen.getByText(/Enter a URL above to get started/)).toBeInTheDocument();
    expect(screen.queryByText(OPEN_ELSEWHERE_HINT)).toBeNull();
  });
});
