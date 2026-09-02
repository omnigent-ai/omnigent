import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ActionsProvider,
  KeybindingDispatcher,
  getKeybindingSnapshot,
  setUserKeybindingRule,
  unbindDefaultKeybinding,
} from "@/actions";
import { resetKeybindingStoreForTesting } from "@/actions/KeybindingStore";
import { EmbeddedProvider } from "@/lib/embedded";
import { KeyboardShortcutsDialog, openKeyboardShortcuts } from "./KeyboardShortcutsDialog";
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
  resetKeybindingStoreForTesting();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  resetKeybindingStoreForTesting();
});

function renderDialog(embedded = false) {
  const content = (
    <ActionsProvider>
      <KeybindingDispatcher />
      <KeyboardShortcutsDialog />
    </ActionsProvider>
  );
  return render(embedded ? <EmbeddedProvider>{content}</EmbeddedProvider> : content);
}

// jsdom's navigator is non-mac, so the modifier glyph renders as "Ctrl".
function toggleViaHotkey() {
  return fireEvent.keyDown(window, { key: "/", ctrlKey: true });
}

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
    expect(screen.getByText("New chat")).toBeTruthy();
    expect(screen.getByText("Open command palette")).toBeTruthy();
    expect(screen.getByText("Open keyboard shortcuts")).toBeTruthy();
    expect(screen.getByText("Send message")).toBeTruthy();
    expect(screen.getByText("Recall previous prompt")).toBeTruthy();
    expect(screen.getByText("Open previous session")).toBeTruthy();
    expect(screen.getByText("Toggle conversations sidebar")).toBeTruthy();
    expect(screen.getByText("Select previous suggestion")).toBeTruthy();
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

  it("shows the active pinned-session chord for the current shell", () => {
    renderDialog();
    toggleViaHotkey();
    let row = screen.getByText(/Open pinned session/).closest("li");
    expect(within(row!).getByText("Ctrl+Alt+1")).toBeTruthy();

    cleanup();
    isNativeShell.mockReturnValue(true);
    renderDialog();
    toggleViaHotkey();
    row = screen.getByText(/Open pinned session/).closest("li");
    expect(within(row!).getByText("Ctrl+1")).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(16);
    expect(screen.queryByText("Stop response")).toBeNull();
  });

  it("includes a newly customized action outside the default compact set", () => {
    expect(
      setUserKeybindingRule({
        id: "user.navigateInbox",
        action: "workbench.action.navigateInbox",
        sequence: "ctrl+i",
        mode: "global",
      }),
    ).toEqual({ ok: true, changed: true });
    renderDialog();
    toggleViaHotkey();
    expect(screen.getByText("Go to Inbox")).toBeInTheDocument();
  });

  it("includes a user alternate even when the action also has an intact default", () => {
    expect(
      setUserKeybindingRule({
        id: "user.file.save",
        action: "file.action.save",
        sequence: "ctrl+alt+s",
        mode: "fileViewer",
      }),
    ).toEqual({ ok: true, changed: true });
    renderDialog();
    toggleViaHotkey();
    const row = screen.getByText("Save file").closest("li")!;
    expect(within(row).getByText("Ctrl+S")).toBeInTheDocument();
    expect(within(row).queryByText("Ctrl+Alt+S")).toBeNull();
  });

  it("does not advertise standalone-only shortcuts when embedded", () => {
    renderDialog(true);
    toggleViaHotkey();
    expect(screen.queryByText("New chat")).toBeNull();
    expect(screen.getByText("Open command palette")).toBeInTheDocument();
  });

  it("updates and removes compact hints live from the effective keymap", () => {
    renderDialog();
    toggleViaHotkey();
    const paletteRow = screen.getByText("Open command palette").closest("li")!;
    expect(within(paletteRow).getByText("Ctrl+K")).toBeTruthy();

    act(() => {
      expect(
        setUserKeybindingRule({
          id: "workbench.showCommands",
          action: "workbench.action.showCommands",
          sequence: "ctrl+shift+k",
          mode: "global",
        }),
      ).toEqual({ ok: true, changed: true });
    });
    expect(within(paletteRow).getByText("Ctrl+Shift+K")).toBeTruthy();

    const defaultRule = getKeybindingSnapshot().defaultRules.find(
      (rule) => rule.id === "workbench.showCommands",
    )!;
    act(() => {
      expect(unbindDefaultKeybinding(defaultRule)).toEqual({ ok: true, changed: true });
    });
    expect(screen.queryByText("Open command palette")).toBeNull();
  });
});
