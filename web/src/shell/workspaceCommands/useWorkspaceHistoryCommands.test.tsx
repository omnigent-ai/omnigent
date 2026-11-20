import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ShortcutRegistry from "@/lib/shortcuts/registry";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import type { WorkspaceHistoryFacts } from "./model";
import { useWorkspaceHistoryCommands } from "./useWorkspaceHistoryCommands";

const platform = vi.hoisted(() => ({ native: false }));
vi.mock("@/lib/shortcuts/registry", async (importOriginal) => {
  const registry = await importOriginal<typeof ShortcutRegistry>();
  return {
    ...registry,
    currentShortcutEnvironment: (overrides = {}) =>
      registry.currentShortcutEnvironment({ native: platform.native, ...overrides }),
  };
});

const BACK_ONLY: WorkspaceHistoryFacts = {
  back: { target: "Files", waitingForPage: false },
  forward: { target: null, waitingForPage: false },
};

function bind(isMac: boolean, conversationId: string | null = "conv_1") {
  const go = vi.fn();
  renderHook(() => useWorkspaceHistoryCommands({ conversationId, facts: BACK_ONLY, go }, isMac));
  return go;
}

function press(init: KeyboardEventInit, target: EventTarget = window): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
}

const browserBack = { key: "ArrowLeft", ctrlKey: true, altKey: true, shiftKey: true };
const browserForward = { ...browserBack, key: "ArrowRight" };

beforeEach(() => {
  platform.native = false;
});
afterEach(() => {
  cleanup();
  document.body.replaceChildren();
});

describe("useWorkspaceHistoryCommands", () => {
  it("offers Back and Forward in ⌘K, labelled with where they land", () => {
    const go = bind(false);
    const rows = registeredPaletteCommands().filter((row) =>
      row.id.startsWith("workspace:conv_1:"),
    );
    expect(rows.map((row) => [row.id, row.availability?.available])).toEqual([
      ["workspace:conv_1:history:back", true],
      ["workspace:conv_1:history:forward", false],
    ]);
    expect(rows.every((row) => row.movesFocus)).toBe(true);
    rows[0]!.run();
    expect(go).toHaveBeenCalledWith("back");
  });

  it("steps on its chord, even from a text field, and claims an unavailable step's chord", () => {
    const go = bind(false);
    const field = document.body.appendChild(document.createElement("textarea"));
    field.focus();
    expect(press(browserBack, field).defaultPrevented).toBe(true);
    expect(press(browserForward, field).defaultPrevented).toBe(true);
    expect(go.mock.calls).toEqual([["back"]]);
  });

  it("leaves ⌘← to a focused text field, editor or terminal in the macOS app, and steps elsewhere", () => {
    platform.native = true;
    const go = bind(true);
    const commandBack = { key: "ArrowLeft", metaKey: true };
    const field = document.body.appendChild(document.createElement("input"));
    field.focus();
    expect(press(commandBack, field).defaultPrevented).toBe(false);
    const editor = document.body.appendChild(document.createElement("div"));
    editor.className = "cm-editor";
    const content = editor.appendChild(document.createElement("div"));
    content.tabIndex = 0;
    content.focus();
    expect(press(commandBack, content).defaultPrevented).toBe(false);
    // A shell takes keys in xterm's helper textarea, or on its own screen once clicked.
    const terminal = document.body.appendChild(document.createElement("div"));
    terminal.className = "xterm";
    const helper = terminal.appendChild(document.createElement("textarea"));
    helper.className = "xterm-helper-textarea";
    helper.focus();
    expect(press(commandBack, helper).defaultPrevented).toBe(false);
    const screenElement = terminal.appendChild(document.createElement("div"));
    screenElement.className = "xterm-screen";
    screenElement.tabIndex = 0;
    screenElement.focus();
    expect(press(commandBack, screenElement).defaultPrevented).toBe(false);
    expect(go).not.toHaveBeenCalled();

    screenElement.blur();
    expect(press(commandBack).defaultPrevented).toBe(true);
    expect(go).toHaveBeenCalledWith("back");
  });

  it("offers and binds nothing without a session", () => {
    const go = bind(false, null);
    expect(registeredPaletteCommands().some((row) => row.id.includes("history"))).toBe(false);
    expect(press(browserBack).defaultPrevented).toBe(false);
    expect(go).not.toHaveBeenCalled();
  });
});
