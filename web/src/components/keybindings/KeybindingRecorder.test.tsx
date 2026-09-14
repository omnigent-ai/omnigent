import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActionsProvider, HANDLED, KeybindingDispatcher, useRegisterAction } from "@/actions";
import { KeybindingRecorder } from "./KeybindingRecorder";

function PaletteHandler({ run }: { run: () => typeof HANDLED }) {
  useRegisterAction("workbench.action.showCommands", { acceptsKeybindings: true, run });
  return null;
}

function renderRecorder({
  onComplete = vi.fn(),
  onCancel = vi.fn(),
  run = vi.fn(() => HANDLED),
  preferPhysical = false,
} = {}) {
  render(
    <ActionsProvider>
      <KeybindingDispatcher />
      <PaletteHandler run={run} />
      <KeybindingRecorder
        onComplete={onComplete}
        onCancel={onCancel}
        preferPhysical={preferPhysical}
      />
    </ActionsProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Record binding" }));
  return {
    recorder: screen.getByRole("application", { name: "Keybinding recorder" }),
    onComplete,
    onCancel,
    run,
  };
}

afterEach(cleanup);

describe("KeybindingRecorder", () => {
  it("records one key combination immediately while normal dispatch is suspended", () => {
    const { recorder, onComplete, run } = renderRecorder();
    expect(fireEvent.keyDown(recorder, { key: "k", code: "KeyK", ctrlKey: true })).toBe(false);
    expect(onComplete).toHaveBeenCalledWith("mod+k");
    expect(run).not.toHaveBeenCalled();
    expect(screen.queryByTestId("keybinding-recorder")).toBeNull();
  });

  it("cancels on Escape", () => {
    const { recorder, onComplete, onCancel } = renderRecorder();
    expect(fireEvent.keyDown(recorder, { key: "Escape" })).toBe(false);
    expect(onCancel).toHaveBeenCalledOnce();
    expect(onComplete).not.toHaveBeenCalled();
  });

  it.each(["Backspace", "Delete"])("clears pending input with bare %s", (key) => {
    const { recorder, onComplete } = renderRecorder();
    expect(fireEvent.keyDown(recorder, { key })).toBe(false);
    expect(screen.getByTestId("keybinding-recorder")).toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("allows modified Backspace to be recorded", () => {
    const { recorder, onComplete } = renderRecorder();
    fireEvent.keyDown(recorder, { key: "Backspace", code: "Backspace", ctrlKey: true });
    expect(onComplete).toHaveBeenCalledWith("mod+Backspace");
  });

  it("ignores composition, AltGraph, modifier-only, and repeat events", () => {
    const { recorder, onComplete } = renderRecorder();
    const composing = new KeyboardEvent("keydown", {
      key: "Process",
      bubbles: true,
      cancelable: true,
      isComposing: true,
    });
    recorder.dispatchEvent(composing);
    const altGraph = new KeyboardEvent("keydown", {
      key: "@",
      ctrlKey: true,
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    altGraph.getModifierState = (key) => key === "AltGraph";
    recorder.dispatchEvent(altGraph);
    fireEvent.keyDown(recorder, { key: "Control", ctrlKey: true });
    fireEvent.keyDown(recorder, { key: "k", code: "KeyK", ctrlKey: true, repeat: true });
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("records bracket and Alt-modified keys by physical code", () => {
    const bracket = renderRecorder({ preferPhysical: true });
    fireEvent.keyDown(bracket.recorder, {
      key: "[",
      code: "BracketLeft",
      ctrlKey: true,
      altKey: true,
    });
    expect(bracket.onComplete).toHaveBeenCalledWith("mod+alt+[BracketLeft]");

    cleanup();
    const altKey = renderRecorder();
    fireEvent.keyDown(altKey.recorder, { key: "v", code: "KeyV", altKey: true });
    expect(altKey.onComplete).toHaveBeenCalledWith("alt+[KeyV]");
  });

  it("keeps recording and explains unsupported keys", () => {
    const { recorder, onComplete } = renderRecorder();
    fireEvent.keyDown(recorder, { key: "[", code: "", ctrlKey: true });
    expect(screen.getByRole("alert")).toHaveTextContent("cannot be recorded");
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("cancels when focus leaves and exposes screen-reader instructions", () => {
    const { recorder, onCancel } = renderRecorder();
    expect(screen.getByText(/Press one key combination\./, { selector: ".sr-only" })).toHaveClass(
      "sr-only",
    );
    fireEvent.blur(recorder, { relatedTarget: null });
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
