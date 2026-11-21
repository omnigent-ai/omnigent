// The app commands only the QuickPick ⌘K offers, listed with its other actions.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { KEYBOARD_SHORTCUTS_EVENT } from "@/components/KeyboardShortcutsDialog";
import { readAlwaysSteer } from "@/lib/alwaysSteerPreferences";
import { registerFindSurface } from "@/lib/findRouting";
import { readFoldAssistantNarration } from "@/lib/foldNarrationPreferences";
import { useThemePickerStore } from "@/store/themePicker";
import { readTerminalThemeMode } from "@/lib/terminalThemePreferences";

import { QuickPickCommandPalette } from "./QuickPickCommandPalette";

vi.mock("@/lib/routing", () => ({
  useNavigate: () => vi.fn(),
  useLocation: () => ({ pathname: "/", search: "", hash: "", state: null, key: "default" }),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ session_search_scope: true }),
}));
vi.mock("@/hooks/useConversations", () => ({
  useConversations: () => ({ data: { pages: [] }, isFetching: false }),
  useSessionSearch: () => ({ isFetching: false }),
  useProjects: () => ({ data: [] }),
}));

afterEach(() => {
  cleanup();
  localStorage.clear();
});

function Palette() {
  const [open, setOpen] = useState(true);
  return (
    <QuickPickCommandPalette
      open={open}
      onOpenChange={setOpen}
      onToggleLeftSidebar={vi.fn()}
      onToggleRightSidebar={vi.fn()}
    />
  );
}

function renderPalette() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Palette />
    </QueryClientProvider>,
  );
  return screen.getByTestId("command-palette-input");
}

const actionNames = () =>
  within(screen.getByRole("group", { name: "Actions" }))
    .getAllByRole("option")
    .map((option) => option.textContent ?? "");

describe("QuickPick ⌘K Go to Session…", () => {
  // As the shell does: the palette continues in its sessions-only mode, as a fresh palette.
  function Shell() {
    const [open, setOpen] = useState(false);
    const [sessionsOnly, setSessionsOnly] = useState(false);
    return (
      <>
        <button
          type="button"
          onClick={() => {
            setSessionsOnly(false);
            setOpen(true);
          }}
        >
          Search
        </button>
        <QuickPickCommandPalette
          key={sessionsOnly ? "sessions" : "commands"}
          sessionsOnly={sessionsOnly}
          open={open}
          onOpenChange={setOpen}
          onToggleLeftSidebar={vi.fn()}
          onToggleRightSidebar={vi.fn()}
          onGoToSession={() => setSessionsOnly(true)}
        />
      </>
    );
  }

  it("switches the open palette to sessions, focused, and Escape returns where ⌘K opened", async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Shell />
      </QueryClientProvider>,
    );
    const opener = screen.getByRole("button", { name: "Search" });
    opener.focus();
    fireEvent.click(opener);
    const input = await screen.findByTestId("command-palette-input");
    fireEvent.change(input, { target: { value: "go to session" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const sessions = await screen.findByRole("combobox", { name: "Switch session" });
    await waitFor(() => expect(sessions).toHaveFocus());
    // The palette it replaced finishes closing without taking focus back.
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });
    expect(sessions).toHaveFocus();
    fireEvent.keyDown(sessions, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("isn't offered where the palette can't switch", () => {
    renderPalette();
    expect(actionNames().some((name) => name.includes("Go to Session"))).toBe(false);
  });
});

describe("QuickPick ⌘K setting rows", () => {
  const heading = () =>
    document.querySelector('[data-slot="quick-pick-heading"]')?.textContent ?? null;

  it.each([
    { label: "Always steer", from: "On", to: "Off", read: readAlwaysSteer, saved: false },
    {
      label: "Show only the final answer",
      from: "Off",
      to: "On",
      read: readFoldAssistantNarration,
      saved: true,
    },
  ])(
    "changes $label from its row, back on the row showing $to",
    async ({ label, from, to, read, saved }) => {
      const input = renderPalette();
      const row = () => screen.getByRole("option", { name: new RegExp(label) });
      fireEvent.change(input, { target: { value: label.toLowerCase() } });
      expect(row()).toHaveTextContent(from);
      fireEvent.keyDown(input, { key: "Enter" });
      await waitFor(() => expect(heading()).toBe(label));
      fireEvent.change(input, { target: { value: to.toLowerCase() } });
      fireEvent.keyDown(input, { key: "Enter" });
      expect(read()).toBe(saved);
      await waitFor(() => expect(heading()).toBeNull());
      expect(row()).toHaveTextContent(to);
    },
  );

  it("finds the terminal theme by a value's name and opens on it", async () => {
    const input = renderPalette();
    fireEvent.change(input, { target: { value: "terminal theme dark" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(heading()).toBe("Terminal theme"));
    fireEvent.keyDown(input, { key: "Enter" });
    expect(readTerminalThemeMode()).toBe("dark");
  });
});

describe("QuickPick ⌘K app commands", () => {
  it("opens the keyboard shortcuts once the palette has closed", async () => {
    const opened = vi.fn(() => expect(screen.queryByRole("dialog")).toBeNull());
    window.addEventListener(KEYBOARD_SHORTCUTS_EVENT, opened);
    onTestFinished(() => window.removeEventListener(KEYBOARD_SHORTCUTS_EVENT, opened));
    const input = renderPalette();
    fireEvent.change(input, { target: { value: "keyboard shortcuts" } });
    expect(actionNames()[0]).toContain("Show keyboard shortcuts");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(opened).not.toHaveBeenCalled();
    await waitFor(() => expect(opened).toHaveBeenCalledOnce());
  });

  it("opens the theme picker once the palette has closed", async () => {
    onTestFinished(() => useThemePickerStore.setState({ open: false, replaced: false }));
    const input = renderPalette();
    fireEvent.change(input, { target: { value: "select theme" } });
    expect(actionNames()[0]).toContain("Select theme…");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(useThemePickerStore.getState().open).toBe(false);
    await waitFor(() => expect(useThemePickerStore.getState().open).toBe(true));
  });

  it("offers Find only while something on screen can find, and opens it there", async () => {
    const input = renderPalette();
    expect(actionNames().some((name) => name.includes("Find…"))).toBe(false);
    const openFind = vi.fn();
    let unregister = () => undefined as void;
    act(() => {
      unregister = registerFindSurface({ isActive: () => true, openFind });
    });
    onTestFinished(() => unregister());
    fireEvent.change(input, { target: { value: "find in conversation" } });
    expect(actionNames()[0]).toContain("Find…");
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(openFind).toHaveBeenCalledOnce());
  });
});
