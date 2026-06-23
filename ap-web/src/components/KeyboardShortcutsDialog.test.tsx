import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KeyboardShortcutsDialog, openKeyboardShortcuts } from "./KeyboardShortcutsDialog";

// The pinned-session row is desktop-only. Default to browser (false).
const isNativeShell = vi.fn(() => false);
vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: () => isNativeShell(),
}));

beforeEach(() => {
  isNativeShell.mockReturnValue(false);
});
afterEach(cleanup);

// jsdom's navigator is non-mac, so the modifier glyph renders as "Ctrl".
function toggleViaHotkey() {
  fireEvent.keyDown(window, { key: "/", ctrlKey: true });
}

describe("KeyboardShortcutsDialog", () => {
  it("renders nothing until opened", () => {
    render(<KeyboardShortcutsDialog />);
    expect(screen.queryByText("Send message")).toBeNull();
  });

  it("opens on the modifier+/ hotkey and lists one shortcut from each group", () => {
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();

    expect(screen.getByText("Keyboard shortcuts")).toBeTruthy();
    // General / In chats / Navigation / Slash commands — one representative each.
    expect(screen.getByText("Show keyboard shortcuts")).toBeTruthy();
    expect(screen.getByText("Send message")).toBeTruthy();
    expect(screen.getByText("Recall previous prompt")).toBeTruthy();
    expect(screen.getByText("Search sessions")).toBeTruthy();
    expect(screen.getByText("Previous session")).toBeTruthy();
    expect(screen.getByText("Navigate suggestions")).toBeTruthy();
  });

  it("toggles closed on a second hotkey press", async () => {
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();
    expect(screen.getByText("Send message")).toBeTruthy();

    toggleViaHotkey();
    await waitFor(() => expect(screen.queryByText("Send message")).toBeNull());
  });

  it("opens when openKeyboardShortcuts() is dispatched (menu entry path)", async () => {
    render(<KeyboardShortcutsDialog />);
    openKeyboardShortcuts();
    // The event dispatch isn't wrapped in act(), so wait for the re-render.
    expect(await screen.findByText("Send message")).toBeTruthy();
  });

  it("hides the pinned-session shortcut in a plain browser", () => {
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();
    expect(screen.queryByText("Jump to pinned session (1–10)")).toBeNull();
  });

  it("shows the pinned-session shortcut in the Electron shell", () => {
    isNativeShell.mockReturnValue(true);
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();
    expect(screen.getByText("Jump to pinned session (1–10)")).toBeTruthy();
  });
});
