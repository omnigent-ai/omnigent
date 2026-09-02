import { StrictMode, useState } from "react";
import { createPortal } from "react-dom";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActionScope, ActionsProvider } from "./ActionProvider";
import { KeybindingDispatcher } from "./KeybindingDispatcher";
import { when } from "./context";
import { parseKeybinding } from "./keybindingParser";
import type { KeybindingRule } from "./types";
import { HANDLED, NOT_HANDLED } from "./types";
import { useRegisterAction } from "./useRegisterAction";

function Handler({
  action,
  run,
}: {
  action:
    | "workbench.action.showCommands"
    | "chat.action.acceptApproval"
    | "composer.action.send"
    | "panel.action.closeFiles";
  run: () => "handled" | "notHandled";
}) {
  useRegisterAction(action, { run, acceptsKeybindings: true });
  return null;
}

function renderActions(children: React.ReactNode, rules?: readonly KeybindingRule[]) {
  return render(
    <ActionsProvider>
      <KeybindingDispatcher rules={rules} />
      {children}
    </ActionsProvider>,
  );
}

afterEach(() => vi.useRealTimers());

describe("KeybindingDispatcher", () => {
  it("is inert when no action handler is registered", () => {
    renderActions(null);
    const event = new KeyboardEvent("keydown", {
      key: "k",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
  });

  it("dispatches a capture binding and consumes it only when handled", () => {
    const run = vi.fn(() => HANDLED);
    renderActions(<Handler action="workbench.action.showCommands" run={run} />);
    const event = new KeyboardEvent("keydown", {
      key: "k",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    window.dispatchEvent(event);
    expect(run).toHaveBeenCalledOnce();
    expect(event.defaultPrevented).toBe(true);
  });

  it("preserves terminal Ctrl+K ownership while allowing Meta+K", () => {
    const run = vi.fn(() => HANDLED);
    renderActions(
      <>
        <Handler action="workbench.action.showCommands" run={run} />
        <div className="xterm">
          <textarea aria-label="terminal" />
        </div>
      </>,
    );
    const terminal = screen.getByRole("textbox", { name: "terminal" });
    fireEvent.keyDown(terminal, { key: "k", ctrlKey: true });
    expect(run).not.toHaveBeenCalled();
    fireEvent.keyDown(terminal, { key: "k", metaKey: true });
    expect(run).toHaveBeenCalledOnce();
  });

  it("falls through a notHandled capture action to a composer bubble action", () => {
    const approve = vi.fn(() => NOT_HANDLED);
    const send = vi.fn(() => HANDLED);
    renderActions(
      <>
        <Handler action="chat.action.acceptApproval" run={approve} />
        <ActionScope mode="composer">
          <div>
            <Handler action="composer.action.send" run={send} />
            <textarea aria-label="composer" />
          </div>
        </ActionScope>
      </>,
    );
    const composer = screen.getByRole("textbox", { name: "composer" });
    fireEvent.keyDown(composer, { key: "Enter", ctrlKey: true });
    expect(approve).toHaveBeenCalledOnce();
    expect(send).toHaveBeenCalledOnce();
  });

  it("yields bubble bindings to focused widgets that prevented the event", () => {
    const send = vi.fn(() => HANDLED);
    renderActions(
      <ActionScope mode="composer">
        <div>
          <Handler action="composer.action.send" run={send} />
          <textarea aria-label="composer" onKeyDown={(event) => event.preventDefault()} />
        </div>
      </ActionScope>,
    );
    fireEvent.keyDown(screen.getByRole("textbox", { name: "composer" }), { key: "Enter" });
    expect(send).not.toHaveBeenCalled();
  });

  it("ignores composition, AltGraph, and disallowed repeat events", () => {
    const run = vi.fn(() => HANDLED);
    renderActions(<Handler action="workbench.action.showCommands" run={run} />);
    window.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "k",
        ctrlKey: true,
        isComposing: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    const altGraph = new KeyboardEvent("keydown", {
      key: "k",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    altGraph.getModifierState = (key) => key === "AltGraph";
    window.dispatchEvent(altGraph);
    window.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "k",
        ctrlKey: true,
        repeat: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(run).not.toHaveBeenCalled();
  });

  it("dispatches scoped actions through React portals and parent scope ancestry", () => {
    const send = vi.fn(() => HANDLED);
    const portalRoot = document.createElement("div");
    document.body.append(portalRoot);
    const view = renderActions(
      <ActionScope mode="fileViewer">
        <div>
          {createPortal(
            <ActionScope mode="composer">
              <div>
                <Handler action="composer.action.send" run={send} />
                <textarea aria-label="portaled composer" />
              </div>
            </ActionScope>,
            portalRoot,
          )}
        </div>
      </ActionScope>,
    );
    fireEvent.keyDown(screen.getByRole("textbox", { name: "portaled composer" }), { key: "Enter" });
    expect(send).toHaveBeenCalledOnce();
    view.unmount();
    portalRoot.remove();
  });

  it("does not reuse stale scope focus after the focused element unmounts", () => {
    const send = vi.fn(() => HANDLED);
    let hideComposer = () => {};
    function Probe() {
      const [shown, setShown] = useState(true);
      hideComposer = () => setShown(false);
      return (
        <ActionScope mode="composer">
          <div>
            <Handler action="composer.action.send" run={send} />
            {shown ? <textarea aria-label="temporary composer" /> : null}
          </div>
        </ActionScope>
      );
    }
    renderActions(<Probe />);
    screen.getByRole("textbox", { name: "temporary composer" }).focus();
    act(() => hideComposer());
    window.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
    );
    expect(send).not.toHaveBeenCalled();
  });

  it("dispatches state-active panel rules outside the focused branch", () => {
    const close = vi.fn(() => HANDLED);
    const rules: readonly KeybindingRule[] = [
      {
        id: "test.active-panel",
        action: "panel.action.closeFiles",
        sequence: parseKeybinding("escape"),
        mode: "filesPanel",
        activation: "active",
        when: when("fileSearchOpen"),
      },
    ];
    renderActions(
      <>
        <ActionScope mode="filesPanel" context={{ fileSearchOpen: true }}>
          <div>
            <Handler action="panel.action.closeFiles" run={close} />
          </div>
        </ActionScope>
        <textarea aria-label="outside" />
      </>,
      rules,
    );
    fireEvent.keyDown(screen.getByRole("textbox", { name: "outside" }), { key: "Escape" });
    expect(close).toHaveBeenCalledOnce();
  });

  it("does not leak handlers or listeners through Strict Mode or unmount", () => {
    const run = vi.fn(() => HANDLED);
    const view = render(
      <StrictMode>
        <ActionsProvider>
          <KeybindingDispatcher />
          <Handler action="workbench.action.showCommands" run={run} />
        </ActionsProvider>
      </StrictMode>,
    );
    const key = () =>
      window.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "k",
          ctrlKey: true,
          bubbles: true,
          cancelable: true,
        }),
      );
    key();
    expect(run).toHaveBeenCalledOnce();
    view.unmount();
    key();
    expect(run).toHaveBeenCalledOnce();
  });

  it("honors explicit event consumption policy", () => {
    const run = vi.fn(() => HANDLED);
    const downstream = vi.fn();
    const rules: readonly KeybindingRule[] = [
      {
        id: "test.consumption",
        action: "workbench.action.showCommands",
        sequence: parseKeybinding("primary+x"),
        mode: "global",
        phase: "capture",
        preventDefault: false,
        stopPropagation: true,
      },
    ];
    renderActions(
      <button type="button" aria-label="target">
        Target
        <Handler action="workbench.action.showCommands" run={run} />
      </button>,
      rules,
    );
    document.addEventListener("keydown", downstream, { once: true, capture: true });
    const target = screen.getByRole("button", { name: "target" });
    const event = new KeyboardEvent("keydown", {
      key: "x",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    target.dispatchEvent(event);
    expect(run).toHaveBeenCalledOnce();
    expect(event.defaultPrevented).toBe(false);
    expect(downstream).not.toHaveBeenCalled();
  });

  it("supports two-stroke chords and clears the pending prefix", () => {
    const run = vi.fn(() => HANDLED);
    const rules: readonly KeybindingRule[] = [
      {
        id: "test.chord",
        action: "workbench.action.showCommands",
        sequence: parseKeybinding("primary+k primary+s"),
        mode: "global",
      },
    ];
    renderActions(<Handler action="workbench.action.showCommands" run={run} />, rules);
    const first = new KeyboardEvent("keydown", {
      key: "k",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    window.dispatchEvent(first);
    expect(first.defaultPrevented).toBe(true);
    expect(run).not.toHaveBeenCalled();
    window.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "s",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(run).toHaveBeenCalledOnce();
  });

  it("cancels a chord on an invalid continuation, Escape, or focus change", () => {
    const run = vi.fn(() => HANDLED);
    const rules: readonly KeybindingRule[] = [
      {
        id: "test.chord.cancel",
        action: "workbench.action.showCommands",
        sequence: parseKeybinding("primary+k primary+s"),
        mode: "global",
      },
    ];
    renderActions(<Handler action="workbench.action.showCommands" run={run} />, rules);
    const key = (value: string, ctrlKey = false) =>
      window.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: value,
          ctrlKey,
          bubbles: true,
          cancelable: true,
        }),
      );

    key("k", true);
    key("x");
    key("s", true);
    key("k", true);
    key("Escape");
    key("s", true);
    key("k", true);
    window.dispatchEvent(new FocusEvent("focusin"));
    key("s", true);
    expect(run).not.toHaveBeenCalled();
  });

  it("expires a pending chord", () => {
    vi.useFakeTimers();
    const run = vi.fn(() => HANDLED);
    const rules: readonly KeybindingRule[] = [
      {
        id: "test.chord.timeout",
        action: "workbench.action.showCommands",
        sequence: parseKeybinding("primary+k primary+s"),
        mode: "global",
      },
    ];
    renderActions(<Handler action="workbench.action.showCommands" run={run} />, rules);
    window.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "k",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    vi.advanceTimersByTime(1_501);
    window.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "s",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(run).not.toHaveBeenCalled();
  });
});
