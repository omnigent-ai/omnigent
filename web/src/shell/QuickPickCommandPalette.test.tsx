import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { FlagIcon } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type PaletteCommand, usePaletteCommandsStore } from "@/store/paletteCommands";

import { QuickPickCommandPalette } from "./QuickPickCommandPalette";

// Behavior specific to the QuickPick palette; CommandPalette.quickPick.test.tsx runs
// every shared CommandPalette case against it.

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
  useLocation: () => ({ pathname: "/", search: "", hash: "", state: null, key: "default" }),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ session_search_scope: true }),
}));
const useConversations = vi.fn();
vi.mock("@/hooks/useConversations", () => ({
  useConversations: (...args: unknown[]) => useConversations(...args),
  useSessionSearch: () => ({ isFetching: false }),
  useProjects: () => ({ data: [] }),
}));

beforeEach(() => {
  navigate.mockClear();
  useConversations.mockReturnValue({
    data: {
      pages: [{ data: [{ id: "c1", title: "Fix the parser", agent_name: null, archived: false }] }],
    },
    isFetching: false,
  });
});
afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
});

function renderPalette() {
  const onOpenChange = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <QuickPickCommandPalette
        open
        onOpenChange={onOpenChange}
        onToggleLeftSidebar={vi.fn()}
        onToggleRightSidebar={vi.fn()}
      />
    </QueryClientProvider>,
  );
  return { onOpenChange, input: screen.getByTestId("command-palette-input") };
}

describe("QuickPickCommandPalette", () => {
  it("lines session labels up with action labels through a blank icon slot", () => {
    renderPalette();
    const session = screen.getByRole("option", { name: /Fix the parser/ });
    const action = screen.getByRole("option", { name: /Go to Settings/ });
    // Both rows start with the same icon slot; the session's is empty.
    const sessionSlot = session.firstElementChild;
    const actionSlot = action.firstElementChild;
    expect(sessionSlot?.className).toBe(actionSlot?.className);
    expect(sessionSlot?.querySelector("svg")).toBeNull();
    expect(actionSlot?.querySelector("svg")).toBeTruthy();
  });

  it("finds actions by their initials and underlines the letters they hit", () => {
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "gts" } });
    const actions = within(screen.getByRole("group", { name: "Actions" })).getAllByRole("option");
    expect(actions[0].textContent).toContain("Go to Settings");
    const marks = [...actions[0].querySelectorAll("mark")];
    expect(marks.map((mark) => mark.textContent)).toEqual(["G", "t", "S"]);
    expect(marks.every((mark) => mark.dataset.matchHighlight === "underline")).toBe(true);
  });

  it("moves between groups with Alt+Arrow and runs the action with Enter", () => {
    const { input, onOpenChange } = renderPalette();
    fireEvent.keyDown(input, { key: "ArrowDown", altKey: true });
    const active = document.getElementById(input.getAttribute("aria-activedescendant") ?? "");
    expect(active?.closest('[role="group"]')?.getAttribute("aria-labelledby")).toBeTruthy();
    expect(within(screen.getByRole("group", { name: "Actions" })).getAllByRole("option")[0]).toBe(
      active,
    );
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(navigate).toHaveBeenCalledWith("/");
  });
});

describe("QuickPickCommandPalette feature commands", () => {
  // No sessions, so the command the query names is the first row.
  beforeEach(() => useConversations.mockReturnValue({ data: { pages: [] }, isFetching: false }));
  const offer = (commands: PaletteCommand[]) =>
    act(() =>
      usePaletteCommandsStore.getState().register("tabs", { heading: "Workspace", commands }),
    );
  const command = (id: string, overrides: Partial<PaletteCommand> = {}): PaletteCommand => ({
    id,
    label: id,
    icon: FlagIcon,
    keywords: [],
    run: vi.fn(),
    ...overrides,
  });
  const accept = (input: HTMLElement, query: string) => {
    fireEvent.change(input, { target: { value: query } });
    fireEvent.keyDown(input, { key: "Enter" });
  };

  it("lists a feature's commands under its heading and runs one", () => {
    const reopen = command("Reopen closed tab");
    offer([reopen]);
    const { input, onOpenChange } = renderPalette();
    expect(
      within(screen.getByRole("group", { name: "Workspace" })).getByRole("option", {
        name: /Reopen closed tab/,
      }),
    ).toBeTruthy();
    accept(input, "reopen closed");
    expect(reopen.run).toHaveBeenCalledOnce();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("runs a command that moves focus only once the palette has closed", async () => {
    const focusFilter = command("Filter files", { movesFocus: true });
    offer([focusFilter]);
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
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Palette />
      </QueryClientProvider>,
    );
    accept(screen.getByTestId("command-palette-input"), "filter files");
    expect(focusFilter.run).not.toHaveBeenCalled();
    await waitFor(() => expect(focusFilter.run).toHaveBeenCalledOnce());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps the palette open when the accepted row's command was withdrawn or greyed out since", () => {
    const later = command("Later");
    offer([later]);
    const { input, onOpenChange } = renderPalette();
    fireEvent.change(input, { target: { value: "later" } });
    // Withdrawn between the render and the accept, as a feature unmounting would.
    usePaletteCommandsStore.setState({ groups: {} });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(later.run).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();
  });
});
