import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePaletteCommandsStore } from "@/store/paletteCommands";

import {
  useWorkspaceFullScreenCommand,
  type WorkspaceFullScreenOptions,
} from "./useWorkspaceFullScreenCommand";

const rows = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);

function pressFullScreenChord(
  init: KeyboardEventInit = {},
  target: EventTarget = window,
): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    key: "F",
    code: "KeyF",
    metaKey: true,
    shiftKey: true,
    bubbles: true,
    cancelable: true,
    ...init,
  });
  act(() => {
    target.dispatchEvent(event);
  });
  return event;
}

/** Mounts the hook; once full screen is available, waits for its row and chord to load. */
async function mount(options: Partial<WorkspaceFullScreenOptions> = {}) {
  const toggle = vi.fn();
  const initialProps = { conversationId: "conv", fullScreen: false, toggle, ...options };
  const view = renderHook(
    (props: WorkspaceFullScreenOptions) => useWorkspaceFullScreenCommand(props, true),
    { initialProps },
  );
  if (initialProps.conversationId !== null && initialProps.fullScreen !== null) {
    await waitFor(() => expect(rows()).toHaveLength(1));
  }
  return { toggle, view };
}

function focusField(handlesChord: boolean): HTMLInputElement {
  const field = document.createElement("input");
  if (handlesChord)
    field.addEventListener("keydown", (event) => {
      if (event.metaKey && event.shiftKey && event.code === "KeyF") event.preventDefault();
    });
  document.body.append(field);
  field.focus();
  return field;
}

afterEach(() => {
  usePaletteCommandsStore.setState({ groups: {} });
  document.body.replaceChildren();
});

describe("useWorkspaceFullScreenCommand", () => {
  it("offers one row named for what it does now, showing ⌘⇧F", async () => {
    const { toggle, view } = await mount();
    expect(rows().map((row) => [row.label, row.shortcut])).toEqual([
      ["Make the workspace full screen", "workspace.toggleFullScreen"],
    ]);
    act(() => rows()[0]!.run());
    expect(toggle).toHaveBeenCalledOnce();

    view.rerender({ conversationId: "conv", fullScreen: true, toggle });
    expect(rows().map((row) => row.label)).toEqual(["Exit workspace full screen"]);
  });

  it("toggles from the chord once per press, ignoring a held key", async () => {
    const { toggle } = await mount();
    expect(pressFullScreenChord().defaultPrevented).toBe(true);
    pressFullScreenChord({ repeat: true });
    expect(toggle).toHaveBeenCalledOnce();
  });

  it("offers nothing and leaves the chord alone where full screen isn't available", async () => {
    const { toggle, view } = await mount({ fullScreen: null });
    expect(rows()).toEqual([]);
    expect(pressFullScreenChord().defaultPrevented).toBe(false);

    view.rerender({ conversationId: null, fullScreen: false, toggle });
    expect(rows()).toEqual([]);
    expect(pressFullScreenChord().defaultPrevented).toBe(false);
    expect(toggle).not.toHaveBeenCalled();
  });

  it("leaves the old text-search chord and ⌘F alone", async () => {
    const { toggle } = await mount();
    expect(pressFullScreenChord({ shiftKey: false }).defaultPrevented).toBe(false);
    expect(pressFullScreenChord({ altKey: true, key: "ƒ" }).defaultPrevented).toBe(false);
    expect(toggle).not.toHaveBeenCalled();
  });

  it("leaves the chord to a focused control that handles it", async () => {
    const { toggle } = await mount();
    pressFullScreenChord({}, focusField(true));
    expect(toggle).not.toHaveBeenCalled();
  });

  it("still toggles from a focused field that doesn't handle the chord", async () => {
    const { toggle } = await mount();
    expect(pressFullScreenChord({}, focusField(false)).defaultPrevented).toBe(true);
    expect(toggle).toHaveBeenCalledOnce();
  });

  it("ignores the chord while an IME is composing", async () => {
    const { toggle } = await mount();
    const field = focusField(false);
    expect(pressFullScreenChord({ isComposing: true }, field).defaultPrevented).toBe(false);
    expect(pressFullScreenChord({ keyCode: 229 }, field).defaultPrevented).toBe(false);
    expect(toggle).not.toHaveBeenCalled();
  });
});
