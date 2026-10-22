import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversation as conv, conversationPage } from "@/test/sidebarMockHelpers";
// Behaviour tests for the mobile sidebar drawer shape: it stops short of the
// right edge so a strip of the chat stays visible, tapping that strip dismisses
// it (replacing the collapse toggle, which is now desktop-only), and Search /
// Settings float at the top and bottom of the drawer.

import { act, cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return conversationHooksMock();
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

vi.mock("@/lib/serverOrigin", () => ({
  isCurrentServerLocal: () => false,
  isLocalServerOrigin: (origin: string) =>
    ["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"].includes(new URL(origin).hostname),
}));

import { useConversations } from "@/hooks/useConversations";

const useConvMock = vi.mocked(useConversations);

function mockConversations(conversations: Conversation[]) {
  useConvMock.mockImplementation(() => conversationPage(conversations));
}

beforeEach(() => {
  mockConversations([conv("conv_a")]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("mobile sidebar drawer", () => {
  it("dismisses when the exposed strip of the chat is tapped", () => {
    const onClose = vi.fn();
    renderSidebar({ onClose });

    fireEvent.click(screen.getByTestId("sidebar-scrim"));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps the scrim inert while the drawer is closed", () => {
    renderSidebar({ open: false });

    const scrim = screen.getByTestId("sidebar-scrim");
    expect(scrim).toHaveClass("pointer-events-none", "opacity-0");
    // Untappable is not enough for a focusable control: parked, it must also
    // leave the tab order and the a11y tree. (Not `inert` — React 18 drops the
    // boolean form, so this would silently stay reachable.)
    expect(scrim).toHaveAttribute("tabindex", "-1");
    expect(scrim).toHaveAttribute("aria-hidden", "true");
  });

  it("exposes the dismiss as a labeled control, not a bare click target", () => {
    // With the collapse toggle gone on mobile, the scrim is the only
    // non-navigational way out — so it has to be reachable and announced,
    // not just tappable.
    renderSidebar();

    const scrim = screen.getByTestId("sidebar-scrim");
    expect(scrim.tagName).toBe("BUTTON");
    expect(scrim).toHaveAttribute("aria-label", "Close sidebar");
    expect(scrim).toHaveAttribute("tabindex", "0");
    expect(scrim).toHaveAttribute("aria-hidden", "false");
  });

  it("stacks the scrim above chat chrome but below the drawer", () => {
    // Chat chrome (ChatPage's jump-to-top pill) also sits at z-40 and renders
    // after the sidebar, so a same-z scrim would lose the tie inside the strip.
    renderSidebar();

    expect(screen.getByTestId("sidebar-scrim")).toHaveClass("z-[45]");
    expect(screen.getByRole("complementary", { name: "Conversations" })).toHaveClass("z-50");
  });

  it("stops the drawer short of the right edge so the chat stays reachable", () => {
    renderSidebar();

    expect(screen.getByRole("complementary", { name: "Conversations" })).toHaveClass(
      "max-md:right-14",
    );
  });

  it("drops the peek strip and the scrim on the settings page, where Back is the only exit", () => {
    renderSidebar({ route: "/settings" });

    expect(screen.queryByTestId("sidebar-scrim")).toBeNull();
    expect(screen.getByRole("complementary", { name: "Conversations" })).not.toHaveClass(
      "max-md:right-14",
    );
  });

  it("floats Settings at the bottom and hides the desktop collapse toggle on mobile", () => {
    renderSidebar();

    // Search stays in the header row (top); Settings gets its own float at the
    // bottom of the session list.
    const headerActions = screen.getByTestId("sidebar-header-actions");
    expect(within(headerActions).getByTestId("sidebar-search-button")).toBeInTheDocument();

    const float = screen.getByTestId("sidebar-settings-float");
    expect(float).toHaveAttribute("href", "/settings");
    expect(float.closest("a, button")).toHaveClass("absolute", "bottom-3", "md:hidden");

    // The header-row Settings copy and the collapse toggle are desktop-only.
    expect(screen.getByTestId("settings-button")).toHaveClass("max-md:hidden");
    expect(within(headerActions).getByRole("button", { name: "Close sidebar" })).toHaveClass(
      "max-md:hidden",
    );
  });

  it("keeps both floating actions icon-only on mobile", () => {
    renderSidebar();

    const search = screen.getByTestId("sidebar-search-button");
    const settings = screen.getByTestId("sidebar-settings-float").closest("a, button")!;

    for (const button of [search, settings]) {
      expect(button).toHaveClass("max-md:size-11", "max-md:text-foreground");
      expect(button).not.toHaveClass("sidebar-glass-chip", "max-md:rounded-full");
    }
  });

  it("gives the session list a gutter so the last row clears the floating action", () => {
    // The action doesn't scroll, so without this it would cover the last row,
    // hiding its title and state badge and blocking the tap target.
    renderSidebar();

    expect(screen.getByRole("navigation")).toHaveClass("max-md:pb-16");
  });
});

/**
 * Simulate the iOS native shell and its live visual viewport. The keyboard
 * "opens" by shrinking the visual viewport below the layout viewport
 * (window.innerHeight); useIOSNativeKeyboardInset reads the delta. Pass
 * visibleHeight === layoutHeight to model a closed keyboard (inset 0).
 */
function setIOSViewport(layoutHeight: number, visibleHeight: number): void {
  (window as unknown as Record<string, unknown>).omnigentNative = { kind: "ios" };
  vi.stubGlobal("innerHeight", layoutHeight);
  vi.stubGlobal("visualViewport", {
    offsetTop: 0,
    height: visibleHeight,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  });
}

// The mobile drawer is a `fixed inset-0` overlay that the iOS shell-lock (which
// only resizes flow content inside .app-shell) can't lift above the soft
// keyboard. It pads its own bottom by the keyboard inset so the session list
// stays fully scrollable while an inline rename holds the keyboard up —
// without it the last rows sit behind the keyboard and can never be reached.
describe("mobile sidebar drawer keyboard inset", () => {
  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).omnigentNative;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("pads the drawer bottom by the keyboard inset when the iOS keyboard is open", () => {
    setIOSViewport(844, 508); // keyboard covers 336px of the 844px layout
    renderSidebar();

    expect(screen.getByRole("complementary", { name: "Conversations" })).toHaveStyle({
      paddingBottom: "336px",
    });
  });

  it("applies no bottom padding when the keyboard is closed", () => {
    setIOSViewport(844, 844); // visible viewport fills the layout — no keyboard
    renderSidebar();

    expect(screen.getByRole("complementary", { name: "Conversations" }).style.paddingBottom).toBe(
      "",
    );
  });

  it("applies no bottom padding for a sub-threshold viewport delta", () => {
    // A small visual-viewport shrink (browser chrome shifting, not a
    // keyboard) sits below the hook's inset threshold and must not pad.
    setIOSViewport(844, 804); // 40px delta — below the 80px threshold
    renderSidebar();

    expect(screen.getByRole("complementary", { name: "Conversations" }).style.paddingBottom).toBe(
      "",
    );
  });

  it("leaves the list scroll alone when the keyboard opens for focus outside it", () => {
    // Focused rename rows are re-centered in Sidebar.rowActions.test.tsx.
    setIOSViewport(844, 844);
    renderSidebar();
    const composer = document.createElement("textarea");
    document.body.appendChild(composer);
    composer.focus();
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");

    act(() => {
      (window.visualViewport as unknown as { height: number }).height = 508;
      window.dispatchEvent(new Event("resize"));
    });

    expect(screen.getByRole("complementary", { name: "Conversations" })).toHaveStyle({
      paddingBottom: "336px",
    });
    expect(scrollIntoView).not.toHaveBeenCalled();
    composer.remove();
  });

  it("applies no bottom padding off the iOS shell even when the viewport shrinks", () => {
    // A shrunk visual viewport but no iOS shell marker: the browser/Electron
    // keyboard is handled by normal layout, so the drawer must not pad itself.
    vi.stubGlobal("innerHeight", 844);
    vi.stubGlobal("visualViewport", {
      offsetTop: 0,
      height: 508,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });
    renderSidebar();

    expect(screen.getByRole("complementary", { name: "Conversations" }).style.paddingBottom).toBe(
      "",
    );
  });
});
