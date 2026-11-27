import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  isCommandAvailable,
  isCommandListed,
  type PaletteCommand,
  usePaletteCommandsStore,
} from "@/store/paletteCommands";
import { useRecentSessionSwitchStore } from "@/hooks/useRecentSessionSwitch";
import { conversationPage } from "@/test/sidebarMockHelpers";
import { renderSidebar } from "@/test/sidebarTestHelpers";
import { resetArchiveNavigationForTests } from "./sessionCommands/archiveNavigation";

// With quick_pick_palette on, ⌘K offers the sidebar's selection: Select all, Deselect all,
// and the selection bar's actions, listed and greyed exactly as the bar's buttons are.

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
const writes = vi.hoisted(() => ({
  move: { mutate: vi.fn(), isPending: false, isError: false },
  archive: { mutate: vi.fn(), isPending: false, isError: false },
}));
vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useBulkMoveToProject: () => writes.move,
    useBulkArchiveConversations: () => writes.archive,
    useProjects: () => ({ data: [{ id: "p1", name: "Payments", icon: null }] }),
  };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";

const row = (id: string, title: string, updatedAt: number): Conversation => ({
  id,
  object: "conversation",
  title,
  created_at: 1_700_000_000,
  updated_at: updatedAt,
  labels: {},
  permission_level: null,
  status: "idle",
});
const ROWS = [
  row("conv_a", "Alpha", 1_700_000_003),
  row("conv_b", "Beta", 1_700_000_002),
  row("conv_c", "Gamma", 1_700_000_001),
];
const QUICK_PICK = { quick_pick_palette: true };

beforeEach(() => {
  const page = conversationPage(ROWS);
  vi.mocked(useConversations).mockImplementation(() => page);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  writes.move.isPending = false;
  resetArchiveNavigationForTests();
  useRecentSessionSwitchStore.setState({
    history: [],
    readLiveSessionIds: null,
    sessionsKnown: false,
    sessionsFailed: false,
  });
  usePaletteCommandsStore.setState({ groups: {} });
});

const selectionCommands = (): PaletteCommand[] =>
  Object.entries(usePaletteCommandsStore.getState().groups)
    .filter(([source]) => source.startsWith("sidebar.selection"))
    .flatMap(([, group]) => group.commands);
const command = (label: string): PaletteCommand => {
  const found = selectionCommands().find((candidate) => candidate.label === label);
  if (!found) throw new Error(`No ⌘K command "${label}"`);
  return found;
};
const listed = () =>
  selectionCommands()
    .filter((candidate) => isCommandListed(candidate.availability))
    .map((candidate) => candidate.label);

function startSelecting() {
  fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
}
const selectedCount = () => screen.getByText(/\d+ selected/).textContent;

// The bar's buttons, by test id, and the ⌘K command each one is.
const BAR_COMMANDS: Record<string, RegExp> = {
  "bulk-mark-read": /^Mark selected as read$/,
  "bulk-mark-unread": /^Mark selected as unread$/,
  "bulk-archive": /^Archive selected$/,
  "bulk-unarchive": /^Unarchive selected$/,
  "bulk-move-to-project": /^Move selected to project$/,
  "bulk-delete": /^Delete/,
};

/** Each bar button has a ⌘K command, available exactly when the button is enabled. */
function expectCommandsMatchBar() {
  for (const [testId, label] of Object.entries(BAR_COMMANDS)) {
    const button = screen.queryByTestId(testId) as HTMLButtonElement | null;
    const offered = selectionCommands().find(
      (candidate) => label.test(candidate.label) && isCommandListed(candidate.availability),
    );
    expect(Boolean(offered), testId).toBe(button !== null);
    if (button && offered) {
      expect(isCommandAvailable(offered.availability), testId).toBe(!button.disabled);
    }
  }
}

