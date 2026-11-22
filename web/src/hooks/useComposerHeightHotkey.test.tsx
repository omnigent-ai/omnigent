import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

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

afterEach(() => {
  cleanup();
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
    const { rerender } = mount("session:a");
    mount("session:b");
    expect(rows()).toHaveLength(1);
    expect(heightGroups().map((group) => group.heading)).toEqual(["Message"]);
    expect(row()?.label).toBe("Expand message box");

    rerender({ scope: "session:a", enabled: false });
    // The other box still can.
    expect(rows()).toHaveLength(1);
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
    mount("session:a");
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
