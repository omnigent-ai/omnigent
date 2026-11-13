import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversation as conv, conversationPage } from "@/test/sidebarMockHelpers";
// Mobile drawer: a strip of chat stays visible and dismisses it on tap, Search
// floats at the top, and Settings sits in the profile footer.

import { act, cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type * as NativeBridgeModule from "@/lib/nativeBridge";

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return conversationHooksMock();
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

const { isAndroidShellMock } = vi.hoisted(() => ({ isAndroidShellMock: vi.fn(() => false) }));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  isAndroidShell: () => isAndroidShellMock(),
}));
vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

vi.mock("@/lib/serverOrigin", () => ({
  isCurrentServerLocal: () => false,
  isLocalServerOrigin: (origin: string) =>
    ["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"].includes(new URL(origin).hostname),
}));

import { useConversations } from "@/hooks/useConversations";
import { stubMatchMedia } from "@/test-helpers/matchMedia";
import { ROW_GESTURE_HOLD_MS } from "@/hooks/useRowGesture";
import { DEFAULT_SWIPE_ACTIONS, writeSwipeActions } from "@/lib/swipeActionPreferences";

const useConvMock = vi.mocked(useConversations);
const originalMatchMedia = window.matchMedia;

function mockConversations(conversations: Conversation[]) {
  useConvMock.mockImplementation(() => conversationPage(conversations));
}

