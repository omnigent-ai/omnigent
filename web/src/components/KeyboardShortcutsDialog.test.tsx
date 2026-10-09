import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  KeyboardShortcutsDialog,
  KeyboardShortcutsList,
  openKeyboardShortcuts,
} from "./KeyboardShortcutsDialog";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "@/lib/composerSendShortcutPreferences";

// The pinned-session row shows in both shells; only its chord differs (Alt in
// the browser). Default the mock to browser (false); flip per-test for native.
const isNativeShell = vi.fn(() => false);
const isElectronShell = vi.fn(() => false);
const supportsBrowser = vi.fn(() => false);
vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: () => isNativeShell(),
  isElectronShell: () => isElectronShell(),
  supportsBrowser: () => supportsBrowser(),
  // DialogContent (rendered here) reads isIOSShell to size modals for the iOS
  // keyboard; this suite exercises the browser path, so it's always false.
  isIOSShell: () => false,
}));

beforeEach(() => {
  isNativeShell.mockReturnValue(false);
  isElectronShell.mockReturnValue(false);
  supportsBrowser.mockReturnValue(false);
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
});

// jsdom's navigator is non-mac, so the modifier glyph renders as "Ctrl".
function toggleViaHotkey() {
  fireEvent.keyDown(window, { key: "/", ctrlKey: true });
}

function keysFor(label: string): string[] {
  const row = screen.getByText(label).closest("li");
  expect(row).toBeTruthy();
  return Array.from(row!.querySelectorAll('[data-slot="kbd"]')).map((key) => key.textContent ?? "");
}

describe("KeyboardShortcutsList composer rows", () => {
  it("shows Enter to send and Shift+Enter or Alt+Enter for a new line by default", () => {
    render(<KeyboardShortcutsList />);

    expect(keysFor("Send message")).toEqual(["↵"]);
    expect(keysFor("New line in message")).toEqual(["⇧", "↵", "Alt", "↵"]);
    expect(screen.getByText("New line in message").closest("li")).toHaveTextContent("⇧↵orAlt↵");
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

describe("KeyboardShortcutsList settings layout", () => {
  it("groups shortcut rows into settings cards", () => {
    render(<KeyboardShortcutsList variant="settings" />);

    const heading = screen.getByRole("heading", { name: "General" });
    const list = heading.closest("section")?.querySelector("ul");
    expect(heading).toHaveClass("text-ui", "text-foreground");
    expect(list).toHaveClass("rounded-xl", "border-border", "bg-card");
  });
});

describe("KeyboardShortcutsDialog", () => {
  it("advertises the session-search chord without taking the Print shortcut", () => {
    render(<KeyboardShortcutsList />);
    expect(keysFor("Find a session by name")).toEqual(["Ctrl", "Alt", "S"]);
  });

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
    expect(keysFor("Start a new session")).toEqual(["Ctrl", "Alt", "N"]);
    expect(screen.getByText("Open command palette")).toBeTruthy();
    expect(keysFor("Open Settings")).toEqual(["Ctrl", "Alt", ","]);
    expect(screen.getByText("Show keyboard shortcuts")).toBeTruthy();
    expect(screen.getByText("Send message")).toBeTruthy();
    expect(keysFor("Open model picker")).toEqual(["Ctrl", "⇧", "M"]);
    expect(screen.getByText("Recall previous prompt")).toBeTruthy();
    expect(screen.getByText("Previous session")).toBeTruthy();
    expect(keysFor("Previous session")).toEqual(["Ctrl", "["]);
    expect(keysFor("Next session")).toEqual(["Ctrl", "]"]);
    expect(keysFor("Toggle Chat / Terminal view")).toEqual(["Ctrl", "Alt", "\\"]);
    expect(screen.getByText("Toggle conversations sidebar")).toBeTruthy();
    expect(screen.getByText("Focus or close workspace sidebar")).toBeTruthy();
    expect(screen.queryByText("Open a new browser tab")).toBeNull();
    expect(screen.getByText("Open a new shell")).toBeTruthy();
    const workspaceTabRow = screen.getByText("Select a workspace tab").closest("li");
    expect(keysFor("Select a workspace tab")).toEqual(["Ctrl", "Alt", "]", "1…4"]);
    expect(workspaceTabRow?.textContent?.match(/\+/g)).toHaveLength(1);
    expect(screen.getByText("Navigate suggestions")).toBeTruthy();
  });

  it("shows the Browser shortcut only when the desktop bridge supports it", () => {
    const { rerender } = render(<KeyboardShortcutsList />);
    expect(screen.queryByText("Open a new browser tab")).toBeNull();

    supportsBrowser.mockReturnValue(true);
    rerender(<KeyboardShortcutsList />);
    expect(keysFor("Open a new browser tab")).toEqual(["Ctrl", "Alt", "B"]);
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

  it("shows the recent-session switcher only in Electron", () => {
    const { rerender } = render(<KeyboardShortcutsList />);
    expect(screen.queryByText("Switch recent sessions")).toBeNull();

    isNativeShell.mockReturnValue(true);
    isElectronShell.mockReturnValue(true);
    rerender(<KeyboardShortcutsList />);
    expect(keysFor("Switch recent sessions")).toEqual(["Ctrl", "Tab"]);
    expect(
      screen.getByRole("heading", { name: "Navigation" }).closest("section")?.querySelector("li"),
    ).toHaveTextContent("Switch recent sessions");
  });
});
