import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActionsProvider } from "./ActionProvider";
import { KeybindingDispatcher } from "./KeybindingDispatcher";
import { HANDLED, type ArglessActionId } from "./types";
import { useRegisterAction } from "./useRegisterAction";

function Handler({ action, run }: { action: ArglessActionId; run: () => typeof HANDLED }) {
  useRegisterAction(action, { run, acceptsKeybindings: true });
  return null;
}

afterEach(() => vi.restoreAllMocks());

const commands = [
  { action: "workbench.action.showCommands", key: "k", code: "KeyK", altKey: false },
  { action: "workbench.action.showSessionSearch", key: "ß", code: "KeyS", altKey: true },
  { action: "session.action.openPrevious", key: "[", code: "BracketLeft", altKey: false },
  { action: "session.action.openNext", key: "]", code: "BracketRight", altKey: false },
] as const;

describe.each([false, true])("platform shortcut parity (Mac=%s)", (isMac) => {
  it.each(commands)("preserves modifier and focus ownership for $action", (command) => {
    vi.spyOn(navigator, "platform", "get").mockReturnValue(isMac ? "MacIntel" : "Linux x86_64");
    const run = vi.fn(() => HANDLED);
    render(
      <ActionsProvider>
        <KeybindingDispatcher />
        <Handler action={command.action} run={run} />
        <input aria-label="composer" />
        <input aria-label="palette" cmdk-input="" />
        <div className="monaco-editor">
          <textarea aria-label="editor" />
        </div>
        <div className="xterm">
          <textarea aria-label="terminal" />
        </div>
      </ActionsProvider>,
    );
    const event = { ...command, metaKey: isMac, ctrlKey: !isMac };
    const composer = screen.getByRole("textbox", { name: "composer" });
    expect(fireEvent.keyDown(composer, { ...event, metaKey: !isMac, ctrlKey: isMac })).toBe(true);
    expect(fireEvent.keyDown(composer, { ...event, metaKey: true, ctrlKey: true })).toBe(true);
    expect(fireEvent.keyDown(composer, { ...event, repeat: true })).toBe(true);
    expect(fireEvent.keyDown(composer, { ...event, shiftKey: true })).toBe(true);
    expect(run).not.toHaveBeenCalled();
    expect(fireEvent.keyDown(composer, event)).toBe(false);
    expect(run).toHaveBeenCalledOnce();
    run.mockClear();

    const navigation = command.action.startsWith("session.");
    expect(fireEvent.keyDown(screen.getByRole("textbox", { name: "editor" }), event)).toBe(true);
    expect(run).not.toHaveBeenCalled();
    const paletteHandled = !navigation;
    expect(fireEvent.keyDown(screen.getByRole("textbox", { name: "palette" }), event)).toBe(
      !paletteHandled,
    );
    expect(run).toHaveBeenCalledTimes(paletteHandled ? 1 : 0);
    run.mockClear();
    const terminalHandled = isMac && !navigation;
    expect(fireEvent.keyDown(screen.getByRole("textbox", { name: "terminal" }), event)).toBe(
      !terminalHandled,
    );
    expect(run).toHaveBeenCalledTimes(terminalHandled ? 1 : 0);
  });
});
