import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionHotkeyHint, SessionHotkeyHints } from "./SessionHotkeyHints";

const platform = vi.hoisted(() => ({ native: true, mac: true }));
vi.mock("@/lib/nativeBridge", () => ({ isNativeShell: () => platform.native }));
vi.mock("@/lib/hotkeys", () => ({
  isMacPlatform: () => platform.mac,
  hasCommandModifier: (event: KeyboardEvent) =>
    platform.mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey,
}));
beforeEach(() => {
  platform.native = true;
  platform.mac = true;
});
afterEach(cleanup);

function hints(ids = ["a", "b"]) {
  return (
    <SessionHotkeyHints ids={ids}>
      <SessionHotkeyHint id="a" />
      <SessionHotkeyHint id="b" />
    </SessionHotkeyHints>
  );
}

describe("session hotkey hints", () => {
  it("shows muted digits only while Command is held, and updates their order", () => {
    const { rerender } = render(hints());
    expect(screen.queryByText("1")).toBeNull();
    fireEvent.keyDown(window, { key: "Meta", metaKey: true });
    expect(screen.getByLabelText("Switch to session: ⌘1")).toBeTruthy();
    rerender(hints(["b"]));
    expect(screen.queryByText("2")).toBeNull();
    fireEvent.keyUp(window, { key: "Meta" });
    expect(screen.queryByText("1")).toBeNull();
  });

  it("shows the browser Alt modifier and clears stuck hints on blur", () => {
    platform.native = false;
    render(hints());
    fireEvent.keyDown(window, { metaKey: true });
    expect(screen.getByText("⌥1")).toBeTruthy();
    fireEvent.blur(window);
    expect(screen.queryByText("⌥1")).toBeNull();
  });

  it("uses Control on Windows/Linux and does not label slots beyond ten", () => {
    platform.mac = false;
    render(hints(["0", "1", "2", "3", "4", "5", "6", "7", "8", "a", "b"]));
    fireEvent.keyDown(window, { metaKey: true });
    expect(screen.queryByText("0")).toBeNull();
    fireEvent.keyDown(window, { ctrlKey: true });
    expect(screen.getByLabelText("Switch to session: Ctrl+0")).toBeTruthy();
    expect(document.querySelectorAll("kbd")).toHaveLength(1);
  });
});
