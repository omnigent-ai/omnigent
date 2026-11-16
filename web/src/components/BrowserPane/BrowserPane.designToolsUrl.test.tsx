// The page the desktop design tools name in their requests: the one the view has loaded,
// never an address still being typed in the bar.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as NativeBridge from "@/lib/nativeBridge";
import { BrowserPane } from "./BrowserPane";

vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isElectronShell: () => true,
  supportsBrowser: () => true,
  isIOSShell: () => false,
  getDesktopFeatures: async () => null,
}));
// The tools themselves, reduced to the page they were handed.
vi.mock("@/components/BrowserPane/designTools/LazyDesktopDesignTools", () => ({
  hasDesktopDesignTools: () => true,
  useHasDesktopDesignTools: () => true,
  DesktopDesignTools: ({ url }: { url: string }) => <output aria-label="Tools' page">{url}</output>,
}));

afterEach(() => {
  cleanup();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("BrowserPane's design tools", () => {
  it("name the page the view loaded, not what's typed in the address bar", async () => {
    let urlChanged: (payload: { conversationId: string; url: string }) => void = () => {};
    const unsubscribe = () => {};
    (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
      browserHasView: vi.fn().mockResolvedValue({ exists: true, url: "https://a.example/" }),
      onBrowserViewCreated: () => unsubscribe,
      onBrowserHostActiveChanged: () => unsubscribe,
      onBrowserViewClosed: () => unsubscribe,
      onBrowserUrlChanged: (callback: typeof urlChanged) => {
        urlChanged = callback;
        return unsubscribe;
      },
      onBrowserNavState: () => unsubscribe,
      browserSetActive: vi.fn().mockResolvedValue({ ok: true }),
      browserResize: vi.fn().mockResolvedValue({ ok: true }),
    };
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
      observe() {}
      disconnect() {}
    };
    render(<BrowserPane conversationId="conv_url" />);
    const tools = await screen.findByLabelText("Tools' page");
    await act(async () => {});
    expect(tools).toHaveTextContent("https://a.example/");
    const bar = screen.getByRole("textbox", { name: "Address bar" });
    fireEvent.focus(bar);
    fireEvent.change(bar, { target: { value: "https://b.example/" } });
    expect(tools).toHaveTextContent("https://a.example/");
    // The page navigates on its own while the bar is being edited.
    act(() => urlChanged({ conversationId: "conv_url", url: "https://c.example/" }));
    expect(tools).toHaveTextContent("https://c.example/");
    expect(bar).toHaveValue("https://b.example/");
  });
});
