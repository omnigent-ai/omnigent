// The Browser pane's toolbar with a desktop shell that has the design tools.
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type * as NativeBridge from "@/lib/nativeBridge";
import type * as SessionsApi from "@/lib/sessionsApi";
import { BrowserPane } from "./BrowserPane";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApi>()),
  getSessionSlim: async (id: string) => ({ id, hostId: null, parentSessionId: null }),
}));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isElectronShell: () => true,
  supportsBrowser: () => true,
  isIOSShell: () => false,
  getDesktopFeatures: async () => null,
}));

function installBridge(overrides: Record<string, unknown> = {}) {
  const unsubscribe = () => {};
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    browserHasView: vi.fn().mockResolvedValue({ exists: false }),
    onBrowserViewCreated: () => unsubscribe,
    onBrowserHostActiveChanged: () => unsubscribe,
    onBrowserViewClosed: () => unsubscribe,
    onBrowserUrlChanged: () => unsubscribe,
    onBrowserNavState: () => unsubscribe,
    browserSetActive: vi.fn().mockResolvedValue({ ok: true }),
    browserResize: vi.fn().mockResolvedValue({ ok: true }),
    browserEnableDesignMode: vi.fn().mockResolvedValue({ ok: true }),
    browserDisableDesignMode: vi.fn().mockResolvedValue({ ok: true }),
    browserDesignTools: vi.fn().mockResolvedValue({ ok: true }),
    onBrowserDesignToolsEvent: vi.fn().mockReturnValue(unsubscribe),
    ...overrides,
  };
}

// The tools load lazily: load their module once here, so a busy test run's import time
// isn't what these tests wait on.
beforeAll(async () => {
  await import("./designTools/DesktopDesignTools");
}, 30_000);

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("BrowserPane design tools", () => {
  it("offers Annotate and Draw instead of design mode where the shell has them", async () => {
    installBridge();
    render(<BrowserPane conversationId="conv_dt1" />);
    const tools = await screen.findByRole("group", { name: "Design tools" });
    expect(
      within(tools)
        .getAllByRole("button")
        .map((button) => button.getAttribute("aria-label")),
    ).toEqual(["Annotate an element", "Draw on the page"]);
    expect(screen.queryByRole("button", { name: /design mode/i })).toBeNull();
  });

  it("keeps design mode in a shell without the tools", async () => {
    installBridge({ browserDesignTools: undefined, onBrowserDesignToolsEvent: undefined });
    render(<BrowserPane conversationId="conv_dt2" />);
    await screen.findByRole("textbox", { name: /address bar/i });
    expect(screen.getByRole("button", { name: /enter design mode/i })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Design tools" })).toBeNull();
  });

  it("closes an open design tool when the retained pane is hidden", async () => {
    const browserDesignTools = vi.fn(async ({ op }: { op: string }) =>
      op === "start"
        ? { ok: true, session: "open_1", frame: "omnigent-design-tools:open_1" }
        : { ok: true },
    );
    installBridge({
      browserHasView: vi.fn().mockResolvedValue({ exists: true, url: "http://localhost:5173" }),
      browserDesignTools,
    });
    // The pane has room, and the tool's overlay opens (a document of its own).
    const box = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue({ width: 800, height: 600, x: 0, y: 0, top: 0, left: 0 } as DOMRect);
    const frame = document.createElement("iframe");
    document.body.append(frame);
    const open = vi.spyOn(window, "open").mockReturnValue(frame.contentWindow);
    try {
      const { rerender } = render(<BrowserPane conversationId="conv_dt3" active />);
      const annotate = await screen.findByRole("button", { name: "Annotate an element" });
      await waitFor(() => expect(annotate).toBeEnabled());
      act(() => annotate.click());
      await waitFor(() => expect(annotate).toHaveAttribute("aria-pressed", "true"));
      await waitFor(() =>
        expect(open).toHaveBeenCalledWith("about:blank", "omnigent-design-tools:open_1"),
      );
      expect(browserDesignTools).not.toHaveBeenCalledWith(expect.objectContaining({ op: "stop" }));
      rerender(<BrowserPane conversationId="conv_dt3" active={false} />);
      await waitFor(() =>
        expect(browserDesignTools).toHaveBeenCalledWith(
          expect.objectContaining({ op: "stop", session: "open_1" }),
        ),
      );
      rerender(<BrowserPane conversationId="conv_dt3" active />);
      expect(screen.getByRole("button", { name: "Annotate an element" })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    } finally {
      open.mockRestore();
      box.mockRestore();
      frame.remove();
    }
  });
});
