import { act, cleanup, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as HotkeysModule from "@/lib/hotkeys";
import type * as NativeBridgeModule from "@/lib/nativeBridge";

import {
  composerHeightOf,
  resetComposerHeightsForTests,
  setComposerHeight,
} from "@/lib/composerHeightState";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import {
  COMPOSER_HEIGHT_COMMAND_SOURCE,
  targetComposerScope,
  useComposerHeightHotkey,
} from "./useComposerHeightHotkey";

// ⇧⌘M is bound only in the macOS desktop app, so each test picks the shell it runs in.
const shell = vi.hoisted(() => ({ native: true, mac: true }));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  isNativeShell: () => shell.native,
}));
vi.mock("@/lib/hotkeys", async (importOriginal) => ({
  ...(await importOriginal<typeof HotkeysModule>()),
  isMacPlatform: () => shell.mac,
}));

afterEach(() => {
  cleanup();
  shell.native = true;
  shell.mac = true;
  resetComposerHeightsForTests();
  usePaletteCommandsStore.setState({ groups: {} });
  document.body.replaceChildren();
});

/** A composer for `scope`, with a focusable input inside its root. */
function mount(scope = "session:a", enabled = true) {
  const root = document.createElement("div");
  const input = document.createElement("textarea");
  root.append(input);
  document.body.append(root);
  // One ref for the composer's life, as a component's useRef gives.
  const rootRef = { current: root };
  const view = renderHook(
    (props: { scope: string; enabled: boolean }) =>
      useComposerHeightHotkey({ root: rootRef, ...props }),
    { initialProps: { scope, enabled } },
  );
  return { ...view, input };
}

const heightGroups = () =>
  Object.entries(usePaletteCommandsStore.getState().groups)
    .filter(([source]) => source.startsWith(`${COMPOSER_HEIGHT_COMMAND_SOURCE}:`))
    .map(([, group]) => group);
const rows = () => heightGroups().flatMap((group) => group.commands);
const row = () => rows()[0];
const leaveForThePalette = () => {
  const palette = document.createElement("input");
  document.body.append(palette);
  act(() => palette.focus());
};

describe("useComposerHeightHotkey", () => {
  it("offers one ⌘K row named for what it does next, withdrawn where the box can't expand", () => {
    const first = mount("session:a");
    const second = mount("session:b");
    expect(rows()).toHaveLength(1);
    expect(heightGroups().map((group) => group.heading)).toEqual(["Message"]);
    expect(row()?.label).toBe("Expand message box");

    // The box the row acts on can't expand any more; the other box still can.
    second.rerender({ scope: "session:b", enabled: false });
    expect(rows()).toHaveLength(1);
    expect(targetComposerScope()).toBe("session:a");
    first.rerender({ scope: "session:a", enabled: false });
    expect(rows()).toHaveLength(0);
  });

  it("acts on the newest box until focus enters one, then on the one focused last", () => {
    const first = mount("session:a");
    mount("session:b");
    expect(targetComposerScope()).toBe("session:b");
    act(() => row()!.run());
    expect(composerHeightOf("session:b")).toBe("expanded");
    expect(row()?.label).toBe("Collapse message box");

    act(() => first.input.focus());
    leaveForThePalette();
    expect(targetComposerScope()).toBe("session:a");
    expect(row()?.label).toBe("Expand message box");
    act(() => row()!.run());
    expect(composerHeightOf("session:a")).toBe("expanded");
    expect(composerHeightOf("session:b")).toBe("expanded");
  });

  it("names the row for the box it acts on as focus moves between boxes", () => {
    act(() => setComposerHeight("session:a", "expanded"));
    const tall = mount("session:a");
    const normal = mount("session:b");
    act(() => tall.input.focus());
    expect(row()?.label).toBe("Collapse message box");
    act(() => normal.input.focus());
    expect(row()?.label).toBe("Expand message box");
    act(() => row()!.run());
    expect(composerHeightOf("session:b")).toBe("expanded");
    expect(composerHeightOf("session:a")).toBe("expanded");
  });

  it("keeps acting on the box focused last when another mounts", () => {
    const first = mount("session:a");
    act(() => first.input.focus());
    // A second box opens while focus stays in the first.
    mount("session:b");
    leaveForThePalette();
    expect(targetComposerScope()).toBe("session:a");
    act(() => row()!.run());
    expect(composerHeightOf("session:a")).toBe("expanded");
    expect(composerHeightOf("session:b")).toBe("compact");
  });

  it("keeps acting on the box focused last when it switches to another session", () => {
    const first = mount("session:a");
    const second = mount("session:b");
    act(() => second.input.focus());
    act(() => first.input.focus());
    // Focus leaves for the sidebar, where the first box's session is switched.
    act(() => first.input.blur());
    first.rerender({ scope: "session:c", enabled: true });

    expect(targetComposerScope()).toBe("session:c");
    act(() => row()!.run());
    expect(composerHeightOf("session:c")).toBe("expanded");
    expect(composerHeightOf("session:b")).toBe("compact");
  });

  it("acts on a box that mounts with focus already in it", () => {
    // Focus entered the first box, so the second wins only by holding focus as it mounts.
    const first = mount("session:a");
    act(() => first.input.focus());
    const root = document.createElement("div");
    const input = document.createElement("textarea");
    root.append(input);
    document.body.append(root);
    input.focus();
    const rootRef = { current: root };
    renderHook(() => useComposerHeightHotkey({ scope: "session:b", root: rootRef, enabled: true }));
    leaveForThePalette();
    expect(targetComposerScope()).toBe("session:b");
  });

  it("moves the row to the box left once the one it acted on unmounts, and drops it with none", () => {
    const first = mount("session:a");
    const second = mount("session:b");
    expect(targetComposerScope()).toBe("session:b");
    second.unmount();
    expect(targetComposerScope()).toBe("session:a");
    expect(rows()).toHaveLength(1);
    first.unmount();
    expect(targetComposerScope()).toBeNull();
    expect(rows()).toHaveLength(0);
  });
});

