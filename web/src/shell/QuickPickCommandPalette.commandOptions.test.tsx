// A command's options in ⌘K, such as where Move Tab… puts a tab: its row opens a page of them,
// a choice runs once the palette closes, and a choice gone meanwhile keeps the palette open
// saying so.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ArrowRightLeftIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { create } from "zustand";

import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import { CommandContextProvider } from "./commandContext";
import { OPTION_GONE_STATUS } from "./commandOptionsPage";
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

/** The groups the test's Move Tab… can move the tab to, as its host holds them. */
const tabGroups = create<{ groups: string[]; offered: boolean }>(() => ({
  groups: ["Left", "Right"],
  offered: true,
}));
const moved = vi.fn();

function MoveTab() {
  const { offered } = tabGroups();
  const commands = useMemo<PaletteCommand[]>(
    () =>
      offered
        ? [
            {
              id: "move-tab",
              label: "Move Tab…",
              icon: ArrowRightLeftIcon,
              keywords: [],
              optionsEmptyMessage: "There's no other group to move it to.",
              options: () =>
                tabGroups.getState().groups.map((group) => ({
                  id: group,
                  label: `To ${group}`,
                  run: () => moved(group),
                })),
              run: () => undefined,
            },
          ]
        : [],
    [offered],
  );
  usePaletteCommands("test-tabs", "Tabs", commands);
  return null;
}

/** The file the command context names; changing it retargets every command. */
const target = create<{ filePath: string | null }>(() => ({ filePath: null }));

function Harness() {
  const [open, setOpen] = useState(true);
  const { filePath } = target();
  return (
    <CommandContextProvider session={null} view="other" workspaceTab={null} filePath={filePath}>
      <MoveTab />
      <QuickPickCommandPalette
        open={open}
        onOpenChange={setOpen}
        onToggleLeftSidebar={vi.fn()}
        onToggleRightSidebar={vi.fn()}
      />
    </CommandContextProvider>
  );
}

function renderPalette() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Harness />
    </QueryClientProvider>,
  );
}

const input = () => screen.getByRole("combobox");
const type = (text: string) => fireEvent.change(input(), { target: { value: text } });
const press = (key: string) => fireEvent.keyDown(input(), { key });
const heading = () =>
  document.querySelector('[data-slot="quick-pick-heading"]')?.textContent ?? null;
const status = () =>
  document.querySelector('[data-slot="palette-page-status"]')?.textContent ?? null;
const optionLabels = () =>
  screen.getAllByRole("option").map((row) => row.querySelector(".truncate")?.textContent);

async function openMoveTab() {
  type("Move Tab");
  await screen.findByRole("option", { name: /Move Tab…/ });
  press("Enter");
  await waitFor(() => expect(heading()).toBe("Move Tab"));
}

beforeEach(() => {
  tabGroups.setState({ groups: ["Left", "Right"], offered: true });
  target.setState({ filePath: null });
  moved.mockReset();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("a command's options in ⌘K", () => {
  it("opens a page of its options, with the query cleared and focus kept", async () => {
    renderPalette();
    await openMoveTab();
    expect(optionLabels()).toEqual(["To Left", "To Right"]);
    expect(input()).toHaveValue("");
    expect(input()).toHaveFocus();
  });

  it("runs the chosen option once the palette has closed", async () => {
    renderPalette();
    await openMoveTab();
    type("right");
    press("Enter");
    expect(moved).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(moved).toHaveBeenCalledExactlyOnceWith("Right"));
  });

  it.each([
    ["withdrawn", () => tabGroups.setState({ offered: false })],
    ["retargeted", () => target.setState({ filePath: "/other.ts" })],
  ])("runs nothing when the command is %s while the palette closes", async (_, change) => {
    renderPalette();
    await openMoveTab();
    type("right");
    press("Enter");
    // Before the close finishes and runs the option.
    act(() => change());
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    expect(moved).not.toHaveBeenCalled();
  });

  it("keeps the palette open, saying so, when the chosen option went meanwhile", async () => {
    renderPalette();
    await openMoveTab();
    type("left");
    tabGroups.setState({ groups: ["Right"] });
    press("Enter");
    await waitFor(() => expect(status()).toBe(OPTION_GONE_STATUS));
    expect(heading()).toBe("Move Tab");
    expect(moved).not.toHaveBeenCalled();
  });

  it("says why when there's nothing to choose", async () => {
    tabGroups.setState({ groups: [] });
    renderPalette();
    await openMoveTab();
    expect(screen.getByText("There's no other group to move it to.")).toBeTruthy();
  });

  it("leaves the page when the command is withdrawn", async () => {
    renderPalette();
    await openMoveTab();
    act(() => tabGroups.setState({ offered: false }));
    await waitFor(() => expect(heading()).toBeNull());
    expect(moved).not.toHaveBeenCalled();
  });

  it("goes back to the commands on Escape, with the query and row as they were", async () => {
    renderPalette();
    await openMoveTab();
    press("Escape");
    await waitFor(() => expect(heading()).toBeNull());
    expect(input()).toHaveValue("Move Tab");
    expect(document.querySelector('[role="option"][aria-selected="true"]')).toHaveTextContent(
      "Move Tab…",
    );
  });
});
