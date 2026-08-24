import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KeyboardShortcutsDialog, openKeyboardShortcuts } from "./KeyboardShortcutsDialog";

// The pinned-session row shows in both shells; only its chord differs (Alt in
// the browser). Default the mock to browser (false); flip per-test for native.
const isNativeShell = vi.fn(() => false);
vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: () => isNativeShell(),
  // DialogContent (rendered here) reads isIOSShell to size modals for the iOS
  // keyboard; this suite exercises the browser path, so it's always false.
  isIOSShell: () => false,
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
    // General / In chats / Navigation / View / Slash commands — one each.
    expect(screen.getByText("Start a new session")).toBeTruthy();
    expect(screen.getByText("Open command palette")).toBeTruthy();
    expect(screen.getByText("Show keyboard shortcuts")).toBeTruthy();
    expect(screen.getByText("Send message")).toBeTruthy();
    expect(screen.getByText("Recall previous prompt")).toBeTruthy();
    expect(screen.getByText("Previous session")).toBeTruthy();
    expect(screen.getByText("Toggle conversations sidebar")).toBeTruthy();
    expect(screen.getByText("Navigate suggestions")).toBeTruthy();
  });

  it("lists both archive routes: the ⌘⌥A hotkey and the menu-scoped 'A'", () => {
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();

    // Two rows share the label — the chat-scoped chord and the menu key.
    const rows = screen.getAllByText("Archive session").map((el) => el.closest("li"));
    expect(rows).toHaveLength(2);
    // jsdom's navigator is non-mac → "Ctrl" / "Alt".
    const chord = rows.find((row) => within(row!).queryByText("Ctrl") !== null);
    expect(chord).toBeTruthy();
    expect(within(chord!).getByText("Alt")).toBeTruthy();
    expect(within(chord!).getByText("A")).toBeTruthy();
    // The menu row is the bare letter, with no modifier chips.
    const menuRow = rows.find((row) => row !== chord);
    expect(within(menuRow!).getByText("A")).toBeTruthy();
    expect(within(menuRow!).queryByText("Ctrl")).toBeNull();
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

  it("shows the pinned-session shortcut with the Alt chord in a plain browser", () => {
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();
    const row = screen.getByText("Jump to pinned session (1–10)").closest("li");
    expect(row).toBeTruthy();
    // Browser chord adds Alt (jsdom navigator is non-mac → "Alt") + the 1…0 chip.
    expect(within(row!).getByText("Alt")).toBeTruthy();
    expect(within(row!).getByText("1…0")).toBeTruthy();
  });

  it("shows the pinned-session shortcut without Alt in the Electron shell", () => {
    isNativeShell.mockReturnValue(true);
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();
    const row = screen.getByText("Jump to pinned session (1–10)").closest("li");
    expect(row).toBeTruthy();
    expect(within(row!).queryByText("Alt")).toBeNull();
    expect(within(row!).getByText("1…0")).toBeTruthy();
  });
});
