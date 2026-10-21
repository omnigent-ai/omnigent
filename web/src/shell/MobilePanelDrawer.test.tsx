import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MobilePanelDrawer } from "./MobilePanelDrawer";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** Stub the iOS shell's keyboard API; returns a setter that plays a keyboard-frame change. */
function stubIOSKeyboard(): (keyboardHeight: number) => void {
  let keyboardHeight = 0;
  const listeners = new Set<() => void>();
  vi.stubGlobal("innerWidth", 390);
  vi.stubGlobal("innerHeight", 844);
  vi.stubGlobal("visualViewport", Object.assign(new EventTarget(), { height: 844, offsetTop: 0 }));
  vi.stubGlobal("omnigentNative", {
    kind: "ios",
    getKeyboardViewport: () => ({ width: 390, height: 844 - keyboardHeight }),
    onKeyboardViewportChanged: (callback: () => void) => {
      listeners.add(callback);
      return () => listeners.delete(callback);
    },
  });
  return (height) =>
    act(() => {
      keyboardHeight = height;
      for (const listener of listeners) listener();
    });
}

function renderDrawer(props: Partial<Parameters<typeof MobilePanelDrawer>[0]> = {}) {
  return render(
    <MobilePanelDrawer
      open
      title="Shells"
      onClose={() => {}}
      testId="shells-panel-drawer"
      {...props}
    >
      <div data-testid="drawer-body" />
    </MobilePanelDrawer>,
  );
}

describe("MobilePanelDrawer", () => {
  it("carries the safe-area hook class so the header clears the notch", () => {
    // The drawer is a `fixed inset-0` overlay on phones. Without this class
    // the native shells' inset rule (index.css) misses it and the title +
    // Close button render under the status bar / dynamic island — leaving no
    // way to dismiss the panel.
    renderDrawer();
    expect(screen.getByTestId("shells-panel-drawer")).toHaveClass("mobile-panel-drawer");
  });

  it("keeps the safe-area class while closed", () => {
    // The class must not be state-dependent: the drawer animates in from the
    // right, so the inset has to be in place before it is on screen.
    renderDrawer({ open: false });
    expect(screen.getByTestId("shells-panel-drawer")).toHaveClass("mobile-panel-drawer");
  });

  it("only casts a shadow while visible", () => {
    const { rerender } = renderDrawer({ open: false });
    const drawer = screen.getByTestId("shells-panel-drawer");
    expect(drawer).toHaveClass("shadow-none");
    expect(drawer).not.toHaveClass("shadow-lg");

    rerender(
      <MobilePanelDrawer open title="Shells" onClose={() => {}} testId="shells-panel-drawer">
        <div />
      </MobilePanelDrawer>,
    );
    expect(drawer).toHaveClass("shadow-lg");
    expect(drawer).not.toHaveClass("shadow-none");
  });

  it("renders the title and a working Close button", () => {
    const onClose = vi.fn();
    renderDrawer({ onClose });

    expect(screen.getByRole("heading", { name: "Shells" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("mounts children only while open", () => {
    const { rerender } = renderDrawer({ open: false });
    expect(screen.queryByTestId("drawer-body")).toBeNull();

    rerender(
      <MobilePanelDrawer open title="Shells" onClose={() => {}} testId="shells-panel-drawer">
        <div data-testid="drawer-body" />
      </MobilePanelDrawer>,
    );
    expect(screen.getByTestId("drawer-body")).toBeInTheDocument();
  });

  it("reserves the docked iOS keyboard so bottom content such as a composer stays visible", () => {
    const setKeyboardHeight = stubIOSKeyboard();
    renderDrawer();
    const drawer = screen.getByTestId("shells-panel-drawer");
    expect(drawer.style.paddingBottom).toBe("");

    setKeyboardHeight(336);
    expect(drawer.style.paddingBottom).toBe("336px");

    setKeyboardHeight(0);
    expect(drawer.style.paddingBottom).toBe("");
  });

  it("does not reserve the keyboard while closed", () => {
    // A kept-mounted drawer (side chats) sits off-screen while another
    // surface holds the keyboard.
    const setKeyboardHeight = stubIOSKeyboard();
    const { rerender } = renderDrawer({ keepMounted: true });
    const drawer = screen.getByTestId("shells-panel-drawer");
    setKeyboardHeight(336);
    expect(drawer.style.paddingBottom).toBe("336px");

    rerender(
      <MobilePanelDrawer
        open={false}
        title="Shells"
        onClose={() => {}}
        testId="shells-panel-drawer"
        keepMounted
      >
        <div data-testid="drawer-body" />
      </MobilePanelDrawer>,
    );
    expect(drawer.style.paddingBottom).toBe("");
    setKeyboardHeight(300);
    expect(drawer.style.paddingBottom).toBe("");
  });
});