describe("the ⇧⌘M chord", () => {
  const pressChord = (target: Element = document.body, init: KeyboardEventInit = {}) =>
    fireEvent.keyDown(target, {
      key: "M",
      code: "KeyM",
      metaKey: true,
      shiftKey: true,
      ...init,
    });

  it("toggles the box ⌘K acts on, once however many boxes are up", () => {
    const first = mount("session:a");
    mount("session:b");
    act(() => first.input.focus());
    expect(pressChord(first.input)).toBe(false);
    expect(composerHeightOf("session:a")).toBe("expanded");
    expect(composerHeightOf("session:b")).toBe("compact");
    pressChord(first.input);
    expect(composerHeightOf("session:a")).toBe("compact");
  });

  it("leaves held repeats, terminals and the file editor alone", () => {
    mount("session:a");
    pressChord(document.body, { repeat: true });
    const terminal = document.createElement("div");
    terminal.className = "xterm";
    const terminalInput = document.createElement("textarea");
    terminal.append(terminalInput);
    document.body.append(terminal);
    act(() => terminalInput.focus());
    expect(pressChord(terminalInput)).toBe(true);
    expect(composerHeightOf("session:a")).toBe("compact");
  });

  it("isn't bound in a browser, or in the desktop app off macOS", () => {
    for (const [native, mac] of [
      [false, true],
      [true, false],
    ]) {
      shell.native = native;
      shell.mac = mac;
      mount("session:a");
      pressChord();
      pressChord(document.body, { metaKey: false, ctrlKey: true });
      expect(composerHeightOf("session:a")).toBe("compact");
      cleanup();
    }
  });

  it("listens once for every box up, and stops with the last one", () => {
    const listening = () => {
      const added = add.mock.calls.filter(([type]) => type === "keydown").length;
      return added - remove.mock.calls.filter(([type]) => type === "keydown").length;
    };
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    const first = mount("session:a");
    const second = mount("session:b");
    expect(listening()).toBe(1);
    first.unmount();
    expect(listening()).toBe(1);
    second.unmount();
    expect(listening()).toBe(0);
    vi.restoreAllMocks();
  });

  it("shows the chord on the ⌘K row", () => {
    mount("session:a");
    expect(row()?.shortcut).toBe("composer.toggleHeight");
  });
});
