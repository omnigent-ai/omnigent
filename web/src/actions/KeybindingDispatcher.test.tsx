import { StrictMode, useState } from "react";
import { createPortal } from "react-dom";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EmbeddedProvider } from "@/lib/embedded";
import { ActionScope, ActionsProvider, useSuspendKeybindingDispatch } from "./ActionProvider";
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
    | "composer.action.acceptSuggestion"
    | "composer.action.commitDictation"
    | "composer.action.dismissSuggestions"
    | "file.action.closeSearch"
    | "panel.action.closeFiles"
    | "panel.action.closeTerminals"
    | "session.action.new"
    | "workbench.action.toggleConversationsSidebar"
    | "workbench.action.toggleWorkspaceSidebar";
  run: () => "handled" | "notHandled";
}) {
  useRegisterAction(action, { run, acceptsKeybindings: true });
  return null;
}

function SuspensionToggle() {
  const [suspended, setSuspended] = useState(false);
  useSuspendKeybindingDispatch(suspended);
  return (
    <button type="button" onClick={() => setSuspended((value) => !value)}>
      {suspended ? "Resume keys" : "Suspend keys"}
    </button>
  );
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

  it("keeps new-session disabled but command palette active in embedded mode", () => {
    const palette = vi.fn(() => HANDLED);
    const newSession = vi.fn(() => HANDLED);
    render(
      <EmbeddedProvider>
        <ActionsProvider>
          <KeybindingDispatcher />
          <Handler action="session.action.new" run={newSession} />
          <Handler action="workbench.action.showCommands" run={palette} />
        </ActionsProvider>
      </EmbeddedProvider>,
    );
    const newEvent = new KeyboardEvent("keydown", {
      key: "n",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    window.dispatchEvent(newEvent);
    expect(newSession).not.toHaveBeenCalled();
    expect(newEvent.defaultPrevented).toBe(false);
    window.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "k",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(palette).toHaveBeenCalledOnce();
  });

  it("ignores AltGraph for centralized sidebar bindings", () => {
    const run = vi.fn(() => HANDLED);
    renderActions(<Handler action="workbench.action.toggleConversationsSidebar" run={run} />);
    const event = new KeyboardEvent("keydown", {
      key: "[",
      code: "BracketLeft",
      ctrlKey: true,
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    event.getModifierState = (key) => key === "AltGraph";
    window.dispatchEvent(event);
    expect(run).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it("suspends keyboard dispatch without disabling action handlers", () => {
    const run = vi.fn(() => HANDLED);
    renderActions(
      <>
        <SuspensionToggle />
        <Handler action="workbench.action.showCommands" run={run} />
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Suspend keys" }));
    expect(fireEvent.keyDown(window, { key: "k", ctrlKey: true })).toBe(true);
    expect(run).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Resume keys" }));
    expect(fireEvent.keyDown(window, { key: "k", ctrlKey: true })).toBe(false);
    expect(run).toHaveBeenCalledOnce();
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

  it("leaves both command-palette modifier variants to Monaco", () => {
    const run = vi.fn(() => HANDLED);
    renderActions(
      <>
        <Handler action="workbench.action.showCommands" run={run} />
        <div className="monaco-editor">
          <textarea aria-label="code editor" />
        </div>
      </>,
    );
    const editor = screen.getByRole("textbox", { name: "code editor" });
    expect(fireEvent.keyDown(editor, { key: "k", ctrlKey: true })).toBe(true);
    expect(fireEvent.keyDown(editor, { key: "k", metaKey: true })).toBe(true);
    expect(run).not.toHaveBeenCalled();
  });

  it("matches both physical sidebar brackets and rejects missing Alt", () => {
    const left = vi.fn(() => HANDLED);
    const right = vi.fn(() => HANDLED);
    renderActions(
      <>
        <Handler action="workbench.action.toggleConversationsSidebar" run={left} />
        <Handler action="workbench.action.toggleWorkspaceSidebar" run={right} />
      </>,
    );
    window.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "‘",
        code: "BracketRight",
        ctrlKey: true,
        altKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(right).toHaveBeenCalledOnce();
    expect(left).not.toHaveBeenCalled();
    window.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "[",
        code: "BracketLeft",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(left).not.toHaveBeenCalled();
  });

  it("lets opted-in legacy globals run after a target prevents default", () => {
    const left = vi.fn(() => HANDLED);
    renderActions(
      <>
        <Handler action="workbench.action.toggleConversationsSidebar" run={left} />
        <textarea aria-label="owned widget" onKeyDown={(event) => event.preventDefault()} />
      </>,
    );
    fireEvent.keyDown(screen.getByRole("textbox", { name: "owned widget" }), {
      key: "[",
      code: "BracketLeft",
      ctrlKey: true,
      altKey: true,
    });
    expect(left).toHaveBeenCalledOnce();
  });

  it("prioritizes dictation capture and suggestions over composer send", () => {
    const commit = vi.fn(() => HANDLED);
    const accept = vi.fn(() => HANDLED);
    const send = vi.fn(() => HANDLED);
    const first = renderActions(
      <>
        <Handler action="composer.action.commitDictation" run={commit} />
        <ActionScope mode="composer">
          <div>
            <Handler action="composer.action.send" run={send} />
            <textarea aria-label="dictating composer" />
          </div>
        </ActionScope>
      </>,
    );
    fireEvent.keyDown(screen.getByRole("textbox", { name: "dictating composer" }), {
      key: "Enter",
    });
    expect(commit).toHaveBeenCalledOnce();
    expect(send).not.toHaveBeenCalled();
    first.unmount();

    renderActions(
      <ActionScope mode="composer" context={{ composerSuggestionsOpen: true }}>
        <div>
          <Handler action="composer.action.acceptSuggestion" run={accept} />
          <Handler action="composer.action.send" run={send} />
          <textarea aria-label="suggesting composer" />
        </div>
      </ActionScope>,
    );
    fireEvent.keyDown(screen.getByRole("textbox", { name: "suggesting composer" }), {
      key: "Enter",
    });
    expect(accept).toHaveBeenCalledOnce();
    expect(send).not.toHaveBeenCalled();
  });

  it("prefers a focused composer rule over an active file rule", () => {
    const dismiss = vi.fn(() => HANDLED);
    const closeSearch = vi.fn(() => HANDLED);
    renderActions(
      <>
        <ActionScope mode="fileViewer" context={{ fileSearchOpen: true }}>
          <div>
            <Handler action="file.action.closeSearch" run={closeSearch} />
          </div>
        </ActionScope>
        <ActionScope mode="composer" context={{ composerSuggestionsOpen: true }}>
          <div>
            <Handler action="composer.action.dismissSuggestions" run={dismiss} />
            <textarea aria-label="focused composer" />
          </div>
        </ActionScope>
      </>,
    );
    fireEvent.keyDown(screen.getByRole("textbox", { name: "focused composer" }), {
      key: "Escape",
    });
    expect(dismiss).toHaveBeenCalledOnce();
    expect(closeSearch).not.toHaveBeenCalled();
  });

  it("closes only the focused active panel when multiple panels are open", () => {
    const closeFiles = vi.fn(() => HANDLED);
    const closeTerminals = vi.fn(() => HANDLED);
    const rules = [
      {
        id: "panel.terminals",
        action: "panel.action.closeTerminals",
        sequence: parseKeybinding("escape"),
        mode: "terminalsPanel",
        activation: "active" as const,
      },
      {
        id: "panel.files",
        action: "panel.action.closeFiles",
        sequence: parseKeybinding("escape"),
        mode: "filesPanel",
        activation: "active" as const,
      },
    ] satisfies KeybindingRule[];
    renderActions(
      <>
        <ActionScope mode="filesPanel">
          <div>
            <Handler action="panel.action.closeFiles" run={closeFiles} />
            <button type="button">Files</button>
          </div>
        </ActionScope>
        <ActionScope mode="terminalsPanel">
          <div>
            <Handler action="panel.action.closeTerminals" run={closeTerminals} />
            <button type="button">Terminals</button>
          </div>
        </ActionScope>
      </>,
      rules,
    );
    const terminals = screen.getByRole("button", { name: "Terminals" });
    terminals.focus();
    fireEvent.keyDown(terminals, { key: "Escape" });
    expect(closeTerminals).toHaveBeenCalledOnce();
    expect(closeFiles).not.toHaveBeenCalled();
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
});