describe("Sidebar selection in ⌘K", () => {
  it("offers nothing with quick_pick_palette off", async () => {
    renderSidebar({ features: {} });
    await screen.findByRole("link", { name: /Alpha/ });
    startSelecting();
    fireEvent.click(screen.getByRole("link", { name: /Alpha/ }));
    expect(selectionCommands()).toEqual([]);
  });

  it("selects and deselects every listed session", async () => {
    renderSidebar({ features: QUICK_PICK });
    await screen.findByRole("link", { name: /Alpha/ });
    expect(selectionCommands()).toEqual([]);
    startSelecting();
    expect(listed()).toContain("Select all sessions");
    expect(listed()).not.toContain("Deselect all sessions");
    act(() => command("Select all sessions").run());
    expect(selectedCount()).toBe("3 selected");
    act(() => command("Deselect all sessions").run());
    expect(selectedCount()).toBe("0 selected");
  });

  it("lists the bar's actions, greyed as its buttons are", async () => {
    renderSidebar({ features: QUICK_PICK });
    await screen.findByRole("link", { name: /Alpha/ });
    startSelecting();
    await waitFor(() => expect(selectionCommands().length).toBeGreaterThan(2));
    expectCommandsMatchBar();
    fireEvent.click(screen.getByRole("link", { name: /Alpha/ }));
    fireEvent.click(screen.getByRole("link", { name: /Beta/ }));
    expectCommandsMatchBar();
    expect(isCommandAvailable(command("Archive selected").availability)).toBe(true);
  });

  it("greys the actions while a bulk change is saving, as the bar's buttons are", async () => {
    writes.move.isPending = true;
    renderSidebar({ features: QUICK_PICK });
    await screen.findByRole("link", { name: /Alpha/ });
    startSelecting();
    fireEvent.click(screen.getByRole("link", { name: /Alpha/ }));
    expectCommandsMatchBar();
    expect(isCommandAvailable(command("Archive selected").availability)).toBe(false);
  });

  it("runs the bar's own actions", async () => {
    renderSidebar({ features: QUICK_PICK });
    await screen.findByRole("link", { name: /Alpha/ });
    startSelecting();
    fireEvent.click(screen.getByRole("link", { name: /Alpha/ }));
    const [payments] = command("Move selected to project").options?.() ?? [];
    expect(payments.label).toBe("Payments");
    act(() => payments.run());
    expect(writes.move.mutate).toHaveBeenCalledWith(
      { ids: ["conv_a"], project: "Payments" },
      expect.anything(),
    );
    act(() => command("Delete selected").run());
    expect(await screen.findByRole("dialog", { name: /Delete 1 session/ })).toBeTruthy();
  });

  it("archives the selection from Archive selected, and leaves the open session as the bar does", async () => {
    function LocationProbe() {
      return <output data-testid="location">{useLocation().pathname}</output>;
    }
    useRecentSessionSwitchStore.setState({
      history: ["conv_a", "conv_b", "conv_c"],
      readLiveSessionIds: () => new Set(ROWS.map((session) => session.id)),
      sessionsKnown: true,
      sessionsFailed: false,
    });
    renderSidebar({
      features: QUICK_PICK,
      route: "/c/conv_a",
      routePattern: "/c/:conversationId",
      children: <LocationProbe />,
    });
    await screen.findByRole("link", { name: /Alpha/ });
    startSelecting();
    fireEvent.click(screen.getByRole("link", { name: /Alpha/ }));
    fireEvent.click(screen.getByRole("link", { name: /Beta/ }));
    act(() => command("Archive selected").run());

    expect(writes.archive.mutate).toHaveBeenCalledTimes(1);
    const { ids, archived } = writes.archive.mutate.mock.calls[0][0];
    expect([...ids].sort()).toEqual(["conv_a", "conv_b"]);
    expect(archived).toBe(true);
    expect(screen.getByTestId("location").textContent).toBe("/c/conv_c");
  });

  it("names the selection in each action's ID, so an action picked before it changed does nothing", async () => {
    renderSidebar({ features: QUICK_PICK });
    await screen.findByRole("link", { name: /Alpha/ });
    startSelecting();
    fireEvent.click(screen.getByRole("link", { name: /Alpha/ }));
    const before = command("Delete selected").id;
    fireEvent.click(screen.getByRole("link", { name: /Beta/ }));
    expect(command("Delete selected").id).not.toBe(before);
    // ⌘K runs a picked row by its ID; the old one now finds no command, so nothing starts.
    expect(selectionCommands().find((candidate) => candidate.id === before)).toBeUndefined();
  });
});
