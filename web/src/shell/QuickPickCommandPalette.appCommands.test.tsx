// The app commands only the QuickPick ⌘K offers, listed with its other actions.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { KEYBOARD_SHORTCUTS_EVENT } from "@/components/KeyboardShortcutsDialog";
import { registerFindSurface } from "@/lib/findRouting";

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
