import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { createPortal } from "react-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MobilePanelDrawer, useMobilePanelHeaderTarget } from "./MobilePanelDrawer";

const drawerError = new Error("Drawer component failed");

function suppressDrawerError(event: ErrorEvent) {
  if (event.error === drawerError) event.preventDefault();
}

afterEach(() => {
  cleanup();
  window.removeEventListener("error", suppressDrawerError);
  vi.restoreAllMocks();
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
  const { children = <div data-testid="drawer-body" />, ...drawerProps } = props;
  return render(
    <MobilePanelDrawer
      open
      title="Shells"
      onClose={() => {}}
      testId="shells-panel-drawer"
      {...drawerProps}
    >
      {children}
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

  it("preserves a kept-mounted child's draft and DOM when closed and reopened", () => {
    function Draft() {
      const [text, setText] = useState("");
      return (
        <textarea
          aria-label="Side chat draft"
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
      );
    }
    const { rerender } = renderDrawer({ keepMounted: true, children: <Draft /> });
    const draft = screen.getByRole("textbox", { name: "Side chat draft" });
    fireEvent.change(draft, { target: { value: "Unsent side chat" } });

    rerender(
      <MobilePanelDrawer open={false} title="Shells" onClose={() => {}} keepMounted>
        <Draft />
      </MobilePanelDrawer>,
    );
    expect(draft).toBeInTheDocument();
    expect(draft).toHaveValue("Unsent side chat");

    rerender(
      <MobilePanelDrawer open title="Shells" onClose={() => {}} keepMounted>
        <Draft />
      </MobilePanelDrawer>,
    );
    expect(screen.getByRole("textbox", { name: "Side chat draft" })).toBe(draft);
    expect(draft).toHaveValue("Unsent side chat");
  });

  describe("render failures", () => {
    beforeEach(() => {
      window.addEventListener("error", suppressDrawerError);
      vi.spyOn(console, "error").mockImplementation(() => {});
    });

    it.each([false, true])(
      "keeps the title and Close usable and recovers when the drawer reopens (keepMounted=%s)",
      (keepMounted) => {
        let broken = true;
        function Panel() {
          if (broken) throw drawerError;
          return <p>Drawer recovered</p>;
        }
        function Drawer() {
          const [open, setOpen] = useState(true);
          return (
            <>
              <button type="button" onClick={() => setOpen(true)}>
                Open drawer
              </button>
              <MobilePanelDrawer
                open={open}
                title="Shells"
                onClose={() => setOpen(false)}
                testId="drawer"
                keepMounted={keepMounted}
              >
                <Panel />
              </MobilePanelDrawer>
            </>
          );
        }
        render(<Drawer />);
        expect(screen.getByRole("alert")).toHaveTextContent("Unable to display Shells.");
        expect(screen.getByRole("heading", { name: "Shells" })).toBeInTheDocument();

        fireEvent.click(screen.getByRole("button", { name: "Close" }));
        expect(screen.getByTestId("drawer")).toHaveAttribute("data-state", "closed");
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();

        broken = false;
        fireEvent.click(screen.getByRole("button", { name: "Open drawer" }));
        expect(screen.getByText("Drawer recovered")).toBeInTheDocument();
      },
    );

    it("contains failures portaled into the header and restores them on retry", () => {
      let broken = true;
      function HeaderAction() {
        if (broken) throw drawerError;
        return <button type="button">Panel action</button>;
      }
      function Panel() {
        const header = useMobilePanelHeaderTarget();
        return (
          <>
            {header?.actions && createPortal(<HeaderAction />, header.actions)}
            <p>Drawer content</p>
          </>
        );
      }
      const onClose = vi.fn();
      renderDrawer({ children: <Panel />, onClose });
      expect(screen.getByRole("alert")).toHaveTextContent("Unable to display Shells.");
      expect(screen.getByRole("heading", { name: "Shells" })).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Close" }));
      expect(onClose).toHaveBeenCalledOnce();

      broken = false;
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      expect(screen.getByText("Drawer content")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Panel action" })).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
  });
});