beforeEach(() => {
  stubMatchMedia({ width: 375 });
  mockConversations([conv("conv_a")]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  isAndroidShellMock.mockReturnValue(false);
  writeSwipeActions(DEFAULT_SWIPE_ACTIONS);
  window.matchMedia = originalMatchMedia;
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
    // Invisible too: on the macOS desktop shell even a transparent scrim is a
    // no-drag region that keeps the title bar from dragging the window.
    expect(scrim).toHaveClass("pointer-events-none", "invisible", "opacity-0");
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

  it("keeps Settings in the profile footer and hides the desktop collapse toggle on mobile", async () => {
    renderSidebar();

    // Search stays in the header row (top); Settings sits in the footer below
    // the session list, so the list needs no gutter for a floating button.
    const headerActions = screen.getByTestId("sidebar-header-actions");
    expect(within(headerActions).getByTestId("sidebar-search-button")).toBeInTheDocument();
    expect(within(headerActions).queryByLabelText("Settings")).toBeNull();

    const footer = await screen.findByTestId("sidebar-server-picker-row");
    expect(within(footer).getByTestId("sidebar-profile-settings")).toHaveAttribute(
      "href",
      "/settings",
    );
    expect(screen.getByRole("navigation")).not.toHaveClass("max-md:pb-16");

    expect(within(headerActions).getByRole("button", { name: "Close sidebar" })).toHaveClass(
      "max-md:hidden",
    );
  });

  it("keeps Search and Settings icon-only on mobile", async () => {
    renderSidebar();

    const search = screen.getByTestId("sidebar-search-button");
    const settings = await screen.findByTestId("sidebar-profile-settings");

    for (const button of [search, settings]) {
      expect(button).toHaveClass("max-md:size-11", "max-md:text-foreground");
      expect(button.className).toMatch(/(?:^| )(?:max-md:)?rounded-full(?: |$)/);
      expect(button).not.toHaveClass("sidebar-glass-chip");
    }
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

/** Dispatch a touch event with `fingers` touches down on `target` at `time` ms. */
function touch(target: Element, type: string, x: number, time: number, fingers = 1): void {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, "touches", {
    value:
      type === "touchend"
        ? []
        : Array.from({ length: fingers }, () => ({ clientX: x, clientY: 300 })),
  });
  Object.defineProperty(event, "timeStamp", { value: time });
  act(() => {
    target.dispatchEvent(event);
  });
}

/** One finger step on a row: the pointer event its gesture reads, then the touch event. */
function fingerOnRow(target: Element, type: "down" | "move" | "up", x: number, time: number): void {
  const pointer = { pointerId: 1, pointerType: "touch", isPrimary: true, button: 0, clientX: x };
  const fire = {
    down: fireEvent.pointerDown,
    move: fireEvent.pointerMove,
    up: fireEvent.pointerUp,
  };
  fire[type](target, { ...pointer, clientY: 300 });
  touch(target, { down: "touchstart", move: "touchmove", up: "touchend" }[type], x, time);
}

function stubMobileLayout(mobile: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    // `isMobileViewport` reads the md min-width query, the inverse of mobile; the
    // side-by-side query (min and max width) never matches.
    matches:
      query.includes("min-width") && !query.includes("max-width")
        ? !mobile
        : mobile && query.includes("max-width") && !query.includes("min-width"),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

// A leftward swipe on the open drawer slides it shut under the finger, the
// reverse of the iOS shell's left-edge swipe that opens it.
describe("mobile sidebar drawer swipe to close", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function renderDrawer(props: { route?: string; dragProgress?: number } = {}) {
    const onClose = vi.fn();
    const { setOpen } = renderSidebar({ onClose, ...props });
    const drawer = screen.getByRole("complementary", { name: "Conversations" });
    drawer.getBoundingClientRect = () => ({ width: 400 }) as DOMRect;
    return { onClose, drawer, setOpen };
  }

  it("follows the finger and closes once released past a third of its width", () => {
    stubMobileLayout(true);
    const { onClose, drawer } = renderDrawer();

    touch(drawer, "touchstart", 300, 0);
    touch(drawer, "touchmove", 250, 60);
    touch(drawer, "touchmove", 100, 120);

    expect(drawer.style.transform).toBe("translateX(-50%)");
    expect(screen.getByTestId("sidebar-scrim").style.opacity).toBe("0.5");
    expect(onClose).not.toHaveBeenCalled();

    touch(drawer, "touchend", 0, 400);

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(drawer.style.transform).toBe("");
  });

  it("springs back open without closing after a short drag", () => {
    stubMobileLayout(true);
    const { onClose, drawer } = renderDrawer();

    touch(drawer, "touchstart", 300, 0);
    touch(drawer, "touchmove", 270, 80);
    touch(drawer, "touchmove", 250, 160);
    touch(drawer, "touchend", 0, 400);

    expect(onClose).not.toHaveBeenCalled();
    expect(drawer.style.transform).toBe("");
  });

  it("drops the drag when a second finger lands, so the drawer can still close", () => {
    stubMobileLayout(true);
    const { onClose, drawer, setOpen } = renderDrawer();

    touch(drawer, "touchstart", 300, 0);
    touch(drawer, "touchmove", 270, 80);
    touch(drawer, "touchmove", 250, 160);
    expect(drawer.style.transform).toBe("translateX(-12.5%)");

    touch(drawer, "touchstart", 250, 200, 2);
    touch(drawer, "touchend", 0, 260);

    expect(drawer.style.transform).toBe("");
    expect(onClose).not.toHaveBeenCalled();

    setOpen(false);

    expect(drawer).toHaveAttribute("data-collapsed", "true");
    expect(drawer.style.transform).toBe("");
    expect(screen.getByTestId("sidebar-scrim").style.opacity).toBe("");
  });

  it("clears a drag cut off by the drawer closing, so reopening shows no stale offset", () => {
    stubMobileLayout(true);
    const { onClose, drawer, setOpen } = renderDrawer();

    touch(drawer, "touchstart", 300, 0);
    touch(drawer, "touchmove", 250, 80);
    touch(drawer, "touchmove", 200, 160);
    expect(drawer.style.transform).toBe("translateX(-25%)");

    setOpen(false);
    expect(drawer).toHaveAttribute("data-collapsed", "true");
    expect(drawer.style.transform).toBe("");

    touch(drawer, "touchmove", 100, 200);
    touch(drawer, "touchend", 0, 240);
    setOpen(true);

    expect(drawer.style.transform).toBe("");
    expect(screen.getByTestId("sidebar-scrim").style.opacity).toBe("");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("leaves a row long-press to drag-to-reorder", async () => {
    stubMobileLayout(true);
    // Without a swipe-left action the row doesn't claim the swipe up front, so
    // the hold alone hands the touch to the row's drag.
    writeSwipeActions({ left: "none", right: "archive" });
    vi.useFakeTimers();
    const { onClose, drawer } = renderDrawer();
    const link = screen.getByRole("link", { name: "conv_a" });
    const dragPreview = () => document.body.querySelector('[class*="max-w-[16rem]"]');

    try {
      fingerOnRow(link, "down", 300, 0);
      await act(() => vi.advanceTimersByTimeAsync(ROW_GESTURE_HOLD_MS));
      fingerOnRow(link, "move", 250, ROW_GESTURE_HOLD_MS);
      expect(dragPreview()).not.toBeNull();

      fingerOnRow(link, "move", 100, ROW_GESTURE_HOLD_MS + 60);
      expect(drawer.style.transform).toBe("");
      fingerOnRow(link, "up", 100, ROW_GESTURE_HOLD_MS + 100);

      expect(drawer.style.transform).toBe("");
      expect(onClose).not.toHaveBeenCalled();
    } finally {
      // dnd-kit defers removing its document listeners by 50 ms.
      await act(() => vi.advanceTimersByTimeAsync(50));
      vi.useRealTimers();
    }
  });

  it("leaves a leftward swipe on a row to the row's swipe-left action", () => {
    stubMobileLayout(true);
    const { onClose, drawer } = renderDrawer();
    const link = screen.getByRole("link", { name: "conv_a" });

    fingerOnRow(link, "down", 300, 0);
    fingerOnRow(link, "move", 250, 60);
    fingerOnRow(link, "move", 200, 120);

    expect(screen.getByTestId("conversation-swipe-reveal")).toBeInTheDocument();
    expect(drawer.style.transform).toBe("");
    fingerOnRow(link, "up", 200, 160);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes from a row whose swipe-left action is off", () => {
    stubMobileLayout(true);
    writeSwipeActions({ left: "none", right: "archive" });
    const { onClose, drawer } = renderDrawer();
    const link = screen.getByRole("link", { name: "conv_a" });

    fingerOnRow(link, "down", 300, 0);
    fingerOnRow(link, "move", 250, 60);
    fingerOnRow(link, "move", 100, 120);
    expect(drawer.style.transform).toBe("translateX(-50%)");
    expect(screen.queryByTestId("conversation-swipe-reveal")).toBeNull();
    fingerOnRow(link, "up", 0, 400);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  interface SwipeContext {
    mobile: boolean;
    android?: boolean;
    route?: string;
    dragProgress?: number;
  }
  it.each<[string, SwipeContext, string]>([
    ["the desktop layout", { mobile: false }, ""],
    ["Settings", { mobile: true, route: "/settings" }, ""],
    // The native edge swipe owns the drawer while its progress is live.
    ["a native edge swipe", { mobile: true, dragProgress: 0.5 }, "translateX(-50%)"],
    // Android closes the drawer with the system back gesture.
    ["the Android shell", { mobile: true, android: true }, ""],
  ])("ignores the swipe during %s", (_label, { mobile, android = false, ...props }, transform) => {
    stubMobileLayout(mobile);
    isAndroidShellMock.mockReturnValue(android);
    const { onClose, drawer } = renderDrawer(props);

    touch(drawer, "touchstart", 300, 0);
    touch(drawer, "touchmove", 250, 80);
    touch(drawer, "touchmove", 100, 160);
    expect(drawer.style.transform).toBe(transform);
    touch(drawer, "touchend", 0, 400);

    expect(drawer.style.transform).toBe(transform);
    expect(onClose).not.toHaveBeenCalled();
  });
});

// Opening the drawer by swipe leaves focus where it was; a still-focused chat
// composer would keep the iOS soft keyboard up over the open drawer.
describe("mobile sidebar drawer keyboard dismissal", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.body.querySelectorAll("textarea[data-test-composer]").forEach((el) => el.remove());
  });

  function focusComposer(): HTMLTextAreaElement {
    const composer = document.createElement("textarea");
    composer.dataset.testComposer = "";
    document.body.appendChild(composer);
    composer.focus();
    return composer;
  }

  it("blurs a focused composer outside the drawer when the drawer opens", () => {
    stubMobileLayout(true);
    const { setOpen } = renderSidebar({ open: false });
    const composer = focusComposer();
    expect(composer).toHaveFocus();

    setOpen(true);

    expect(composer).not.toHaveFocus();
  });

  it("keeps the composer focused when the window narrows with the sidebar open", () => {
    stubMobileLayout(false);
    const { setOpen } = renderSidebar({ open: true });
    const composer = focusComposer();

    stubMobileLayout(true);
    setOpen(true);

    expect(composer).toHaveFocus();
  });

  it("keeps the composer focused when the drawer opens on the desktop layout", () => {
    stubMobileLayout(false);
    const { setOpen } = renderSidebar({ open: false });
    const composer = focusComposer();

    setOpen(true);

    expect(composer).toHaveFocus();
  });
});
