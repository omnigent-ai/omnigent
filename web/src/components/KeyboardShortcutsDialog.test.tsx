import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ActionsProvider, KeybindingDispatcher } from "@/actions";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "@/lib/composerSendShortcutPreferences";
import {
  KeyboardShortcutsDialog,
  KeyboardShortcutsList,
  openKeyboardShortcuts,
} from "./KeyboardShortcutsDialog";

// The pinned-session row shows in both shells; only its chord differs (Alt in
// the browser). Default the mock to browser (false); flip per-test for native.
const isNativeShell = vi.fn(() => false);
vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: () => isNativeShell(),
  isElectronShell: () => false,
  // DialogContent (rendered here) reads isIOSShell to size modals for the iOS
  // keyboard; this suite exercises the browser path, so it's always false.
  isIOSShell: () => false,
}));

beforeEach(() => {
  isNativeShell.mockReturnValue(false);
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
});

function renderDialog() {
  return render(
    <ActionsProvider>
      <KeybindingDispatcher />
      <KeyboardShortcutsDialog />
    </ActionsProvider>,
  );
}

// jsdom's navigator is non-mac, so the modifier glyph renders as "Ctrl".
function toggleViaHotkey() {
  return fireEvent.keyDown(window, { key: "/", ctrlKey: true });
}

function keysFor(label: string): string[] {
  const row = screen.getByText(label).closest("li");
  expect(row).toBeTruthy();
  return Array.from(row!.querySelectorAll('[data-slot="kbd"]')).map((key) => key.textContent ?? "");
}

describe("KeyboardShortcutsList composer rows", () => {
  it("shows Enter to send and Shift+Enter for a new line by default", () => {
    render(<KeyboardShortcutsList />);

    expect(keysFor("Send message")).toEqual(["↵"]);
    expect(keysFor("New line in message")).toEqual(["⇧", "↵"]);
  });

  it("shows Ctrl+Enter to send and Enter for a new line in alternate mode", () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    render(<KeyboardShortcutsList />);

    expect(keysFor("Send message")).toEqual(["Ctrl", "↵"]);
    expect(keysFor("New line in message")).toEqual(["↵"]);
  });

  it("does not advertise inactive composer chords on touch-primary devices", () => {
    const matchMedia = window.matchMedia;
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
      ...matchMedia(query),
      matches: query.includes("pointer: coarse"),
    }));
    render(<KeyboardShortcutsList />);

    expect(screen.queryByText("Send message")).toBeNull();
    expect(screen.queryByText("New line in message")).toBeNull();
  });
});

describe("KeyboardShortcutsDialog", () => {
  it("renders nothing until opened", () => {
    renderDialog();
    expect(screen.queryByText("Send message")).toBeNull();
  });

  it("opens on the modifier+/ hotkey and lists one shortcut from each group", () => {
    renderDialog();
    expect(toggleViaHotkey()).toBe(false);

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

  it("toggles closed on a second hotkey press", async () => {
    renderDialog();
    toggleViaHotkey();
    expect(screen.getByText("Send message")).toBeTruthy();

    toggleViaHotkey();
    await waitFor(() => expect(screen.queryByText("Send message")).toBeNull());
  });

  it("opens when openKeyboardShortcuts() is dispatched (menu entry path)", async () => {
    renderDialog();
    openKeyboardShortcuts();
    // The event dispatch isn't wrapped in act(), so wait for the re-render.
    expect(await screen.findByText("Send message")).toBeTruthy();
  });

  it("shows the pinned-session shortcut with the Alt chord in a plain browser", () => {
    renderDialog();
    toggleViaHotkey();
    const row = screen.getByText("Jump to pinned session (1–10)").closest("li");
    expect(row).toBeTruthy();
    // Browser chord adds Alt (jsdom navigator is non-mac → "Alt") + the 1…0 chip.
    expect(within(row!).getByText("Alt")).toBeTruthy();
    expect(within(row!).getByText("1…0")).toBeTruthy();
  });

  it("shows the pinned-session shortcut without Alt in the Electron shell", () => {
    isNativeShell.mockReturnValue(true);
    renderDialog();
    toggleViaHotkey();
    const row = screen.getByText("Jump to pinned session (1–10)").closest("li");
    expect(row).toBeTruthy();
    expect(within(row!).queryByText("Alt")).toBeNull();
    expect(within(row!).getByText("1…0")).toBeTruthy();
  });
});
