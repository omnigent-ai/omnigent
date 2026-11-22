// The page the desktop design tools name in their requests: the one the view has loaded,
// never an address still being typed in the bar. And design mode, offered while they can't load.
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
// The tools themselves, reduced to the page they were handed, and whether they can load.
const toolsState = vi.hoisted(() => ({ available: true, listeners: new Set<() => void>() }));
vi.mock("@/components/BrowserPane/designTools/LazyDesktopDesignTools", async () => {
  const { useSyncExternalStore } = await import("react");
  const subscribe = (listener: () => void) => {
    toolsState.listeners.add(listener);
    return () => void toolsState.listeners.delete(listener);
  };
  return {
    hasDesktopDesignTools: () => toolsState.available,
    useHasDesktopDesignTools: () => useSyncExternalStore(subscribe, () => toolsState.available),
    DesktopDesignTools: ({ url }: { url: string }) => (
      <output aria-label="Tools' page">{url}</output>
    ),
  };
});

function setToolsAvailable(available: boolean) {
  toolsState.available = available;
  for (const listener of [...toolsState.listeners]) listener();
}

/** A shell with one loaded view; `overrides` replace its bridge methods. */
function installBridge(overrides: Record<string, unknown> = {}) {
  const unsubscribe = () => {};
  const bridge = {
    browserHasView: vi.fn().mockResolvedValue({ exists: true, url: "https://a.example/" }),
    onBrowserViewCreated: () => unsubscribe,
    onBrowserHostActiveChanged: () => unsubscribe,
    onBrowserViewClosed: () => unsubscribe,
    onBrowserUrlChanged: () => unsubscribe,
    onBrowserNavState: () => unsubscribe,
    browserSetActive: vi.fn().mockResolvedValue({ ok: true }),
    browserResize: vi.fn().mockResolvedValue({ ok: true }),
    browserEnableDesignMode: vi.fn().mockResolvedValue({ ok: true }),
    browserDisableDesignMode: vi.fn().mockResolvedValue({ ok: true }),
    ...overrides,
  };
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = bridge;
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  return bridge;
}

afterEach(() => {
  toolsState.available = true;
  cleanup();
  vi.unstubAllGlobals();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("BrowserPane's design tools", () => {
  it("name the page the view loaded, not what's typed in the address bar", async () => {
    let urlChanged: (payload: { conversationId: string; url: string }) => void = () => {};
    let viewClosed: (payload: { conversationId: string; reason: string | null }) => void = () => {};
    installBridge({
      onBrowserViewClosed: (callback: typeof viewClosed) => {
        viewClosed = callback;
        return () => {};
      },
      onBrowserUrlChanged: (callback: typeof urlChanged) => {
        urlChanged = callback;
        return () => {};
      },
    });
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
    // The view closed: a new one names no page until it has loaded one.
    act(() => viewClosed({ conversationId: "conv_url", reason: null }));
    expect(tools).toBeEmptyDOMElement();
  });

  it("name a page the view moved to before its first look at the view answered", async () => {
    let urlChanged: (payload: { conversationId: string; url: string }) => void = () => {};
    let navState: (payload: {
      conversationId: string;
      canGoBack: boolean;
      canGoForward: boolean;
    }) => void = () => {};
    let answer: (value: { exists: boolean; url: string; canGoBack: boolean }) => void = () => {};
    installBridge({
      onBrowserNavState: (callback: typeof navState) => {
        navState = callback;
        return () => {};
      },
      browserHasView: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
      onBrowserUrlChanged: (callback: typeof urlChanged) => {
        urlChanged = callback;
        return () => {};
      },
    });
    render(<BrowserPane conversationId="conv_race" />);
    const page = await screen.findByLabelText("Tools' page");
    act(() => {
      urlChanged({ conversationId: "conv_race", url: "https://new.example/" });
      navState({ conversationId: "conv_race", canGoBack: true, canGoForward: false });
    });
    await act(async () => answer({ exists: true, url: "https://old.example/", canGoBack: false }));
    expect(page).toHaveTextContent("https://new.example/");
    expect(screen.getByRole("button", { name: "Go back" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "Address bar" })).toHaveValue(
      "https://new.example/",
    );
  });

  it("turn design mode's picker off when they load after it was turned on in their place", async () => {
    toolsState.available = false;
    const bridge = installBridge();
    render(<BrowserPane conversationId="conv_back" />);
    const enter = await screen.findByRole("button", { name: "Enter design mode" });
    await waitFor(() => expect(enter).toBeEnabled());
    fireEvent.click(enter);
    expect(bridge.browserEnableDesignMode).toHaveBeenCalledWith("conv_back");
    act(() => setToolsAvailable(true));
    expect(bridge.browserDisableDesignMode).toHaveBeenCalledWith("conv_back");
    expect(screen.queryByRole("button", { name: /design mode/i })).toBeNull();
  });
});
